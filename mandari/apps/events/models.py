# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tabellen der Ereignistechnik (``docs/adr/20260929-ereignistechnik-postgres.md``).

- ``Event`` (``events_event``): das Journal. Fachmodule schreiben es in derselben Transaktion wie
  die fachliche Änderung (transaktionale Outbox). ``seq`` bleibt beim Schreiben leer und wird erst
  nach dem Commit vom Sequenzierer vergeben; ``xid`` setzt die Datenbank.
- ``Subscription`` (``events_subscription``): benannter Konsument mit Cursor.
- ``ParkedEvent`` (``events_parked``): Wiederholung und tote Ereignisse je Abonnement.
- ``Task`` (``events_task``): Aufträge des Tasks-Backends.
- ``Lease`` (``events_lease``): Leader-Rollen (Sequenzierer, Zeitpläne) ohne sitzungsgebundene
  Sperren, damit ein Verbindungspooler im Transaktionsmodus möglich bleibt.

Spaltenstandards liegen in der Datenbank (``db_default``), weil auch der Ingestor ohne Django in
das Journal schreibt. Auf PostgreSQL kommen die Sequenz ``events_seq`` und der Weckruf-Trigger
dazu (Migration ``0001_initial``).

Ereignisse enthalten nur Kennungen und Namen geänderter Felder, nie Inhalte, Mailadressen oder
Namen (``visibility`` und ``tenant_ref`` sind Pflicht).
"""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models.functions import Now

from .fields import CurrentTransactionId, TransactionIdField

#: Kanal des Weckrufs nach neuen Journalzeilen (Trigger ``events_event_notify``).
NOTIFY_CHANNEL = "mandari_events"
#: Sequenz, aus der der Sequenzierer ``seq`` vergibt.
SEQUENCE_NAME = "events_seq"


class Visibility(models.TextChoices):
    """Sichtbarkeit eines Ereignisses; der öffentliche Feed und die Suche filtern darauf."""

    OEFFENTLICH = "oeffentlich", "öffentlich"
    NICHTOEFFENTLICH = "nichtoeffentlich", "nichtöffentlich"
    INTERN = "intern", "intern"
    PERSONENBEZOGEN = "personenbezogen", "personenbezogen"


class Operation(models.TextChoices):
    """Operation im Änderungsfeed (``docs/adr/20260929-aenderungsfeed-format.md``)."""

    UPSERT = "upsert", "angelegt oder geändert"
    DELETE = "delete", "gelöscht"
    REDACT = "redact", "unkenntlich gemacht"


class SubscriptionState(models.TextChoices):
    AKTIV = "aktiv", "aktiv"
    PAUSIERT = "pausiert", "pausiert"
    SCHATTEN = "schatten", "Schattenbetrieb"


class ParkedState(models.TextChoices):
    WIEDERHOLEN = "wiederholen", "wird wiederholt"
    BLOCKIERT = "blockiert", "blockiert (Folgeereignis)"
    TOT = "tot", "tot"


class TaskStatus(models.TextChoices):
    WARTEND = "wartend", "wartend"
    LAEUFT = "laeuft", "läuft"
    ERLEDIGT = "erledigt", "erledigt"
    FEHLGESCHLAGEN = "fehlgeschlagen", "fehlgeschlagen"
    TOT = "tot", "tot"


class Event(models.Model):
    """Eine Zeile im Journal: unveränderliche Meldung „etwas ist geschehen“."""

    id = models.BigAutoField(primary_key=True)
    event_id = models.UUIDField("Ereignis-ID", unique=True, default=uuid.uuid4, editable=False)
    type = models.TextField("Typ", help_text="z. B. ris.paper.released")
    version = models.SmallIntegerField("Schemaversion")
    aggregate_type = models.TextField("Objekttyp", help_text="kanonischer Typ, z. B. Paper")
    aggregate_id = models.UUIDField("Objekt-ID", help_text="kanonische ID")
    tenant_ref = models.TextField("Mandant", help_text="session:<uuid>, org:<uuid> oder source:<uuid>")
    body_id = models.UUIDField("Kommune", null=True, blank=True)
    visibility = models.TextField("Sichtbarkeit", choices=Visibility.choices)
    operation = models.TextField(
        "Operation", choices=Operation.choices, default=Operation.UPSERT, db_default=Operation.UPSERT
    )
    occurred_at = models.DateTimeField("geschehen am")
    recorded_at = models.DateTimeField("erfasst am", db_default=Now(), editable=False)
    actor_ref = models.TextField("ausgelöst von", null=True, blank=True, help_text="user:<uuid> oder system:<job>")
    correlation_id = models.UUIDField("Korrelations-ID")
    causation_id = models.UUIDField("Auslöser-ID", null=True, blank=True)
    payload = models.JSONField("Nutzlast", default=dict, help_text="nur Kennungen und Namen geänderter Felder")
    xid = TransactionIdField("Transaktion", db_default=CurrentTransactionId(), editable=False)
    seq = models.BigIntegerField("Folgenummer", null=True, blank=True, unique=True, editable=False)

    class Meta:
        db_table = "events_event"
        verbose_name = "Ereignis"
        verbose_name_plural = "Ereignisse"
        indexes = [
            # Arbeitsvorrat des Sequenzierers: nur Zeilen ohne Folgenummer, in Transaktionsreihenfolge
            models.Index(fields=["xid", "id"], name="events_event_unsequenced", condition=models.Q(seq__isnull=True)),
            # Zustellung: seq > cursor, gefiltert nach Typ
            models.Index(fields=["seq", "type"], name="events_event_seq_type", condition=models.Q(seq__isnull=False)),
            models.Index(fields=["aggregate_id", "seq"], name="events_event_aggregate"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(visibility__in=Visibility.values), name="events_event_visibility_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(operation__in=Operation.values), name="events_event_operation_valid"
            ),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="events_event_version_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.type} v{self.version} ({self.aggregate_type} {self.aggregate_id})"


class Subscription(models.Model):
    """Benannter Konsument mit Cursor (letzte verarbeitete Folgenummer)."""

    name = models.TextField("Name", primary_key=True, help_text="z. B. suchindex")
    cursor_seq = models.BigIntegerField("Cursor", default=0, db_default=0)
    state = models.TextField(
        "Zustand",
        choices=SubscriptionState.choices,
        default=SubscriptionState.AKTIV,
        db_default=SubscriptionState.AKTIV,
    )
    updated_at = models.DateTimeField("aktualisiert am", auto_now=True, db_default=Now())

    class Meta:
        db_table = "events_subscription"
        verbose_name = "Abonnement"
        verbose_name_plural = "Abonnements"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(state__in=SubscriptionState.values), name="events_subscription_state_valid"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class ParkedEvent(models.Model):
    """Geparktes Ereignis eines Abonnements: wird wiederholt, blockiert Folgeereignisse oder ist tot.

    Statt eines zusammengesetzten Primärschlüssels gibt es eine Ersatzspalte ``id`` und die
    eindeutige Kombination aus Abonnement und Folgenummer: Die Admin-Seite kann Modelle mit
    zusammengesetztem Schlüssel nicht anzeigen.
    """

    id = models.BigAutoField(primary_key=True)
    subscription = models.TextField("Abonnement")
    event_seq = models.BigIntegerField("Folgenummer")
    aggregate_id = models.UUIDField("Objekt-ID")
    state = models.TextField("Zustand", choices=ParkedState.choices)
    attempts = models.IntegerField("Versuche", default=0, db_default=0)
    next_attempt_at = models.DateTimeField("nächster Versuch", null=True, blank=True)
    error_code = models.TextField(
        "Fehlercode", null=True, blank=True, help_text="Ausnahmeklasse oder fester Code, keine Inhalte"
    )

    class Meta:
        db_table = "events_parked"
        verbose_name = "geparktes Ereignis"
        verbose_name_plural = "geparkte Ereignisse"
        constraints = [
            models.UniqueConstraint(fields=["subscription", "event_seq"], name="events_parked_subscription_seq"),
            models.CheckConstraint(condition=models.Q(state__in=ParkedState.values), name="events_parked_state_valid"),
        ]
        indexes = [
            # Reihenfolge je Objekt: Folgeereignisse eines geparkten Objekts werden mitgeparkt
            models.Index(fields=["subscription", "aggregate_id"], name="events_parked_aggregate"),
            models.Index(
                fields=["next_attempt_at"],
                name="events_parked_due",
                condition=models.Q(state=ParkedState.WIEDERHOLEN),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subscription} #{self.event_seq} ({self.state})"


class Task(models.Model):
    """Auftrag: technische Hintergrundarbeit ohne fachliche Aussage (``docs/adr/20260929-auftraege-und-zeitplaene.md``)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    queue = models.TextField("Warteschlange", help_text="default, mail, index, ocr, ai oder adapter")
    task_path = models.TextField("Auftrag", help_text="Importpfad der Funktion")
    args = models.JSONField("Argumente", default=dict, help_text="nur Kennungen, nie Inhalte")
    priority = models.SmallIntegerField("Priorität", default=50, db_default=50)
    run_after = models.DateTimeField("frühestens ab", db_default=Now())
    status = models.TextField(
        "Status", choices=TaskStatus.choices, default=TaskStatus.WARTEND, db_default=TaskStatus.WARTEND
    )
    attempts = models.IntegerField("Versuche", default=0, db_default=0)
    max_attempts = models.IntegerField("höchstens Versuche", default=8, db_default=8)
    idempotency_key = models.TextField("Idempotenzschlüssel", null=True, blank=True)
    locked_until = models.DateTimeField("gesperrt bis", null=True, blank=True)
    created_at = models.DateTimeField("angelegt am", db_default=Now(), editable=False)
    finished_at = models.DateTimeField("beendet am", null=True, blank=True)
    result_code = models.TextField("Ergebniscode", null=True, blank=True)

    class Meta:
        db_table = "events_task"
        verbose_name = "Auftrag"
        verbose_name_plural = "Aufträge"
        constraints = [
            # Als Constraint statt unique=True: kein zusätzlicher text_pattern_ops-Index auf der Warteschlange
            models.UniqueConstraint(fields=["idempotency_key"], name="events_task_idempotency_key"),
            models.CheckConstraint(condition=models.Q(status__in=TaskStatus.values), name="events_task_status_valid"),
        ]
        indexes = [
            models.Index(
                fields=["queue", "run_after"],
                name="events_task_ready",
                condition=models.Q(status=TaskStatus.WARTEND),
            ),
            models.Index(
                fields=["locked_until"],
                name="events_task_locked",
                condition=models.Q(status=TaskStatus.LAEUFT),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.task_path} ({self.status})"


class Lease(models.Model):
    """Leader-Rolle mit Ablaufzeit; wer sie hält, erneuert sie regelmäßig."""

    name = models.TextField("Rolle", primary_key=True, help_text="z. B. sequencer, scheduler")
    holder = models.TextField("Inhaber")
    expires_at = models.DateTimeField("läuft ab")

    class Meta:
        db_table = "events_lease"
        verbose_name = "Leader-Lease"
        verbose_name_plural = "Leader-Leases"

    def __str__(self) -> str:
        return f"{self.name} → {self.holder}"

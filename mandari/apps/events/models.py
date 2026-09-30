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
- ``ScheduleState`` (``events_schedule``): zuletzt geplanter Termin je Zeitplan.
- ``IdempotencyKey`` (``events_idempotency``): Idempotenzschlüssel mit Hash der Anfrage und
  gespeicherter Antwort, z. B. für Befehle (``apps.events.idempotency``).
- ``JournalPruning`` (``events_pruning``): wie weit Zeilen des Journals gelöscht wurden
  (``apps.events.pruning``).

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
            # Öffentliche Ereignisse einer Kommune in Folgenummer-Reihenfolge (öffentlicher Änderungsfeed):
            # seq > cursor je Kommune, ohne die Ereignisse aller anderen zu durchlaufen
            models.Index(
                fields=["body_id", "seq"],
                name="events_event_body_public",
                condition=models.Q(seq__isnull=False, visibility=Visibility.OEFFENTLICH),
            ),
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
            # Kette je Objekt: Folgeereignisse eines geparkten Objekts werden mitgeparkt, das nächste
            # rückt nach Folgenummer nach (``apps.events.dispatch``)
            models.Index(fields=["subscription", "aggregate_id", "event_seq"], name="events_parked_chain"),
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


class ScheduleState(models.Model):
    """Stand eines Zeitplans: zuletzt geplanter Termin. Die Zeitpläne selbst stehen im Code (``schedule.py``)."""

    name = models.TextField("Zeitplan", primary_key=True)
    last_slot = models.DateTimeField("zuletzt geplanter Termin")
    last_task_id = models.UUIDField("letzter Auftrag", null=True, blank=True)
    updated_at = models.DateTimeField("aktualisiert am", auto_now=True, db_default=Now())

    class Meta:
        db_table = "events_schedule"
        verbose_name = "Zeitplan-Stand"
        verbose_name_plural = "Zeitplan-Stände"

    def __str__(self) -> str:
        return f"{self.name} ({self.last_slot:%Y-%m-%d %H:%M})"


class IdempotencyKey(models.Model):
    """
    Idempotenzschlüssel mit Hash der Anfrage und gespeicherter Antwort (``apps.events.idempotency``).

    Befehle (``docs/adr/20260929-befehle-synchron.md``) belegen den Schlüssel in derselben Transaktion,
    in der der Eigentümer die Fachdaten schreibt. Eine Wiederholung mit gleichem Schlüssel und gleicher
    Anfrage erhält die gespeicherte Antwort. ``response`` enthält nur Kennungen und Codes, nie Inhalte.
    """

    id = models.BigAutoField(primary_key=True)
    scope = models.TextField("Bereich", help_text="Mandant und Auslöser, z. B. session:<uuid> user:<uuid>")
    key = models.TextField("Schlüssel")
    label = models.TextField(
        "Art", blank=True, default="", help_text="z. B. Name des Befehls, für Betrieb und Auswertung"
    )
    request_hash = models.CharField("Hash der Anfrage", max_length=64)
    response = models.JSONField("Antwort", default=dict, help_text="nur Kennungen und Codes, nie Inhalte")
    created_at = models.DateTimeField("angelegt am", db_default=Now(), editable=False)

    class Meta:
        db_table = "events_idempotency"
        verbose_name = "Idempotenzschlüssel"
        verbose_name_plural = "Idempotenzschlüssel"
        constraints = [models.UniqueConstraint(fields=["scope", "key"], name="events_idempotency_scope_key")]
        indexes = [models.Index(fields=["created_at"], name="events_idempotency_created")]

    def __str__(self) -> str:
        return f"{self.label or 'Idempotenzschlüssel'} ({self.scope})"


class JournalPruning(models.Model):
    """
    Ein Aufräumen des Journals: bis zu welcher Folgenummer und welchem Erfassungszeitpunkt Zeilen
    gelöscht wurden (``apps.events.pruning``).

    Wer Zeilen des Journals löscht (etwa ganze Monatspartitionen), hält das in derselben Transaktion
    hier fest. Aus den verbliebenen Zeilen lässt es sich nicht ablesen: Der Sequenzierer darf Nummern
    verwerfen (``nextval()`` ist nicht transaktional), Lücken in ``seq`` sind also kein Zeichen für
    Gelöschtes. Leser mit eigenem Stand (der öffentliche Änderungsfeed) erkennen daran, ob ihnen Zeilen
    fehlen können.
    """

    id = models.BigAutoField(primary_key=True)
    through_seq = models.BigIntegerField("gelöscht bis Folgenummer", help_text="höchste Folgenummer, die fehlen kann")
    recorded_before = models.DateTimeField(
        "erfasst vor", help_text="alle gelöschten Zeilen wurden vor diesem Zeitpunkt erfasst"
    )
    pruned_at = models.DateTimeField("aufgeräumt am", db_default=Now(), editable=False)

    class Meta:
        db_table = "events_pruning"
        verbose_name = "Aufräumen des Journals"
        verbose_name_plural = "Aufräumen des Journals"
        constraints = [
            models.CheckConstraint(condition=models.Q(through_seq__gte=1), name="events_pruning_seq_positive"),
        ]

    def __str__(self) -> str:
        return f"bis Folgenummer {self.through_seq}"

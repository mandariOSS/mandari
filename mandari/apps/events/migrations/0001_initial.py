# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignistechnik: Journal, Abonnements, geparkte Ereignisse, Aufträge und Leases (Issue #484).

Nur neue Tabellen; bestehender Code kennt sie nicht. Ein Rückfall auf ein älteres Image braucht
deshalb keinen Rückbau dieser Migration.

Auf PostgreSQL kommen zwei Datenbankobjekte dazu, die das Modell nicht abbildet:

- die Sequenz ``events_seq``, aus der allein der Sequenzierer ``seq`` vergibt. Sie gehört
  bewusst keiner Spalte (kein ``OWNED BY``), damit ein späterer Umbau des Journals, etwa die
  Partitionierung, den Zählerstand nicht mitlöscht;
- der Weckruf: ein Trigger ``AFTER INSERT … FOR EACH STATEMENT`` ruft
  ``pg_notify('mandari_events', '')``. PostgreSQL stellt die Meldung erst beim Commit zu und
  fasst gleiche Meldungen einer Transaktion zu einer zusammen; nach einem Rollback kommt keine.

SQLite (lokale Tests) bekommt nur die Tabellen.
"""

import apps.events.fields
import django.db.models.functions.datetime
import uuid
from django.db import migrations, models

POSTGRES_ANLEGEN = [
    "CREATE SEQUENCE IF NOT EXISTS events_seq AS bigint",
    """
    CREATE OR REPLACE FUNCTION events_event_notify() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM pg_notify('mandari_events', '');
        RETURN NULL;
    END;
    $$
    """,
    "DROP TRIGGER IF EXISTS events_event_notify ON events_event",
    "CREATE TRIGGER events_event_notify AFTER INSERT ON events_event "
    "FOR EACH STATEMENT EXECUTE FUNCTION events_event_notify()",
]

POSTGRES_ENTFERNEN = [
    "DROP TRIGGER IF EXISTS events_event_notify ON events_event",
    "DROP FUNCTION IF EXISTS events_event_notify()",
    "DROP SEQUENCE IF EXISTS events_seq",
]


def postgres_objekte_anlegen(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for sql in POSTGRES_ANLEGEN:
        schema_editor.execute(sql)


def postgres_objekte_entfernen(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for sql in POSTGRES_ENTFERNEN:
        schema_editor.execute(sql)


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Lease",
            fields=[
                (
                    "name",
                    models.TextField(
                        help_text="z. B. sequencer, scheduler",
                        primary_key=True,
                        serialize=False,
                        verbose_name="Rolle",
                    ),
                ),
                ("holder", models.TextField(verbose_name="Inhaber")),
                ("expires_at", models.DateTimeField(verbose_name="läuft ab")),
            ],
            options={
                "verbose_name": "Leader-Lease",
                "verbose_name_plural": "Leader-Leases",
                "db_table": "events_lease",
            },
        ),
        migrations.CreateModel(
            name="Event",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "event_id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        unique=True,
                        verbose_name="Ereignis-ID",
                    ),
                ),
                (
                    "type",
                    models.TextField(
                        help_text="z. B. ris.paper.released", verbose_name="Typ"
                    ),
                ),
                ("version", models.SmallIntegerField(verbose_name="Schemaversion")),
                (
                    "aggregate_type",
                    models.TextField(
                        help_text="kanonischer Typ, z. B. Paper",
                        verbose_name="Objekttyp",
                    ),
                ),
                (
                    "aggregate_id",
                    models.UUIDField(
                        help_text="kanonische ID", verbose_name="Objekt-ID"
                    ),
                ),
                (
                    "tenant_ref",
                    models.TextField(
                        help_text="session:<uuid>, org:<uuid> oder source:<uuid>",
                        verbose_name="Mandant",
                    ),
                ),
                (
                    "body_id",
                    models.UUIDField(blank=True, null=True, verbose_name="Kommune"),
                ),
                (
                    "visibility",
                    models.TextField(
                        choices=[
                            ("oeffentlich", "öffentlich"),
                            ("nichtoeffentlich", "nichtöffentlich"),
                            ("intern", "intern"),
                            ("personenbezogen", "personenbezogen"),
                        ],
                        verbose_name="Sichtbarkeit",
                    ),
                ),
                (
                    "operation",
                    models.TextField(
                        choices=[
                            ("upsert", "angelegt oder geändert"),
                            ("delete", "gelöscht"),
                            ("redact", "unkenntlich gemacht"),
                        ],
                        db_default="upsert",
                        default="upsert",
                        verbose_name="Operation",
                    ),
                ),
                ("occurred_at", models.DateTimeField(verbose_name="geschehen am")),
                (
                    "recorded_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="erfasst am",
                    ),
                ),
                (
                    "actor_ref",
                    models.TextField(
                        blank=True,
                        help_text="user:<uuid> oder system:<job>",
                        null=True,
                        verbose_name="ausgelöst von",
                    ),
                ),
                ("correlation_id", models.UUIDField(verbose_name="Korrelations-ID")),
                (
                    "causation_id",
                    models.UUIDField(blank=True, null=True, verbose_name="Auslöser-ID"),
                ),
                (
                    "payload",
                    models.JSONField(
                        default=dict,
                        help_text="nur Kennungen und Namen geänderter Felder",
                        verbose_name="Nutzlast",
                    ),
                ),
                (
                    "xid",
                    apps.events.fields.TransactionIdField(
                        db_default=apps.events.fields.CurrentTransactionId(),
                        editable=False,
                        verbose_name="Transaktion",
                    ),
                ),
                (
                    "seq",
                    models.BigIntegerField(
                        blank=True,
                        editable=False,
                        null=True,
                        unique=True,
                        verbose_name="Folgenummer",
                    ),
                ),
            ],
            options={
                "verbose_name": "Ereignis",
                "verbose_name_plural": "Ereignisse",
                "db_table": "events_event",
                "indexes": [
                    models.Index(
                        condition=models.Q(("seq__isnull", True)),
                        fields=["xid", "id"],
                        name="events_event_unsequenced",
                    ),
                    models.Index(
                        condition=models.Q(("seq__isnull", False)),
                        fields=["seq", "type"],
                        name="events_event_seq_type",
                    ),
                    models.Index(
                        fields=["aggregate_id", "seq"], name="events_event_aggregate"
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "visibility__in",
                                [
                                    "oeffentlich",
                                    "nichtoeffentlich",
                                    "intern",
                                    "personenbezogen",
                                ],
                            )
                        ),
                        name="events_event_visibility_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("operation__in", ["upsert", "delete", "redact"])
                        ),
                        name="events_event_operation_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("version__gte", 1)),
                        name="events_event_version_positive",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ParkedEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("subscription", models.TextField(verbose_name="Abonnement")),
                ("event_seq", models.BigIntegerField(verbose_name="Folgenummer")),
                ("aggregate_id", models.UUIDField(verbose_name="Objekt-ID")),
                (
                    "state",
                    models.TextField(
                        choices=[
                            ("wiederholen", "wird wiederholt"),
                            ("blockiert", "blockiert (Folgeereignis)"),
                            ("tot", "tot"),
                        ],
                        verbose_name="Zustand",
                    ),
                ),
                (
                    "attempts",
                    models.IntegerField(
                        db_default=0, default=0, verbose_name="Versuche"
                    ),
                ),
                (
                    "next_attempt_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="nächster Versuch"
                    ),
                ),
                (
                    "error_code",
                    models.TextField(
                        blank=True,
                        help_text="Ausnahmeklasse oder fester Code, keine Inhalte",
                        null=True,
                        verbose_name="Fehlercode",
                    ),
                ),
            ],
            options={
                "verbose_name": "geparktes Ereignis",
                "verbose_name_plural": "geparkte Ereignisse",
                "db_table": "events_parked",
                "indexes": [
                    models.Index(
                        fields=["subscription", "aggregate_id"],
                        name="events_parked_aggregate",
                    ),
                    models.Index(
                        condition=models.Q(("state", "wiederholen")),
                        fields=["next_attempt_at"],
                        name="events_parked_due",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("subscription", "event_seq"),
                        name="events_parked_subscription_seq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("state__in", ["wiederholen", "blockiert", "tot"])
                        ),
                        name="events_parked_state_valid",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Subscription",
            fields=[
                (
                    "name",
                    models.TextField(
                        help_text="z. B. suchindex",
                        primary_key=True,
                        serialize=False,
                        verbose_name="Name",
                    ),
                ),
                (
                    "cursor_seq",
                    models.BigIntegerField(
                        db_default=0, default=0, verbose_name="Cursor"
                    ),
                ),
                (
                    "state",
                    models.TextField(
                        choices=[
                            ("aktiv", "aktiv"),
                            ("pausiert", "pausiert"),
                            ("schatten", "Schattenbetrieb"),
                        ],
                        db_default="aktiv",
                        default="aktiv",
                        verbose_name="Zustand",
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True,
                        db_default=django.db.models.functions.datetime.Now(),
                        verbose_name="aktualisiert am",
                    ),
                ),
            ],
            options={
                "verbose_name": "Abonnement",
                "verbose_name_plural": "Abonnements",
                "db_table": "events_subscription",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("state__in", ["aktiv", "pausiert", "schatten"])
                        ),
                        name="events_subscription_state_valid",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="Task",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "queue",
                    models.TextField(
                        help_text="default, mail, index, ocr, ai oder adapter",
                        verbose_name="Warteschlange",
                    ),
                ),
                (
                    "task_path",
                    models.TextField(
                        help_text="Importpfad der Funktion", verbose_name="Auftrag"
                    ),
                ),
                (
                    "args",
                    models.JSONField(
                        default=dict,
                        help_text="nur Kennungen, nie Inhalte",
                        verbose_name="Argumente",
                    ),
                ),
                (
                    "priority",
                    models.SmallIntegerField(
                        db_default=50, default=50, verbose_name="Priorität"
                    ),
                ),
                (
                    "run_after",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        verbose_name="frühestens ab",
                    ),
                ),
                (
                    "status",
                    models.TextField(
                        choices=[
                            ("wartend", "wartend"),
                            ("laeuft", "läuft"),
                            ("erledigt", "erledigt"),
                            ("fehlgeschlagen", "fehlgeschlagen"),
                            ("tot", "tot"),
                        ],
                        db_default="wartend",
                        default="wartend",
                        verbose_name="Status",
                    ),
                ),
                (
                    "attempts",
                    models.IntegerField(
                        db_default=0, default=0, verbose_name="Versuche"
                    ),
                ),
                (
                    "max_attempts",
                    models.IntegerField(
                        db_default=8, default=8, verbose_name="höchstens Versuche"
                    ),
                ),
                (
                    "idempotency_key",
                    models.TextField(
                        blank=True, null=True, verbose_name="Idempotenzschlüssel"
                    ),
                ),
                (
                    "locked_until",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="gesperrt bis"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="angelegt am",
                    ),
                ),
                (
                    "finished_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="beendet am"
                    ),
                ),
                (
                    "result_code",
                    models.TextField(
                        blank=True, null=True, verbose_name="Ergebniscode"
                    ),
                ),
            ],
            options={
                "verbose_name": "Auftrag",
                "verbose_name_plural": "Aufträge",
                "db_table": "events_task",
                "indexes": [
                    models.Index(
                        condition=models.Q(("status", "wartend")),
                        fields=["queue", "run_after"],
                        name="events_task_ready",
                    ),
                    models.Index(
                        condition=models.Q(("status", "laeuft")),
                        fields=["locked_until"],
                        name="events_task_locked",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("idempotency_key",), name="events_task_idempotency_key"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "status__in",
                                [
                                    "wartend",
                                    "laeuft",
                                    "erledigt",
                                    "fehlgeschlagen",
                                    "tot",
                                ],
                            )
                        ),
                        name="events_task_status_valid",
                    ),
                ],
            },
        ),
        migrations.RunPython(postgres_objekte_anlegen, postgres_objekte_entfernen),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benachrichtigungen aus der Datendrehscheibe (Issue #529): Spalte ``event_key`` (Ereignis und Empfänger).

1. Neue, leere Spalte ohne Index (``ADD COLUMN`` mit NULL ist auf PostgreSQL ohne Umschreiben der Tabelle).
2. Eindeutiger Teilindex nur über gesetzte Schlüssel (``WHERE event_key IS NOT NULL``), auf PostgreSQL mit
   ``CREATE UNIQUE INDEX CONCURRENTLY`` (blockiert keine Schreibzugriffe), daher ohne umschließende
   Transaktion. Kein zusätzlicher ``_like``-Index: gesucht wird nur auf Gleichheit. Ein abgebrochener
   ``CONCURRENTLY``-Lauf hinterlässt einen ungültigen Index gleichen Namens; der wird vorher entfernt,
   sonst übersprünge ``IF NOT EXISTS`` ihn für immer (Muster aus ``insight_core/0051``).

Rein additiv: Ein älteres Image kennt die Spalte nicht und legt Benachrichtigungen ohne sie an.
"""

from django.db import migrations, models

INDEX_NAME = "uniq_notification_event_key"
CREATE_SQL = (
    'CREATE UNIQUE INDEX {c} IF NOT EXISTS "uniq_notification_event_key" '
    'ON "work_notification" ("event_key") WHERE "event_key" IS NOT NULL'
)


def _concurrently(schema_editor):
    return "CONCURRENTLY" if schema_editor.connection.vendor == "postgresql" else ""


def _ungueltig(schema_editor, name):
    """Ist ein gleichnamiger Index von einem abgebrochenen ``CONCURRENTLY``-Lauf übrig (nur PostgreSQL)?"""
    if schema_editor.connection.vendor != "postgresql":
        return False
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT NOT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = %s AND pg_catalog.pg_table_is_visible(c.oid)",
            [name],
        )
        zeile = cursor.fetchone()
    return bool(zeile and zeile[0])


def index_anlegen(apps, schema_editor):
    c = _concurrently(schema_editor)
    if _ungueltig(schema_editor, INDEX_NAME):
        schema_editor.execute(f'DROP INDEX {c} IF EXISTS "{INDEX_NAME}"')
    schema_editor.execute(CREATE_SQL.format(c=c))


def index_entfernen(apps, schema_editor):
    schema_editor.execute(f'DROP INDEX {_concurrently(schema_editor)} IF EXISTS "{INDEX_NAME}"')


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("work", "0067_ris_verweise_ohne_kaskade"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="event_key",
            field=models.CharField(
                blank=True, editable=False, max_length=120, null=True, verbose_name="Ereignis und Empfänger"
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddConstraint(
                    model_name="notification",
                    constraint=models.UniqueConstraint(
                        condition=models.Q(("event_key__isnull", False)),
                        fields=("event_key",),
                        name=INDEX_NAME,
                    ),
                ),
            ],
            database_operations=[migrations.RunPython(index_anlegen, index_entfernen)],
        ),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Indizes für die Abhängigkeiten des Suchindex-Abonnements (Issue #821).

Das Abonnement ``suchindex`` sucht zu einer geänderten Sitzung bzw. einem geänderten Tagesordnungspunkt
die Beratungen, die auf sie verweisen (``meeting_external_id``, ``agenda_item_external_id``), um die
Dateien der dort beratenen Vorgänge neu zu bauen. Ohne Index liest jede solche Abfrage die ganze
Tabelle der Beratungen.

Auf PostgreSQL entstehen die Indizes mit ``CREATE INDEX CONCURRENTLY``: Das sperrt keine Schreibzugriffe
des Ingestors, auch nicht auf großen Beständen. Deshalb läuft die Migration ohne umschließende
Transaktion (``atomic = False``), wie ``session/0032_protokoll_indizes``. ``IF NOT EXISTS``: Hat der
Betrieb einen Index vorab angelegt, ist der Schritt ein No-op. Ein abgebrochener ``CONCURRENTLY``-Lauf
hinterlässt einen ungültigen Index gleichen Namens; der wird vorher entfernt, sonst übersprünge ``IF NOT
EXISTS`` ihn für immer. Rückweg: ``DROP INDEX CONCURRENTLY IF EXISTS``. Rein additiv; ein älteres Image läuft
mit den Indizes unverändert.
"""

from django.db import migrations, models

#: (Name, Spalte) je Index auf ``oparl_consultations``
INDEXES = (
    ("oparl_cons_meeting_ext", "meeting_external_id"),
    ("oparl_cons_agenda_ext", "agenda_item_external_id"),
)
TABLE = "oparl_consultations"


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


def indizes_anlegen(apps, schema_editor):
    c = _concurrently(schema_editor)
    for name, spalte in INDEXES:
        if _ungueltig(schema_editor, name):
            schema_editor.execute(f'DROP INDEX {c} IF EXISTS "{name}"')
        schema_editor.execute(f'CREATE INDEX {c} IF NOT EXISTS "{name}" ON "{TABLE}" ("{spalte}")')


def indizes_entfernen(apps, schema_editor):
    for name, _spalte in reversed(INDEXES):
        schema_editor.execute(f'DROP INDEX {_concurrently(schema_editor)} IF EXISTS "{name}"')


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("insight_core", "0050_loeschgrund"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddIndex(
                    model_name="oparlconsultation",
                    index=models.Index(fields=["meeting_external_id"], name="oparl_cons_meeting_ext"),
                ),
                migrations.AddIndex(
                    model_name="oparlconsultation",
                    index=models.Index(fields=["agenda_item_external_id"], name="oparl_cons_agenda_ext"),
                ),
            ],
            database_operations=[migrations.RunPython(indizes_anlegen, indizes_entfernen)],
        ),
    ]

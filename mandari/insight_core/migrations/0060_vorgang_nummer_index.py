# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ausdrucksindex für die Suche nach Vorlagennummern in jeder Schreibweise (Issue #1102).

Die Suche vergleicht den Vergleichsschlüssel der Nummer (``hub.ris.vorlagennummer.SQL_SCHLUESSEL``: Großbuchstaben,
Gruppen mit ``/`` verbunden, ohne führende Nullen) per ``LIKE`` und regulärem Ausdruck. Ein Trigramm-Index (``pg_trgm``,
seit ``0046_kommunenverzeichnis`` vorhanden) auf genau diesem Ausdruck findet die passenden Vorgänge, ohne den Schlüssel
für jede Zeile einer Kommune neu zu rechnen. Er deckt Anfang (``V/591/2026%``), Endstück und Mitte (``%591/2026%``) ab.

Keine neue Spalte und keine Datenmigration: Der Bestand wird nicht umgeschrieben, der Ingestor schreibt weiter wie
bisher, PostgreSQL pflegt den Index selbst. Angelegt wird er mit ``CREATE INDEX CONCURRENTLY`` ohne umschließende
Transaktion (``atomic = False``, wie ``0051_beratung_kontext_indizes``), das sperrt keine Schreibzugriffe. Ein
abgebrochener Lauf hinterlässt einen ungültigen Index gleichen Namens; der wird vorher entfernt. ``IF NOT EXISTS``: Hat
der Betrieb den Index vorab angelegt, ist der Schritt ein No-op. Nur PostgreSQL: SQLite (Tests, Entwicklung) rechnet
den Schlüssel ohne Index, deshalb steht der Index nicht im Modellzustand. Rückweg: ``DROP INDEX CONCURRENTLY``.
"""

from django.db import migrations

INDEX = "oparl_paper_nummer_trgm"
TABLE = "oparl_papers"
#: Stand von ``hub.ris.vorlagennummer.SQL_SCHLUESSEL`` beim Anlegen. Der Index hilft nur, solange die Abfrage genau
#: diesen Ausdruck rechnet; ``hub/ris/tests/test_vorlagennummer.py`` prüft das (ändert er sich, braucht es einen neuen
#: Index in einer neuen Migration).
SQL_SCHLUESSEL = (
    "btrim("
    + "regexp_replace(" * 5
    + "translate(%(expressions)s, 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'), "
    + r"'[^0-9A-Z]+', '/', 'g'), "
    + r"'([A-Z])/(?=[A-Z])', '\1', 'g'), "
    + r"'([A-Z])([0-9])', '\1/\2', 'g'), "
    + r"'([0-9])([A-Z])', '\1/\2', 'g'), "
    + r"'(^|/)0+([0-9])', '\1\2', 'g'), "
    + "'/')"
)


def _ungueltig(schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT NOT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = %s AND pg_catalog.pg_table_is_visible(c.oid)",
            [INDEX],
        )
        zeile = cursor.fetchone()
    return bool(zeile and zeile[0])


def index_anlegen(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    if _ungueltig(schema_editor):
        schema_editor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{INDEX}"')
    ausdruck = SQL_SCHLUESSEL % {"expressions": '"reference"'}
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    schema_editor.execute(
        f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{INDEX}" ON "{TABLE}" USING gin (({ausdruck}) gin_trgm_ops)'
    )


def index_entfernen(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{INDEX}"')


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("insight_core", "0059_dokumentkette_abruf"),
    ]

    operations = [
        migrations.RunPython(index_anlegen, index_entfernen),
    ]

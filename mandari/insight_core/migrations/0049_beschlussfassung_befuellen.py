# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussfassung im RIS-Bestand aus den bisher nur in ``raw_json`` gespeicherten Erweiterungen befüllen (Issue #525).

Tagesordnungspunkte aus mandari Session tragen ``mandari:vote``, ``mandari:rollCall`` und
``mandari:resolutionNumber`` schon in ``raw_json``; die neuen Spalten (0048) übernehmen sie mit derselben
Übersetzung wie Ingestor und Spiegel (``mandari_oparl.extensions.agenda_item_columns``). Sitzungen tragen
``mandari:protocolApproval`` erst ab diesem Stand, daher nur Tagesordnungspunkte.

- Eigene Migration ohne umschließende Transaktion: Die Spalten sind schon angelegt (0048), das Befüllen hält
  keine Tabellensperre, sondern schreibt in kleinen Stapeln.
- Idempotent (dieselbe Eingabe ergibt dieselben Spalten) und ``elidable``: Auf einer neuen Installation gibt es
  nichts zu befüllen, beim nächsten Abgleich schreiben Ingestor und Spiegel die Spalten ohnehin.
- Rückwärts nichts: 0048 rückwärts entfernt die Spalten.
"""

from django.db import migrations

#: Schlüssel in ``raw_json``, die eine Beschlussfassung tragen
KEYS = ["mandari:vote", "mandari:rollCall", "mandari:resolutionNumber", "mandari:implementation"]
BATCH = 500


def befuellen(apps, schema_editor):
    from mandari_oparl.extensions import AGENDA_ITEM_COLUMNS, agenda_item_columns

    agenda_item = apps.get_model("insight_core", "OParlAgendaItem")
    # Erst die Kennungen (wenige Zeilen: nur Session-Mandanten liefern die Erweiterungen), dann stapelweise
    # ohne serverseitigen Cursor (PgBouncer im Transaktionsmodus)
    ids = list(agenda_item.objects.filter(raw_json__has_any_keys=KEYS).values_list("pk", flat=True))
    for start in range(0, len(ids), BATCH):
        items = list(agenda_item.objects.filter(pk__in=ids[start : start + BATCH]).only("pk", "raw_json"))
        for item in items:
            for name, value in agenda_item_columns(item.raw_json or {}).items():
                setattr(item, name, value)
        agenda_item.objects.bulk_update(items, AGENDA_ITEM_COLUMNS)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("insight_core", "0048_beschlussfassung"),
    ]

    operations = [
        migrations.RunPython(befuellen, migrations.RunPython.noop, elidable=True),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesordnungspunkte der Fraktionssitzungen: Der Teil der Tagesordnung (``FactionAgendaItem.visibility``) kennt nur
„öffentlich“ (``public``) und „nichtöffentlich“ (``internal``). Bis zu dieser Version nahm das Eintragen und
Vorschlagen eines TOPs auch andere Werte an und speicherte sie; das Modell weist sie jetzt beim Speichern ab.

Bestehende TOPs mit einem anderen Wert werden nichtöffentlich: Ein solcher TOP stand bisher in keinem der beiden
Teile, und im Zweifel soll nichts sichtbarer werden als gedacht. Geändert wird nur diese eine Spalte solcher Zeilen,
in einer ``UPDATE``-Anweisung ohne Schleife und ohne Änderungshistorie; in aller Regel trifft sie keine Zeile.

Rückwärts geschieht nichts: Der frühere Wert ist nicht mehr bekannt und war nie gültig.
"""

from django.db import migrations

TEILE = ("public", "internal")


def bereinigen(apps, schema_editor):
    agenda_item_model = apps.get_model("work", "FactionAgendaItem")
    agenda_item_model.objects.exclude(visibility__in=TEILE).update(visibility="internal")


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0087_dokument_meta_version"),
    ]

    operations = [
        migrations.RunPython(bereinigen, migrations.RunPython.noop),
    ]

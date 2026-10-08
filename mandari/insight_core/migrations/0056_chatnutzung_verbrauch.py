# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verbrauch des KI-Assistenten je Antwort (Issue #899): Eingabe- und Ausgabe-Token sowie Modellaufrufe.

Additiv mit Vorgabe in der Datenbank (``db_default=0``): Ein älteres Image kennt die Spalten nicht und legt
weiter Zeilen an; vorhandene Zeilen behalten ``tokens_used`` und erhalten 0 in den neuen Spalten.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0054_person_fraktion_beleg"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatusage",
            name="completion_tokens",
            field=models.IntegerField(db_default=0, default=0, verbose_name="Ausgabe-Token"),
        ),
        migrations.AddField(
            model_name="chatusage",
            name="prompt_tokens",
            field=models.IntegerField(db_default=0, default=0, verbose_name="Eingabe-Token"),
        ),
        migrations.AddField(
            model_name="chatusage",
            name="rounds",
            field=models.PositiveSmallIntegerField(db_default=0, default=0, verbose_name="Modellaufrufe"),
        ),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsvorschläge der KI kennzeichnen (Issue #1584).

Eine neue Spalte ``ki_erzeugt`` (Vorgabe ``False``) an den Kommentaren eines Dokuments: Der KI-Assistent legt seine
Ergebnisse als Änderungsvorschlag an, statt den Text zu überschreiben, und der Rand zeigt sie als KI-Vorschlag.
Rein additiv; das Image davor kennt die Spalte nicht und läuft unverändert weiter. Bestehende Kommentare bleiben,
wie sie sind.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0093_terminumfrage_sitzung"),
    ]

    operations = [
        migrations.AddField(
            model_name="motioncomment",
            name="ki_erzeugt",
            field=models.BooleanField(default=False, verbose_name="Von der KI vorgeschlagen"),
        ),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sicherheitsprotokoll: Ereignis „Eingriff in den Betrieb“ (Issue #510).

Nur die Auswahlliste ändert sich, nicht die Spalte; ältere Images lesen die neuen Einträge weiter.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_geraet_merken_und_ruecksetztoken_nur_aus_dem_code"),
    ]

    operations = [
        migrations.AlterField(
            model_name="securityauditlog",
            name="event",
            field=models.CharField(
                choices=[
                    ("login", "Anmeldung"),
                    ("logout", "Abmeldung"),
                    ("login_failed", "Anmeldung fehlgeschlagen"),
                    ("betrieb", "Eingriff in den Betrieb"),
                ],
                max_length=30,
                verbose_name="Ereignis",
            ),
        ),
    ]

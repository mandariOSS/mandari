# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sicherheitsprotokoll: Ereignis „Anmeldung durch Drosselung abgewiesen“ (Issue #366).

Nur die Auswahlliste ändert sich, nicht die Spalte; ältere Images lesen die neuen Einträge weiter.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0007_sicherheitsprotokoll_betrieb"),
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
                    ("login_throttled", "Anmeldung durch Drosselung abgewiesen"),
                    ("betrieb", "Eingriff in den Betrieb"),
                ],
                max_length=30,
                verbose_name="Ereignis",
            ),
        ),
    ]

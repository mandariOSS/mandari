# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Herkunft einer Aufgabe aus einem Protokolleintrag der Fraktionssitzung (Issue #459).

Der Aufgabenimport erkennt übernommene Einträge daran und bietet sie nicht erneut an. Neue, leere
Spalte – abwärtskompatibel (ältere Images ignorieren sie).
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("work", "0060_benachrichtigungsarten_zuruecksetzen"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="related_protocol_entry",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_tasks",
                to="work.factionprotocolentry",
                verbose_name="Protokolleintrag",
            ),
        ),
    ]

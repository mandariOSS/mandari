# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benachrichtigungen aus der Datendrehscheibe (Issue #529): Spalte ``event_key`` (Ereignis und Empfänger).

Rein additiv: neue, leere Spalte mit eindeutigem Index (NULL zählt nicht). Ein älteres Image kennt sie
nicht und legt Benachrichtigungen wie bisher ohne sie an.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("work", "0067_ris_verweise_ohne_kaskade"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="event_key",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=120,
                null=True,
                unique=True,
                verbose_name="Ereignis und Empfänger",
            ),
        ),
    ]

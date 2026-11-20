# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dateien direkt am Tagesordnungspunkt (Issue #1528): ``OParlFile.agenda_item``.

OParl erlaubt Dateien am AgendaItem (``auxiliaryFile``, ``resolutionFile``), etwa Anträge ohne Vorlage. Der Ingestor
schreibt sie mit Bezug zum Punkt und zu seiner Sitzung. Nullable ohne Standardwert: Bestehende Zeilen bleiben, wie sie
sind; ältere Ingestor-Images kennen die Spalte nicht und schreiben weiter ohne sie.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0073_vorgang_finanzcheck"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlfile",
            name="agenda_item",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="files",
                to="insight_core.oparlagendaitem",
                verbose_name="Tagesordnungspunkt",
            ),
        ),
    ]

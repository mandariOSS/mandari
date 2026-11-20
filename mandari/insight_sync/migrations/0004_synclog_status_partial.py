# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zustand „Unvollständig“ (``partial``) im Sync-Protokoll (Issue #1523): Der Ingestor schreibt ihn für einen
Vollabgleich, der nicht alle Listen ganz gelesen hat (Zeitüberschreitung, Schutzschalter, Fensterende). Nur die
Auswahl ändert sich, keine Spalte.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_sync", "0003_syncconfig_entfernen"),
    ]

    operations = [
        migrations.AlterField(
            model_name="synclog",
            name="status",
            field=models.CharField(
                choices=[
                    ("running", "Läuft"),
                    ("success", "Erfolgreich"),
                    ("partial", "Unvollständig"),
                    ("failed", "Fehlgeschlagen"),
                ],
                default="running",
                max_length=20,
                verbose_name="Status",
            ),
        ),
    ]

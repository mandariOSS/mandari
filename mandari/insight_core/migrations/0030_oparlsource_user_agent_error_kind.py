# SPDX-License-Identifier: AGPL-3.0-or-later
"""User-Agent je Quelle und Fehlerklasse des letzten Fehlers (Issue #123).

Beide Spalten sind nullable: Der Ingestor liest bzw. schreibt sie über sein
SQLAlchemy-Modell (``ingestor/src/storage/models.py``), ein älterer Ingestor
kommt ohne sie aus.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0029_portal_indizes"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlsource",
            name="user_agent",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Leer = Standard des Ingestors (mandari-ingestor/<Version> mit Kontaktadresse). "
                    "Nur setzen, wenn mit dem Betreiber der Quelle ein bestimmter Wert vereinbart ist."
                ),
                max_length=255,
                null=True,
                verbose_name="User-Agent",
            ),
        ),
        migrations.AddField(
            model_name="oparlsource",
            name="last_error_kind",
            field=models.CharField(
                blank=True,
                choices=[("ua_blocked", "User-Agent gesperrt"), ("server_error_series", "5xx-Serie")],
                max_length=40,
                null=True,
                verbose_name="Fehlerklasse",
            ),
        ),
    ]

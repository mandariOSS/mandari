# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Markierung abgeleiteter Zuordnungen Beratung → Tagesordnungspunkt (Issue #1578).

ALLRIS liefert Sitzung und Tagesordnungspunkt einer Beratung erst nach der Sitzung; bis dahin ordnet der Ingestor sie
über den Titel der Vorlage zu und markiert das. Eine neue Spalte mit festem Standardwert (``db_default``): PostgreSQL
legt sie ohne Umschreiben der Tabelle an, Bestandszeilen gelten als nicht abgeleitet. Rückweg: Spalte entfernen.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0075_wahlperioden_geteilt"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlconsultation",
            name="agenda_item_inferred",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Über den Titel der Vorlage zugeordnet, bis die Quelle Sitzung und Tagesordnungspunkt nennt.",
                verbose_name="Tagesordnungspunkt abgeleitet",
            ),
        ),
    ]

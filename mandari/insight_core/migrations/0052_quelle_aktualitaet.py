# SPDX-License-Identifier: AGPL-3.0-or-later
"""Aktualität je Quelle (Issue #556): letzter vollständig erfolgreicher Abgleich bzw. Vollabgleich.

Zwei leere Zeitstempel an ``OParlSource``; der Ingestor setzt sie nach einem Abgleich ohne Lücke. Additiv:
Ein älteres Image (Anwendung oder Ingestor) kennt die Spalten nicht und lässt sie unberührt.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0051_beratung_kontext_indizes"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlsource",
            name="last_successful_full_sync",
            field=models.DateTimeField(
                blank=True,
                help_text="Letzter Vollabgleich ohne Lücke. Nur danach darf ein Scraper-Abgleich auf Löschungen schließen.",
                null=True,
                verbose_name="Letzter vollständiger Vollabgleich",
            ),
        ),
        migrations.AddField(
            model_name="oparlsource",
            name="last_successful_sync",
            field=models.DateTimeField(
                blank=True,
                help_text="Letzter Abgleich ohne Lücke: jede Kommune und jede Liste ganz gelesen.",
                null=True,
                verbose_name="Letzter vollständiger Abgleich",
            ),
        ),
    ]

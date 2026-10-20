# SPDX-License-Identifier: AGPL-3.0-or-later
"""Texterkennung in zwei Stufen (Issue #1107): Vermerk „Textebene reicht nicht“ je Inhalt.

Eine nullbare Spalte ohne Standard, kein Umschreiben des Bestands: PostgreSQL legt sie ohne Tabellenumbau an. Der
Ingestor kennt sie (``ingestor/src/storage/models.py``) und schreibt sie nie (``ENRICHMENT_FIELDS``); ältere Images
lassen sie leer stehen. Rückweg: Spalte entfernen (ohne Folgen, sie ist nur ein Hinweis für das Einplanen).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0060_vorgang_nummer_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlfile",
            name="text_ocr_needed_sha256",
            field=models.CharField(
                blank=True, max_length=64, null=True, verbose_name="Textebene reicht nicht (Inhalt, SHA-256)"
            ),
        ),
    ]

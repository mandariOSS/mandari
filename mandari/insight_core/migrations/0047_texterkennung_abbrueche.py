# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abbrüche der Texterkennung (Issue #817): Zähler begonnener, nie beendeter Bearbeitungen und Beginn der
laufenden Bearbeitung je Datei.

Abwärtskompatibel: Der Zähler hat einen Standard in der Datenbank (0; in PostgreSQL ohne Umschreiben der
Tabelle, als ganze Zahl ohne CHECK-Bedingung, die alle Zeilen prüfen müsste), der Beginn ist nullbar.
Ingestor-INSERTs und ein älteres Image kennen die Spalten nicht und schreiben sie nie. Keine Datenmigration:
Bestehende Dateien in "processing" ohne Beginn löst der OCR-Worker nach der Zeitgrenze anhand der letzten
Änderung auf.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("insight_core", "0046_kommunenverzeichnis"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlfile",
            name="text_extraction_attempts",
            field=models.IntegerField(
                db_default=0,
                default=0,
                help_text="Begonnene, nie beendete Bearbeitungen der Texterkennung (Worker beendet)",
                verbose_name="Abgebrochene Versuche",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="text_extraction_started_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Beginn der laufenden Bearbeitung der Texterkennung",
                null=True,
                verbose_name="Bearbeitung seit",
            ),
        ),
    ]

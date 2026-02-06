"""
Migration: Text Extraction Tracking & SEO Fields

Adds:
- OParlFile: text_extraction_status, text_extraction_method, text_extraction_error,
  text_extracted_at, page_count
- OParlBody: slug field for SEO-friendly URLs
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0010_add_contact_request"),
    ]

    operations = [
        # Add text extraction tracking fields to OParlFile
        migrations.AddField(
            model_name="oparlfile",
            name="text_extraction_status",
            field=models.CharField(
                choices=[
                    ("pending", "Ausstehend"),
                    ("processing", "In Bearbeitung"),
                    ("completed", "Abgeschlossen"),
                    ("failed", "Fehlgeschlagen"),
                    ("skipped", "Übersprungen"),
                ],
                db_index=True,
                default="pending",
                help_text="Status der Textextraktion",
                max_length=20,
                verbose_name="Extraktionsstatus",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="text_extraction_method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("pypdf", "pypdf (Text-PDF)"),
                    ("mistral", "Mistral OCR (API)"),
                    ("tesseract", "Tesseract OCR (lokal)"),
                    ("none", "Keine Extraktion"),
                ],
                help_text="Methode die für die Textextraktion verwendet wurde",
                max_length=20,
                null=True,
                verbose_name="Extraktionsmethode",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="text_extraction_error",
            field=models.TextField(
                blank=True,
                help_text="Fehlermeldung falls Extraktion fehlschlug",
                null=True,
                verbose_name="Extraktionsfehler",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="text_extracted_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Zeitpunkt der letzten Textextraktion",
                null=True,
                verbose_name="Extrahiert am",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="page_count",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Anzahl der Seiten (bei PDFs)",
                null=True,
                verbose_name="Seitenanzahl",
            ),
        ),
        # Add slug field to OParlBody for SEO
        migrations.AddField(
            model_name="oparlbody",
            name="slug",
            field=models.SlugField(
                blank=True,
                help_text="URL-freundlicher Identifikator (z.B. 'muenster' für Münster)",
                max_length=100,
                null=True,
                unique=True,
            ),
        ),
    ]

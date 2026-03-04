"""
Georeferenzierung: Tracking-Felder für automatische Ortsextraktion aus Papers.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("insight_core", "0017_bookmark"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlpaper",
            name="georef_status",
            field=models.CharField(
                choices=[
                    ("pending", "Ausstehend"),
                    ("processing", "In Bearbeitung"),
                    ("completed", "Abgeschlossen"),
                    ("ai_needed", "KI-Extraktion benötigt"),
                    ("no_locations", "Keine Ortsbezüge"),
                    ("failed", "Fehlgeschlagen"),
                    ("skipped", "Übersprungen"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
                verbose_name="Georef-Status",
            ),
        ),
        migrations.AddField(
            model_name="oparlpaper",
            name="georef_method",
            field=models.CharField(
                blank=True,
                help_text="regex, ai, regex+ai, manual",
                max_length=20,
                null=True,
                verbose_name="Georef-Methode",
            ),
        ),
        migrations.AddField(
            model_name="oparlpaper",
            name="georef_error",
            field=models.TextField(
                blank=True,
                null=True,
                verbose_name="Georef-Fehler",
            ),
        ),
        migrations.AddField(
            model_name="oparlpaper",
            name="georef_extracted_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Georef-Zeitpunkt",
            ),
        ),
    ]

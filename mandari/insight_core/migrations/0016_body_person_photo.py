"""Add person photo configuration fields to OParlBody."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("insight_core", "0015_chat_usage"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlbody",
            name="person_photo_url_template",
            field=models.CharField(
                blank=True,
                help_text=(
                    "URL-Template für Personenfotos. Verwende {id} als Platzhalter für die Person-ID. "
                    "Beispiel: https://www.stadt-muenster.de/sessionnet/sessionnetbi/im/pe{id}.jpg"
                ),
                max_length=500,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="oparlbody",
            name="person_photo_id_pattern",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Regex-Pattern zum Extrahieren der ID aus der external_id der Person. "
                    "Muss eine Capture-Group enthalten. "
                    r"Beispiel: /people/(\d+)$"
                ),
                max_length=255,
                null=True,
            ),
        ),
    ]

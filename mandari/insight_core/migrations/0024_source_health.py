# SPDX-License-Identifier: AGPL-3.0-or-later
"""Betriebsmonitor: Fehlerstatus je OParl-Quelle (vom Ingestor geschrieben) + Alarm-Zeitstempel."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0023_person_photos_question_topics"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlsource",
            name="last_error",
            field=models.TextField(blank=True, null=True, verbose_name="Letzter Fehler"),
        ),
        migrations.AddField(
            model_name="oparlsource",
            name="last_error_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Letzter Fehler am"),
        ),
        migrations.AddField(
            model_name="oparlsource",
            name="consecutive_failures",
            field=models.PositiveIntegerField(default=0, verbose_name="Fehlversuche in Folge"),
        ),
        migrations.AddField(
            model_name="oparlsource",
            name="health_alert_sent_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Alarm gesendet am"),
        ),
    ]

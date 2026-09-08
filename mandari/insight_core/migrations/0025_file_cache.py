# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lokaler Dokument-Cache: Status je Datei (Issue #87/#86)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0024_source_health"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlfile",
            name="local_status",
            field=models.CharField(
                choices=[
                    ("none", "Nicht zwischengespeichert"),
                    ("ok", "Lokal vorhanden"),
                    ("missing", "Quelle liefert 404"),
                    ("error", "Fehler beim Abruf"),
                    ("too_large", "Zu groß für den Cache"),
                ],
                db_index=True,
                default="none",
                max_length=20,
                verbose_name="Lokale Kopie",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="local_cached_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Lokal gespeichert am"),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="local_error",
            field=models.CharField(blank=True, default="", max_length=500, verbose_name="Cache-Fehler"),
        ),
    ]

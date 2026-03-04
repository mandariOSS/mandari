"""
Bookmark model for Merkliste feature.
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("insight_core", "0016_body_person_photo"),
    ]

    operations = [
        migrations.CreateModel(
            name="Bookmark",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "entity_type",
                    models.CharField(
                        choices=[
                            ("person", "Person"),
                            ("paper", "Vorgang"),
                            ("meeting", "Sitzung"),
                            ("organization", "Gremium"),
                        ],
                        max_length=20,
                    ),
                ),
                ("entity_id", models.UUIDField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bookmarks",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Merkliste-Eintrag",
                "verbose_name_plural": "Merkliste-Einträge",
                "db_table": "insight_bookmarks",
                "ordering": ["-created_at"],
                "unique_together": {("user", "entity_type", "entity_id")},
            },
        ),
    ]

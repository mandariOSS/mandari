# SPDX-License-Identifier: AGPL-3.0-or-later

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("insight_core", "0014_hero_image_for_body"),
    ]

    operations = [
        migrations.CreateModel(
            name="ChatUsage",
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
                    "session_key",
                    models.CharField(db_index=True, max_length=40),
                ),
                (
                    "ip_address",
                    models.GenericIPAddressField(db_index=True),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="chat_usage",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "message",
                    models.TextField(verbose_name="Nachricht"),
                ),
                (
                    "filter_result",
                    models.CharField(
                        choices=[
                            ("passed", "Bestanden"),
                            ("pii_blocked", "PII blockiert"),
                            ("spam_blocked", "Spam blockiert"),
                            ("injection_blocked", "Injection blockiert"),
                        ],
                        default="passed",
                        max_length=20,
                        verbose_name="Filter-Ergebnis",
                    ),
                ),
                (
                    "tokens_used",
                    models.IntegerField(default=0, verbose_name="Tokens verbraucht"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, db_index=True),
                ),
            ],
            options={
                "db_table": "chat_usage",
                "verbose_name": "Chat-Nutzung",
                "verbose_name_plural": "Chat-Nutzungen",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["ip_address", "created_at"],
                        name="chat_usage_ip_address_creat_idx",
                    ),
                    models.Index(
                        fields=["session_key", "created_at"],
                        name="chat_usage_session_key_crea_idx",
                    ),
                ],
            },
        ),
    ]

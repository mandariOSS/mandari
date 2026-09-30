# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Idempotenzspeicher für Befehle (Issue #539, ``docs/adr/20260929-befehle-synchron.md``).

Nur eine neue Tabelle; älterer Code kennt sie nicht. Ein Rückfall auf ein älteres Image braucht
keinen Rückbau dieser Migration.
"""

import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0003_schedule"),
    ]

    operations = [
        migrations.CreateModel(
            name="IdempotencyKey",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "scope",
                    models.TextField(
                        help_text="Mandant und Auslöser, z. B. session:<uuid> user:<uuid>",
                        verbose_name="Bereich",
                    ),
                ),
                ("key", models.TextField(verbose_name="Schlüssel")),
                (
                    "label",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="z. B. Name des Befehls, für Betrieb und Auswertung",
                        verbose_name="Art",
                    ),
                ),
                (
                    "request_hash",
                    models.CharField(max_length=64, verbose_name="Hash der Anfrage"),
                ),
                (
                    "response",
                    models.JSONField(
                        default=dict,
                        help_text="nur Kennungen und Codes, nie Inhalte",
                        verbose_name="Antwort",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="angelegt am",
                    ),
                ),
            ],
            options={
                "verbose_name": "Idempotenzschlüssel",
                "verbose_name_plural": "Idempotenzschlüssel",
                "db_table": "events_idempotency",
                "indexes": [
                    models.Index(
                        fields=["created_at"], name="events_idempotency_created"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("scope", "key"), name="events_idempotency_scope_key"
                    )
                ],
            },
        ),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lebenszeichen der Worker-Prozesse (Issue #508): Tabelle ``events_worker``.

``manage.py events_worker`` erneuert seine Zeile, solange alle seine Rollen arbeiten, und löscht sie
beim Beenden (``apps.events.presence``). Nur eine neue Tabelle; ein Rückfall auf ein älteres Image
braucht keinen Rückbau.
"""

import django.db.models.functions.datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0005_aenderungsfeed"),
    ]

    operations = [
        migrations.CreateModel(
            name="WorkerProcess",
            fields=[
                (
                    "holder",
                    models.TextField(
                        help_text="Rechnername, Prozessnummer, Zufallsanteil",
                        primary_key=True,
                        serialize=False,
                        verbose_name="Prozess",
                    ),
                ),
                ("roles", models.JSONField(default=list, verbose_name="Rollen")),
                (
                    "queues",
                    models.JSONField(
                        default=list,
                        help_text="leer = alle",
                        verbose_name="Warteschlangen",
                    ),
                ),
                (
                    "started_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        editable=False,
                        verbose_name="gestartet am",
                    ),
                ),
                (
                    "seen_at",
                    models.DateTimeField(
                        db_default=django.db.models.functions.datetime.Now(),
                        verbose_name="zuletzt gemeldet",
                    ),
                ),
            ],
            options={
                "verbose_name": "Worker-Prozess",
                "verbose_name_plural": "Worker-Prozesse",
                "db_table": "events_worker",
            },
        ),
    ]

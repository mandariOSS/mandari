# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Standard-Tagesordnung je Organisation (Issue #872).

1. Neue Tabelle ``work_factionstandardagendaitem`` (leer angelegt).
2. Neue, leere Spalte ``standard_item_id`` an ``work_factionagendaitem`` (``ADD COLUMN`` mit NULL, ohne Umschreiben
   der Tabelle; der Index ist bei der kleinen TOP-Tabelle unkritisch).

Rein additiv, keine Datenänderung: Ein älteres Image kennt Tabelle und Spalte nicht und legt TOPs ohne sie an.
Wird ein Standard-TOP entfernt, behalten bereits angelegte Sitzungen ihre TOPs (``SET_NULL``).
"""

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0025_recht_dokumente_ehemaliger_mitglieder"),
        ("work", "0069_ris_anker"),
    ]

    operations = [
        migrations.CreateModel(
            name="FactionStandardAgendaItem",
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
                ("title", models.CharField(max_length=500, verbose_name="Titel")),
                (
                    "visibility",
                    models.CharField(
                        choices=[
                            ("public", "Öffentlich"),
                            ("internal", "Nicht-öffentlich"),
                        ],
                        default="public",
                        max_length=20,
                        verbose_name="Sichtbarkeit",
                    ),
                ),
                (
                    "order",
                    models.PositiveIntegerField(default=0, verbose_name="Reihenfolge"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="faction_standard_agenda_items",
                        to="tenants.organization",
                        verbose_name="Organisation",
                    ),
                ),
            ],
            options={
                "verbose_name": "Standard-TOP",
                "verbose_name_plural": "Standard-Tagesordnung",
                "ordering": ["order", "created_at"],
            },
        ),
        migrations.AddField(
            model_name="factionagendaitem",
            name="standard_item",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="meeting_items",
                to="work.factionstandardagendaitem",
                verbose_name="Aus Standard-TOP",
            ),
        ),
    ]

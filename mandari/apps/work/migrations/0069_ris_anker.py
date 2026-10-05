# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fachliche Anker der Verknüpfungen mit dem RIS-Bestand und Protokoll der Neuzuordnungen, je Organisation (Issue #547).

Zwei neue Tabellen, keine Änderung an bestehenden: Ein älteres Image läuft ohne Rückbau weiter (es kennt die
Tabellen nur nicht). Die Kennung des RIS-Objekts ist bewusst kein Fremdschlüssel (``apps/work/ris/models.py``).
"""

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0025_recht_dokumente_ehemaliger_mitglieder"),
        ("work", "0068_benachrichtigung_aus_ereignis"),
    ]

    operations = [
        migrations.CreateModel(
            name="RisAnker",
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
                    "art",
                    models.CharField(
                        choices=[("top", "Tagesordnungspunkt"), ("vorlage", "Vorlage")],
                        max_length=10,
                        verbose_name="Art",
                    ),
                ),
                (
                    "objekt",
                    models.UUIDField(
                        help_text="Kennung im RIS-Bestand, an der die Work-Daten heute hängen",
                        verbose_name="RIS-Objekt",
                    ),
                ),
                (
                    "kennung",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Leer, bis der Abgleich sie erstmals erfasst",
                        verbose_name="Fachliche Kennung",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("aktuell", "Aktuell"),
                            (
                                "nicht_zugeordnet",
                                "Nicht zugeordnet (anderer Punkt unter derselben Kennung)",
                            ),
                            (
                                "entfallen",
                                "Entfallen (gelöscht oder nicht mehr auf der Tagesordnung)",
                            ),
                            ("mehrdeutig", "Mehrdeutig (mehrere mögliche Nachfolger)"),
                        ],
                        default="aktuell",
                        max_length=20,
                        verbose_name="Status",
                    ),
                ),
                (
                    "geprueft_am",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Zuletzt geprüft"
                    ),
                ),
                (
                    "status_seit",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Status seit"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ris_anker",
                        to="tenants.organization",
                        verbose_name="Organisation",
                    ),
                ),
            ],
            options={
                "verbose_name": "RIS-Anker",
                "verbose_name_plural": "RIS-Anker",
                "indexes": [
                    models.Index(
                        fields=["art", "objekt"], name="work_risanker_objekt_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "art", "objekt"),
                        name="work_risanker_org_art_objekt_eindeutig",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="RisNeuzuordnung",
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
                    "art",
                    models.CharField(
                        choices=[("top", "Tagesordnungspunkt"), ("vorlage", "Vorlage")],
                        max_length=10,
                        verbose_name="Art",
                    ),
                ),
                ("von", models.UUIDField(verbose_name="Bisheriges RIS-Objekt")),
                (
                    "nach",
                    models.UUIDField(
                        blank=True, null=True, verbose_name="Neues RIS-Objekt"
                    ),
                ),
                ("ergebnis", models.CharField(max_length=20, verbose_name="Ergebnis")),
                (
                    "verschoben",
                    models.JSONField(
                        blank=True, default=dict, verbose_name="Umgehängt"
                    ),
                ),
                (
                    "konflikte",
                    models.JSONField(
                        blank=True, default=dict, verbose_name="Nicht umgehängt"
                    ),
                ),
                (
                    "erfolgt_am",
                    models.DateTimeField(auto_now_add=True, verbose_name="Erfolgt am"),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ris_neuzuordnungen",
                        to="tenants.organization",
                        verbose_name="Organisation",
                    ),
                ),
            ],
            options={
                "verbose_name": "RIS-Neuzuordnung",
                "verbose_name_plural": "RIS-Neuzuordnungen",
                "ordering": ["-erfolgt_am"],
                "indexes": [
                    models.Index(fields=["von"], name="work_risneuzuord_von_idx")
                ],
            },
        ),
    ]

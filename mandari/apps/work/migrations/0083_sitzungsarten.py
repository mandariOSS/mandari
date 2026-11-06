# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsarten (Issue #1231, Epic #1226), Teil 1: neue Tabelle und die Spalte ``meeting_type`` an Reihen, Sitzungen
und Standard-TOPs, zunächst leer erlaubt.

Rein additiv. Die Zuordnung zur Standardart und die Pflichtspalte folgen in ``0084_sitzungsarten_zuordnen``.
Rückwärts entfernt Django Spalten und Tabelle wieder; alle übrigen Daten bleiben unverändert.
"""

import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0063_zusammenfassung_hinweis"),
        ("tenants", "0029_provisioning_v2"),
        ("work", "0082_live_notizen_erwaehnungen"),
    ]

    operations = [
        migrations.CreateModel(
            name="FactionMeetingType",
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
                ("name", models.CharField(max_length=200, verbose_name="Name")),
                (
                    "position",
                    models.PositiveIntegerField(default=0, verbose_name="Reihenfolge"),
                ),
                (
                    "is_default",
                    models.BooleanField(
                        default=False,
                        help_text="Genau eine Art je Organisation; von Hand angelegte Sitzungen bekommen sie",
                        verbose_name="Standardart",
                    ),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="Aktiv")),
                (
                    "number_start",
                    models.PositiveIntegerField(
                        default=1,
                        help_text="Erste Sitzungsnummer dieser Art",
                        validators=[django.core.validators.MinValueValidator(1)],
                        verbose_name="Startnummer",
                    ),
                ),
                (
                    "participants_mode",
                    models.CharField(
                        choices=[
                            ("all", "Alle Mitglieder"),
                            ("role", "Eine Rolle"),
                            ("committee", "Ein Gremium"),
                            ("persons", "Ausgewählte Personen"),
                        ],
                        default="all",
                        max_length=20,
                        verbose_name="Teilnehmende",
                    ),
                ),
                (
                    "visibility",
                    models.CharField(
                        choices=[
                            ("public", "Öffentlich"),
                            ("members", "Alle Mitglieder"),
                            ("participants", "Nur Teilnehmende"),
                        ],
                        default="members",
                        max_length=20,
                        verbose_name="Sichtbarkeit",
                    ),
                ),
                (
                    "agenda_mode",
                    models.CharField(
                        choices=[
                            ("standard", "Mit Standard-Tagesordnung"),
                            ("free", "Ohne feste Tagesordnung"),
                        ],
                        default="standard",
                        max_length=20,
                        verbose_name="Tagesordnung",
                    ),
                ),
                (
                    "non_public_enabled",
                    models.BooleanField(default=True, verbose_name="Nichtöffentlicher Teil"),
                ),
                (
                    "voting_enabled",
                    models.BooleanField(default=True, verbose_name="Abstimmungen und Beschlussfähigkeit"),
                ),
                (
                    "settings",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Leer heißt: wie in den Einstellungen der Organisation",
                        verbose_name="Regeln",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="faction_meeting_types",
                        to="tenants.organization",
                        verbose_name="Organisation",
                    ),
                ),
                (
                    "participants",
                    models.ManyToManyField(
                        blank=True,
                        related_name="faction_meeting_types",
                        to="tenants.membership",
                        verbose_name="Ausgewählte Teilnehmende",
                    ),
                ),
                (
                    "participants_committee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="faction_meeting_types",
                        to="insight_core.oparlorganization",
                        verbose_name="Gremium der Teilnehmenden",
                    ),
                ),
                (
                    "participants_role",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="faction_meeting_types",
                        to="tenants.role",
                        verbose_name="Rolle der Teilnehmenden",
                    ),
                ),
            ],
            options={
                "verbose_name": "Sitzungsart",
                "verbose_name_plural": "Sitzungsarten",
                "ordering": ["position", "name"],
            },
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="meeting_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="meetings",
                to="work.factionmeetingtype",
                verbose_name="Sitzungsart",
            ),
        ),
        migrations.AddField(
            model_name="factionmeetingschedule",
            name="meeting_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="schedules",
                to="work.factionmeetingtype",
                verbose_name="Sitzungsart",
            ),
        ),
        migrations.AddField(
            model_name="factionstandardagendaitem",
            name="meeting_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="standard_agenda_items",
                to="work.factionmeetingtype",
                verbose_name="Sitzungsart",
            ),
        ),
        migrations.AddConstraint(
            model_name="factionmeetingtype",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_default", True)),
                fields=("organization",),
                name="uniq_faction_meeting_type_default",
            ),
        ),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
# Änderungshistorie (Audit) für Fraktionssitzungen (Issue #66)

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0019_einladung_und_protokoll_opt_in"),
        ("work", "0041_einladung_und_protokoll_opt_in"),
    ]

    operations = [
        migrations.CreateModel(
            name="FactionAuditLog",
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
                    "actor_label",
                    models.CharField(blank=True, max_length=200, verbose_name="Akteur"),
                ),
                (
                    "ip_address",
                    models.GenericIPAddressField(blank=True, null=True, verbose_name="IP-Adresse"),
                ),
                ("user_agent", models.TextField(blank=True, verbose_name="User-Agent")),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("create", "Erstellt"),
                            ("update", "Geändert"),
                            ("delete", "Gelöscht"),
                            ("status", "Statuswechsel"),
                            ("invitation_sent", "Einladung versandt"),
                            ("invitation_updated", "Aktualisierte Einladung versandt"),
                            ("reminder_sent", "Erinnerung versandt"),
                            ("protocol_submitted", "Protokoll zur Genehmigung"),
                            ("protocol_approved", "Protokoll genehmigt"),
                            ("participation", "Teilnahme geändert"),
                            ("proposal", "TOP vorgeschlagen"),
                            ("proposal_accepted", "TOP-Vorschlag angenommen"),
                            ("proposal_rejected", "TOP-Vorschlag abgelehnt"),
                            ("decision", "Abstimmung erfasst"),
                            ("generated", "Automatisch erzeugt"),
                            ("auto_cancelled", "Automatisch entfallen"),
                        ],
                        max_length=50,
                        verbose_name="Aktion",
                    ),
                ),
                ("model_name", models.CharField(max_length=100, verbose_name="Modell")),
                ("object_id", models.UUIDField(verbose_name="Objekt-ID")),
                (
                    "object_repr",
                    models.CharField(blank=True, max_length=500, verbose_name="Objekt-Beschreibung"),
                ),
                (
                    "meeting_id_ref",
                    models.UUIDField(blank=True, null=True, verbose_name="Sitzungs-ID"),
                ),
                (
                    "is_internal",
                    models.BooleanField(default=False, verbose_name="Nicht-öffentlicher Inhalt"),
                ),
                (
                    "changes",
                    models.JSONField(blank=True, default=dict, verbose_name="Änderungen"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "membership",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="faction_audit_entries",
                        to="tenants.membership",
                        verbose_name="Mitglied",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="faction_audit_logs",
                        to="tenants.organization",
                        verbose_name="Organisation",
                    ),
                ),
            ],
            options={
                "verbose_name": "Fraktions-Audit-Eintrag",
                "verbose_name_plural": "Fraktions-Änderungshistorie",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="factionauditlog",
            index=models.Index(
                fields=["organization", "model_name", "object_id"],
                name="work_factio_organiz_59e3b1_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="factionauditlog",
            index=models.Index(
                fields=["organization", "created_at"],
                name="work_factio_organiz_4eb95a_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="factionauditlog",
            index=models.Index(
                fields=["organization", "meeting_id_ref"],
                name="work_factio_organiz_8311f1_idx",
            ),
        ),
    ]

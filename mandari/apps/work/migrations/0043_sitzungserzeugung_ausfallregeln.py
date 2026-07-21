# SPDX-License-Identifier: AGPL-3.0-or-later
# Sitzungserzeugung aus der Sitzungsreihe + modulare Ausfallregeln (Issue #61)

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0022_oparl_tombstones"),
        ("tenants", "0019_einladung_und_protokoll_opt_in"),
        ("work", "0042_fraktions_aenderungshistorie"),
    ]

    operations = [
        migrations.CreateModel(
            name="FactionSuspensionRule",
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
                ("is_active", models.BooleanField(default=True, verbose_name="Aktiv")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "ris_organization",
                    models.ForeignKey(
                        help_text="Nach einer Sitzung dieses Gremiums entfällt die nächste Fraktionssitzung",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="faction_suspension_rules",
                        to="insight_core.oparlorganization",
                        verbose_name="RIS-Gremium",
                    ),
                ),
                (
                    "schedule",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="suspension_rules",
                        to="work.factionmeetingschedule",
                        verbose_name="Sitzungsplan",
                    ),
                ),
            ],
            options={
                "verbose_name": "RIS-Ausfallregel",
                "verbose_name_plural": "RIS-Ausfallregeln",
                "ordering": ["created_at"],
            },
        ),
        migrations.AlterUniqueTogether(
            name="factionsuspensionrule",
            unique_together={("schedule", "ris_organization")},
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="cancellation_reason",
            field=models.CharField(blank=True, max_length=300, verbose_name="Ausfallgrund"),
        ),
        migrations.AddField(
            model_name="factionmeeting",
            name="scheduled_date",
            field=models.DateField(
                blank=True,
                help_text="Solltermin laut Sitzungsreihe (für automatisch erzeugte Sitzungen)",
                null=True,
                verbose_name="Plantermin",
            ),
        ),
        migrations.AddField(
            model_name="factionmeetingexception",
            name="end_date",
            field=models.DateField(
                blank=True,
                help_text="Optional: Zeitraum bis einschließlich dieses Datums (z.B. Urlaub)",
                null=True,
                verbose_name="Enddatum",
            ),
        ),
        migrations.AlterField(
            model_name="factionmeeting",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="created_faction_meetings",
                to="tenants.membership",
                verbose_name="Erstellt von",
            ),
        ),
        migrations.AddConstraint(
            model_name="factionmeeting",
            constraint=models.UniqueConstraint(
                condition=models.Q(("scheduled_date__isnull", False)),
                fields=("schedule", "scheduled_date"),
                name="uniq_faction_meeting_schedule_scheduled_date",
            ),
        ),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schatten-Quelle des RIS-Projektors für Session-Mandanten (Issue #536): Tabelle ``hub_ris_schatten``.

Nur eine neue Tabelle neben dem RIS-Bestand; ein Rückfall auf ein älteres Image braucht keinen Rückbau.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="RisSchatten",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("mandant", models.UUIDField(verbose_name="Session-Mandant")),
                (
                    "typ",
                    models.CharField(
                        help_text="Segment der OParl-Adresse, z. B. meeting",
                        max_length=20,
                        verbose_name="Objekttyp",
                    ),
                ),
                (
                    "bestand_id",
                    models.UUIDField(
                        help_text="kanonische Kennung, wie im Journal",
                        verbose_name="Kennung im RIS-Bestand",
                    ),
                ),
                (
                    "external_id",
                    models.TextField(
                        help_text="OParl-Adresse, unter der der Bestand das Objekt führt",
                        verbose_name="Adresse",
                    ),
                ),
                (
                    "spalten",
                    models.JSONField(
                        default=dict,
                        help_text="fachliche Spalten des Bestands",
                        verbose_name="Spalten",
                    ),
                ),
                (
                    "verweise",
                    models.JSONField(
                        default=dict,
                        help_text="Adressen der Kommune, Sitzung, Vorlage …",
                        verbose_name="Bezüge",
                    ),
                ),
                (
                    "deleted",
                    models.BooleanField(
                        default=False, verbose_name="gelöscht bzw. zurückgenommen"
                    ),
                ),
                (
                    "deletion_reason",
                    models.CharField(
                        blank=True, max_length=20, null=True, verbose_name="Grund"
                    ),
                ),
                (
                    "oparl_modified",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="geändert laut Quelle"
                    ),
                ),
                (
                    "seq",
                    models.BigIntegerField(
                        blank=True,
                        help_text="zuletzt verarbeitetes Ereignis",
                        null=True,
                        verbose_name="Folgenummer",
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="aktualisiert am"),
                ),
            ],
            options={
                "verbose_name": "Objekt der Schatten-Quelle",
                "verbose_name_plural": "Schatten-Quelle des RIS-Projektors",
                "db_table": "hub_ris_schatten",
                "indexes": [
                    models.Index(fields=["mandant", "typ"], name="hub_ris_schatten_typ")
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("mandant", "bestand_id"),
                        name="hub_ris_schatten_kennung",
                    )
                ],
            },
        ),
    ]

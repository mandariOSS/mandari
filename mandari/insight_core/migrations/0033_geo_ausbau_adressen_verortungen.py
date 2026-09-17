# Geo-Ausbaustufe: Adressen (Hausnummern-Punkte aus OSM) und Verortungstabelle für die
# Umkreissuche mit Index sowie den Admin-Korrektur-Workflow (Issue #54)

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("insight_core", "0032_oparlsource_error_kind_robots"),
    ]

    operations = [
        migrations.CreateModel(
            name="Address",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "osm_type",
                    models.CharField(
                        choices=[
                            ("node", "Node"),
                            ("way", "Way"),
                            ("relation", "Relation"),
                        ],
                        default="node",
                        max_length=8,
                    ),
                ),
                (
                    "osm_id",
                    models.BigIntegerField(
                        help_text="OpenStreetMap-ID des Objekts mit addr:*-Tags"
                    ),
                ),
                ("street", models.CharField(max_length=255, verbose_name="Straße")),
                ("normalized_street", models.CharField(db_index=True, max_length=255)),
                (
                    "house_number",
                    models.CharField(max_length=20, verbose_name="Hausnummer"),
                ),
                ("normalized_house_number", models.CharField(max_length=20)),
                (
                    "postal_code",
                    models.CharField(blank=True, default="", max_length=20),
                ),
                ("latitude", models.DecimalField(decimal_places=7, max_digits=10)),
                ("longitude", models.DecimalField(decimal_places=7, max_digits=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "body",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="addresses",
                        to="insight_core.oparlbody",
                        verbose_name="Kommune",
                    ),
                ),
            ],
            options={
                "verbose_name": "Adresse",
                "verbose_name_plural": "Adressen",
                "db_table": "insight_addresses",
                "indexes": [
                    models.Index(
                        fields=["body", "normalized_street", "normalized_house_number"],
                        name="idx_address_body_street_hn",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("body", "osm_type", "osm_id"),
                        name="uniq_address_body_osm",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="PaperLocation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=500,
                        verbose_name="Ortsbezeichnung",
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("oparl", "OParl-Ort"),
                            ("address_match", "Adresse (Straßenverzeichnis)"),
                            ("street_match", "Straße (Straßenverzeichnis)"),
                            ("ai", "KI-Extraktion"),
                            ("manual", "Manuell"),
                        ],
                        default="street_match",
                        max_length=20,
                        verbose_name="Herkunft",
                    ),
                ),
                (
                    "confidence",
                    models.FloatField(blank=True, null=True, verbose_name="Konfidenz"),
                ),
                ("latitude", models.FloatField(verbose_name="Breite")),
                ("longitude", models.FloatField(verbose_name="Länge")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("auto", "Automatisch"),
                            ("confirmed", "Bestätigt"),
                            ("removed", "Entfernt"),
                        ],
                        db_index=True,
                        default="auto",
                        max_length=12,
                        verbose_name="Prüfstatus",
                    ),
                ),
                (
                    "reviewed_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Geprüft am"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "body",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="paper_locations",
                        to="insight_core.oparlbody",
                        verbose_name="Kommune",
                    ),
                ),
                (
                    "paper",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="paper_locations",
                        to="insight_core.oparlpaper",
                        verbose_name="Vorgang",
                    ),
                ),
            ],
            options={
                "verbose_name": "Verortung",
                "verbose_name_plural": "Verortungen",
                "db_table": "insight_paper_locations",
                "indexes": [
                    models.Index(
                        fields=["body", "latitude", "longitude"],
                        name="idx_paperloc_body_lat_lon",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("paper", "latitude", "longitude"),
                        name="uniq_paperloc_paper_point",
                    )
                ],
            },
        ),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kommunenverzeichnis für den Kommunenwechsel im Bürgerportal (Issue #783, Stufe 2).

Zwei neue Tabellen, abwärtskompatibel (ein älteres Image lässt sie liegen). In PostgreSQL kommt die
Erweiterung ``pg_trgm`` (seit PostgreSQL 13 „trusted“, der Eigentümer der Datenbank darf sie anlegen)
und ein Trigramm-Index auf die Suchbegriffe dazu; SQLite (Tests, Entwicklung) sucht ohne ihn.
"""

import django.db.models.deletion
from django.db import migrations, models

TRIGRAMM_INDEX = "insight_municipality_term_trgm"


def trigramm_index_anlegen(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    schema_editor.execute(
        f'CREATE INDEX IF NOT EXISTS "{TRIGRAMM_INDEX}" ON "insight_municipality_term" '
        'USING gin ("normalized" gin_trgm_ops)'
    )


def trigramm_index_entfernen(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(f'DROP INDEX IF EXISTS "{TRIGRAMM_INDEX}"')


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0044_page_feedback"),
    ]

    operations = [
        migrations.CreateModel(
            name="Municipality",
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
                    "key",
                    models.CharField(
                        help_text="Regionalschlüssel (12 Stellen) oder, wenn nicht bekannt, Amtlicher Gemeindeschlüssel (8 Stellen)",
                        max_length=12,
                        unique=True,
                        verbose_name="Schlüssel",
                    ),
                ),
                (
                    "ags",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        default="",
                        max_length=8,
                        verbose_name="AGS",
                    ),
                ),
                ("name", models.CharField(max_length=200, verbose_name="Name")),
                (
                    "kind",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="z. B. Stadt, Gemeinde, Samtgemeinde",
                        max_length=60,
                        verbose_name="Art",
                    ),
                ),
                (
                    "is_association",
                    models.BooleanField(
                        default=False,
                        help_text="Samtgemeinde, Verbandsgemeinde, Amt o. Ä.: im Stöbern eine Stufe zwischen Kreis und Gemeinde",
                        verbose_name="Gemeindeverband",
                    ),
                ),
                (
                    "district_key",
                    models.CharField(db_index=True, max_length=5, verbose_name="Kreisschlüssel"),
                ),
                (
                    "district",
                    models.CharField(blank=True, default="", max_length=200, verbose_name="Kreis"),
                ),
                (
                    "state_key",
                    models.CharField(db_index=True, max_length=2, verbose_name="Land"),
                ),
                (
                    "latitude",
                    models.FloatField(blank=True, db_index=True, null=True, verbose_name="Breite"),
                ),
                (
                    "longitude",
                    models.FloatField(blank=True, null=True, verbose_name="Länge"),
                ),
                (
                    "imported",
                    models.BooleanField(
                        default=False,
                        help_text="Aus der CSV-Datei (Quellen mit Namensnennung); nicht gesetzt bei Einträgen aus den gelisteten Kommunen",
                        verbose_name="Aus Datei importiert",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Kommune im Verzeichnis",
                "verbose_name_plural": "Kommunenverzeichnis",
                "db_table": "insight_municipality",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="MunicipalityTerm",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("name", "Name"),
                            ("ortsteil", "Ortsteil"),
                            ("plz", "Postleitzahl"),
                        ],
                        default="name",
                        max_length=10,
                    ),
                ),
                ("label", models.CharField(max_length=200, verbose_name="Anzeige")),
                ("normalized", models.CharField(db_index=True, max_length=200)),
                (
                    "municipality",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="terms",
                        to="insight_core.municipality",
                    ),
                ),
            ],
            options={
                "verbose_name": "Suchbegriff im Kommunenverzeichnis",
                "verbose_name_plural": "Suchbegriffe im Kommunenverzeichnis",
                "db_table": "insight_municipality_term",
                "constraints": [
                    models.UniqueConstraint(
                        fields=("municipality", "kind", "normalized"),
                        name="uniq_municipality_term",
                    )
                ],
            },
        ),
        migrations.RunPython(trigramm_index_anlegen, trigramm_index_entfernen, elidable=False),
    ]

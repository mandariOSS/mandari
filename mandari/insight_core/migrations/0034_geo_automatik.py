# Automatische Geo-Zuordnung (Issue #351): Regionalschlüssel, Kennzeichnung „keine Gebietskörperschaft“
# mit übergeordneter Körperschaft und Vorschläge für mehrdeutige OSM-Treffer. Die neuen Spalten an
# oparl_bodies sind nullable oder haben einen DB-Default, weil der Ingestor sie nicht kennt.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("insight_core", "0033_geo_ausbau_adressen_verortungen"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlbody",
            name="is_non_territorial",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Zweckverband, Gesellschaft, Anstalt, Waldgemarkung o. Ä. ohne eigenes Gebiet: keine OSM-Grenze, kein Straßenverzeichnis, erscheint nicht als Lücke. Die Karte zeigt das Gebiet der übergeordneten Körperschaft. Verbandsgemeinden sind Gebietskörperschaften.",
                verbose_name="Keine Gebietskörperschaft",
            ),
        ),
        migrations.AddField(
            model_name="oparlbody",
            name="rgs",
            field=models.CharField(
                blank=True,
                help_text="Amtlicher Regionalschlüssel (12-stellig, bei Verbandsgemeinden, Ämtern und Samtgemeinden 9-stellig). Wird von resolve_body_geodata aus OParl oder OSM übernommen.",
                max_length=12,
                null=True,
                verbose_name="Regionalschlüssel",
            ),
        ),
        migrations.AddField(
            model_name="oparlbody",
            name="territory_parent",
            field=models.ForeignKey(
                blank=True,
                help_text="Übergeordnete Körperschaft, deren Gebiet für die Karte gilt (nur ohne eigenes Gebiet).",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="non_territorial_members",
                to="insight_core.oparlbody",
                verbose_name="Gebiet von",
            ),
        ),
        migrations.AddField(
            model_name="oparlbody",
            name="territory_set_manually",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Wird beim Ändern von „Keine Gebietskörperschaft“ oder „Gebiet von“ im Admin gesetzt. resolve_body_geodata ändert die beiden Angaben dann nicht mehr.",
                verbose_name="Gebietsangabe von Hand gesetzt",
            ),
        ),
        migrations.CreateModel(
            name="OParlBodyGeoSuggestion",
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
                    "osm_relation_id",
                    models.BigIntegerField(verbose_name="OSM-Relation"),
                ),
                ("name", models.CharField(max_length=255, verbose_name="Name in OSM")),
                (
                    "admin_level",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        help_text="OSM admin_level: 6 Kreis/kreisfreie Stadt, 7 Verbandsgemeinde/Amt, 8 Gemeinde, 9–10 Ortsteil",
                        null=True,
                        verbose_name="Verwaltungsebene",
                    ),
                ),
                (
                    "ags",
                    models.CharField(
                        blank=True, default="", max_length=8, verbose_name="AGS"
                    ),
                ),
                (
                    "rgs",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=12,
                        verbose_name="Regionalschlüssel",
                    ),
                ),
                (
                    "reason",
                    models.CharField(
                        blank=True, default="", max_length=255, verbose_name="Herkunft"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "body",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="geo_suggestions",
                        to="insight_core.oparlbody",
                        verbose_name="Kommune",
                    ),
                ),
            ],
            options={
                "verbose_name": "Geo-Vorschlag",
                "verbose_name_plural": "Geo-Vorschläge",
                "db_table": "insight_body_geo_suggestions",
                "ordering": ["body__name", "name"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("body", "osm_relation_id"),
                        name="uniq_geo_suggestion_body_rel",
                    )
                ],
            },
        ),
    ]

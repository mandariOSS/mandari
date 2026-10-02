# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Körperschaften im Mandanten (Issue #756): Modell ``SessionBody`` und Fremdschlüssel an Gremium, Vorlage und
Nummernkreis.

Abwärtskompatibel: neue Tabelle, neue Spalten nullbar ohne Vorgabewert. Ein älteres Image läuft auf diesem Schema
weiter; was es anlegt, hat keine Körperschaft und zählt zur Standardkörperschaft (``body_service.body_q``), bis
der nächste ``migrate``-Lauf es zuordnet. Die Arten der Körperschaft am Mandanten bekommen nur neue Werte (keine
Spaltenänderung). Die Zuordnung des Bestands folgt in 0057.
"""

import django.core.validators
import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0053_dcat_kennung"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sessionnumberrange",
            name="pattern",
            field=models.CharField(
                default="V/{jahr}/{lfd:4}",
                help_text="Platzhalter: {lfd} bzw. {lfd:4}, {jahr}, {jj}, {wp}, {prefix}, {gremium}, {koerperschaft}",
                max_length=100,
                verbose_name="Muster",
            ),
        ),
        migrations.AlterField(
            model_name="sessiontenant",
            name="body_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("stadt", "Stadt"),
                    ("kreisfreie_stadt", "Kreisfreie Stadt"),
                    ("grosse_selbstaendige_stadt", "Große selbständige Stadt"),
                    ("gemeinde", "Gemeinde"),
                    ("einheitsgemeinde", "Einheitsgemeinde"),
                    ("selbstaendige_gemeinde", "Selbständige Gemeinde"),
                    ("samtgemeinde", "Samtgemeinde"),
                    ("mitgliedsgemeinde", "Mitgliedsgemeinde"),
                    ("kreis", "Kreis bzw. Landkreis"),
                    ("region", "Region"),
                    ("bezirk", "Bezirk"),
                    ("gemeindeverband", "Gemeindeverband"),
                    ("regionalverband", "Regionalverband"),
                    ("zweckverband", "Zweckverband"),
                    ("kommunale_gesellschaft", "Kommunale Gesellschaft"),
                    ("sonstige", "Sonstige Körperschaft"),
                ],
                max_length=30,
                null=True,
                verbose_name="Körperschaftstyp",
            ),
        ),
        migrations.CreateModel(
            name="SessionBody",
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
                    "name",
                    models.CharField(
                        help_text="z. B. „Gemeinde Musterdorf“",
                        max_length=255,
                        verbose_name="Name",
                    ),
                ),
                (
                    "short_name",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="z. B. „MD“; Wert des Platzhalters {koerperschaft} in Nummernkreisen",
                        max_length=50,
                        verbose_name="Kurzname",
                    ),
                ),
                (
                    "slug",
                    models.SlugField(
                        help_text="Eindeutig im Mandanten; erscheint in Filteradressen",
                        max_length=100,
                        verbose_name="Kurzkennung",
                    ),
                ),
                (
                    "body_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("stadt", "Stadt"),
                            ("kreisfreie_stadt", "Kreisfreie Stadt"),
                            ("grosse_selbstaendige_stadt", "Große selbständige Stadt"),
                            ("gemeinde", "Gemeinde"),
                            ("einheitsgemeinde", "Einheitsgemeinde"),
                            ("selbstaendige_gemeinde", "Selbständige Gemeinde"),
                            ("samtgemeinde", "Samtgemeinde"),
                            ("mitgliedsgemeinde", "Mitgliedsgemeinde"),
                            ("kreis", "Kreis bzw. Landkreis"),
                            ("region", "Region"),
                            ("bezirk", "Bezirk"),
                            ("gemeindeverband", "Gemeindeverband"),
                            ("regionalverband", "Regionalverband"),
                            ("zweckverband", "Zweckverband"),
                            ("kommunale_gesellschaft", "Kommunale Gesellschaft"),
                            ("sonstige", "Sonstige Körperschaft"),
                        ],
                        default="",
                        max_length=30,
                        verbose_name="Art der Körperschaft",
                    ),
                ),
                (
                    "ags",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="8 Stellen für Gemeinden, 5 für Kreise",
                        max_length=8,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^(\\d{2}|\\d{3}|\\d{5}|\\d{8})$",
                                "2, 3, 5 oder 8 Ziffern.",
                            )
                        ],
                        verbose_name="Amtlicher Gemeindeschlüssel",
                    ),
                ),
                (
                    "rgs",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="12 Stellen; bei Mitgliedsgemeinden mit dem Verbandsschlüssel der Samtgemeinde",
                        max_length=12,
                        validators=[django.core.validators.RegexValidator("^(\\d{9}|\\d{12})$", "9 oder 12 Ziffern.")],
                        verbose_name="Regionalschlüssel",
                    ),
                ),
                (
                    "is_default",
                    models.BooleanField(
                        default=False,
                        help_text="Körperschaft, die die Verwaltung trägt; Vorgabe beim Anlegen und für ältere Daten",
                        verbose_name="Standardkörperschaft",
                    ),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="Aktiv")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        help_text="z. B. die Samtgemeinde einer Mitgliedsgemeinde",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="children",
                        to="session.sessionbody",
                        verbose_name="Übergeordnete Körperschaft",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bodies",
                        to="session.sessiontenant",
                        verbose_name="Mandant",
                    ),
                ),
            ],
            options={
                "verbose_name": "Körperschaft",
                "verbose_name_plural": "Körperschaften",
                "db_table": "session_bodies",
                "ordering": ["-is_default", "name"],
            },
        ),
        migrations.AddField(
            model_name="sessionnumberrange",
            name="body",
            field=models.ForeignKey(
                blank=True,
                help_text="Leer: gilt für alle Körperschaften ohne eigenen Nummernkreis",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="number_ranges",
                to="session.sessionbody",
                verbose_name="Körperschaft",
            ),
        ),
        migrations.AddField(
            model_name="sessionorganization",
            name="body",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="organizations",
                to="session.sessionbody",
                verbose_name="Körperschaft",
            ),
        ),
        migrations.AddField(
            model_name="sessionpaper",
            name="body",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="papers",
                to="session.sessionbody",
                verbose_name="Körperschaft",
            ),
        ),
        migrations.AddConstraint(
            model_name="sessionbody",
            constraint=models.UniqueConstraint(fields=("tenant", "slug"), name="uniq_session_body_slug"),
        ),
        migrations.AddConstraint(
            model_name="sessionbody",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_default", True)),
                fields=("tenant",),
                name="uniq_session_body_default",
            ),
        ),
    ]

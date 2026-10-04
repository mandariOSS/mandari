# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Grund der Löschmarkierung im RIS-Bestand (Issue #524): quelle_geloescht, zurueckgenommen, nichtoeffentlich,
datenschutz. Nur ``AddField`` nullable ohne Standardwert: Ein Image und ein Ingestor ohne die Spalte markieren weiter.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0049_beschlussfassung_befuellen"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlagendaitem",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparlbody",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparlconsultation",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparlfile",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparllegislativeterm",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparllocation",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparlmeeting",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparlmembership",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparlorganization",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparlpaper",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
        migrations.AddField(
            model_name="oparlperson",
            name="deletion_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("quelle_geloescht", "In der Quelle gelöscht"),
                    ("zurueckgenommen", "Zurückgezogen"),
                    ("nichtoeffentlich", "Nicht mehr öffentlich"),
                    ("datenschutz", "Aus Datenschutzgründen entfernt"),
                ],
                max_length=20,
                null=True,
                verbose_name="Grund der Löschmarkierung",
            ),
        ),
    ]

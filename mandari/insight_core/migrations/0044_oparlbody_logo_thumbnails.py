# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WebP-Vorschaubilder der Kommunen-Logos (insight_core/logo_vorschau.py).

Abwärtskompatibel: Die Spalte ist nullbar und hat keinen Default. Der Ingestor und ein älteres Image legen
Kommunen ohne sie an; ein älteres Image ignoriert sie. Keine Datenmigration – der Bestand bekommt seine
Vorschaubilder nach dem Deploy mit ``python manage.py build_logo_thumbnails``; bis dahin zeigen die Seiten
das Original.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0043_dokumentablage_sha256"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlbody",
            name="logo_thumbnails",
            field=models.JSONField(
                blank=True,
                editable=False,
                help_text="Wird beim Speichern eines Logos erzeugt (Befehl build_logo_thumbnails für den Bestand).",
                null=True,
                verbose_name="Logo-Vorschaubilder",
            ),
        ),
    ]

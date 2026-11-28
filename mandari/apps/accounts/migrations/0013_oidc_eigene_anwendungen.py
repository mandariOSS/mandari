# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenID-Connect-Anbieter, eigene Oberflächen und Dienste (Issue #1591): Bereich, der eine Anwendung registriert hat
(Kommune oder Fraktion), und Grenzen je Anwendung (Anfragen je Minute, Einträge je Seite).

Abwärtsverträglich: Alle drei Felder haben einen Standardwert in der Datenbank. Ältere Images legen weiter Anwendungen
an und löschen sie.
"""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0012_email_kleinschreibung"),
    ]

    operations = [
        migrations.AddField(
            model_name="oauthapplication",
            name="owner",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Bereich, der die Anwendung selbst registriert hat, etwa eine Kommune („session-mandant:<Kennung>“) oder eine Fraktion („work-organisation:<Kennung>“). Leer: vom Betreiber angelegt.",
                max_length=128,
                verbose_name="Registriert von",
            ),
        ),
        migrations.AddField(
            model_name="oauthapplication",
            name="rate_limit_per_minute",
            field=models.PositiveIntegerField(
                db_default=600,
                default=600,
                help_text="Höchstens so viele Anfragen je Minute an die Schnittstelle, über alle Tokens der Anwendung. 0: ohne Grenze.",
                validators=[django.core.validators.MaxValueValidator(100000)],
                verbose_name="Anfragen je Minute",
            ),
        ),
        migrations.AddField(
            model_name="oauthapplication",
            name="max_page_size",
            field=models.PositiveSmallIntegerField(
                db_default=100,
                default=100,
                help_text="Höchstens so viele Einträge je Seite in Listen der Schnittstelle.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(1000),
                ],
                verbose_name="Einträge je Seite",
            ),
        ),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anbieterauswahl der Organisation als Aufruf statt fester Liste (Issue #1046).

Die Auswahl kommt aus ``apps.common.ki_anbieter.auswahl_organisation``; neue Vorlagen (hier Nebius Token Factory)
brauchen so keine weitere Migration. Nur der Modellzustand ändert sich, nicht die Datenbank (keine SQL-Anweisung)
und keine Daten; ein älteres Image läuft ohne Rückbau weiter.
"""

from django.db import migrations, models

import apps.common.ki_anbieter


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0027_ki_anbieter_europa"),
    ]

    operations = [
        migrations.AlterField(
            model_name="organization",
            name="ai_provider",
            field=models.CharField(
                blank=True,
                choices=apps.common.ki_anbieter.auswahl_organisation,
                default="",
                help_text=(
                    "Nur mit eigenem KI API Key: Vorlage oder eigener Endpunkt. Ohne eigenen Key gelten die "
                    "KI-Einstellungen der Plattform."
                ),
                max_length=20,
                verbose_name="KI-Anbieter",
            ),
        ),
    ]

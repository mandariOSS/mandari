# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anbieterauswahl der KI-Einstellungen als Aufruf statt fester Liste (Issue #1046).

Die Auswahl kommt aus ``apps.common.ki_anbieter.auswahl_ki_einstellungen``; neue Vorlagen (hier Nebius Token
Factory) brauchen so keine weitere Migration. Nur der Modellzustand ändert sich, nicht die Datenbank (keine SQL-
Anweisung) und keine Daten; ein älteres Image läuft ohne Rückbau weiter.
"""

from django.db import migrations, models

import apps.common.ki_anbieter


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0012_pg_stat_statements"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aisettings",
            name="provider",
            field=models.CharField(
                blank=True,
                choices=apps.common.ki_anbieter.auswahl_ki_einstellungen,
                default="",
                help_text=(
                    "Vorlage eines OpenAI-kompatiblen Anbieters oder eigener Endpunkt. Wirksam nur, wenn der Host in "
                    "KI_ERLAUBTE_HOSTS steht."
                ),
                max_length=20,
                verbose_name="KI-Anbieter",
            ),
        ),
    ]

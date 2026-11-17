# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufgaben: weitere Zuständige (Issue #1462).

Eine Aufgabe hat neben der zugewiesenen Person beliebig viele weitere Zuständige. Die Migration legt dafür nur eine
neue Verknüpfungstabelle an; bestehende Tabellen und Zeilen bleiben unverändert. Das Image davor kennt die Tabelle
nicht und läuft unverändert weiter.

Die Abhängigkeit nennt ``tenants.0030``: Die Tabelle braucht nur die Mitgliedschaften, und dieser Stand ist auch in
der Linie 0.13 vorhanden.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0030_mitgliedschaft_auslauf"),
        ("work", "0088_top_teil_feste_werte"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="weitere_zustaendige",
            field=models.ManyToManyField(
                blank=True,
                related_name="mitzustaendige_aufgaben",
                to="tenants.membership",
                verbose_name="Weitere Zuständige",
            ),
        ),
    ]

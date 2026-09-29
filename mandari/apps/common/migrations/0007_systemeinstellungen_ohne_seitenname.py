# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Systemeinstellungen: Felder „Seitenname“ und „Seitenbeschreibung“ entfernen (Issue #588).

Beide Felder standen im Admin, wirkten aber nirgends. Entfernt werden sie nur aus dem
Modellzustand; die Spalten bleiben, damit eine ältere Version nach einem Rückfall (Image
ohne Migrationsrückbau) sie weiter lesen und schreiben kann. Vorher erhalten sie einen
Datenbank-Standardwert, damit die neue Version Zeilen ohne diese Spalten anlegen kann.

Die Spalten selbst entfallen mit einer Folgeversion.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0006_systemeinstellungen_verschluesselt"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sitesettings",
            name="site_name",
            field=models.CharField(
                db_default="Mandari", default="Mandari", max_length=100, verbose_name="Seitenname"
            ),
        ),
        migrations.AlterField(
            model_name="sitesettings",
            name="site_description",
            field=models.TextField(
                blank=True,
                db_default="Kommunalpolitische Transparenz",
                default="Kommunalpolitische Transparenz",
                verbose_name="Seitenbeschreibung",
            ),
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="sitesettings", name="site_name"),
                migrations.RemoveField(model_name="sitesettings", name="site_description"),
            ],
        ),
    ]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schalter je Mandant für das neue Erscheinungsbild des Sitzungsdienstes (Issue #944).

Rein additiv: Das Feld steht für alle bestehenden Mandanten auf „aus“ (auch in der Datenbank, ``db_default``), ein
Rückfall auf das vorige Image legt neue Mandanten weiter ohne die Spalte an. Kein Mandant wird eingeschaltet, auch die
Demo nicht: Produktion und Demo bleiben unverändert, bis die Freigabe vorliegt. Keine anderen Daten werden berührt.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0067_niederschrift_genehmigungsweg"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessiontenant",
            name="session_new_design",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text=(
                    "Schaltet für diesen Mandanten den neuen Rahmen des Sitzungsdienstes ein (Seitenleiste wie Work, "
                    "Suche in der Kopfzeile, Leiste unten am Handy). Aus: bisheriger Rahmen. Daten ändern sich nicht."
                ),
                verbose_name="Neues Erscheinungsbild im Sitzungsdienst",
            ),
        ),
    ]

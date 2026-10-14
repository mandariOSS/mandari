# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kennzeichen für die automatisch erzeugte Antragsfassung (PDF) an einer Anlage (Teil von #584).

Rein additiv: Bestehende Anlagen stehen auf „nein“ (auch in der Datenbank, ``db_default``), ein Rückfall auf das
vorige Image legt Anlagen weiter ohne die Spalte an. Keine anderen Daten werden berührt.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("session", "0068_session_neues_erscheinungsbild"),
    ]

    operations = [
        migrations.AddField(
            model_name="sessionfile",
            name="is_application_pdf",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="PDF des eingereichten Antrags; wird mit der Vorlage öffentlich.",
                verbose_name="Antragsfassung (automatisch erzeugt)",
            ),
        ),
    ]

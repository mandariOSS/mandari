# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schalter je Organisation für das neue Erscheinungsbild von Work (Issue #852).

Das Feld ist rein additiv und steht für alle bestehenden Organisationen auf „aus“ (auch in der Datenbank,
``db_default``): Ein Rückfall auf das vorige Image schreibt neue Organisationen weiter ohne die Spalte.
Eingeschaltet wird nur die Demo-Organisation (Kennung aus ``setup_demo_environment``), damit die neue
Oberfläche nach dem Deploy zuerst dort geprüft werden kann. Das ist idempotent; der Rückweg ändert nichts,
weil das Feld mit dem Rückbau der Spalte ohnehin verschwindet. Keine anderen Daten werden berührt.
"""

from django.db import migrations, models

#: Kennung der Demo-Organisation (apps/common/management/commands/setup_demo_environment.py, DEMO_ORG_SLUG)
DEMO_ORG_SLUG = "musterfraktion-demo"


def demo_einschalten(apps, schema_editor):
    Organization = apps.get_model("tenants", "Organization")
    Organization.objects.filter(slug=DEMO_ORG_SLUG).update(work_new_design=True)


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0025_recht_dokumente_ehemaliger_mitglieder"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="work_new_design",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text=(
                    "Schaltet für diese Organisation den neuen Rahmen von Work ein (Seitenleiste mit sechs Bereichen, "
                    "Suche in der Kopfzeile, Leiste unten am Handy). Aus: bisheriger Rahmen. Daten ändern sich nicht."
                ),
                verbose_name="Neues Erscheinungsbild in Work",
            ),
        ),
        migrations.RunPython(demo_einschalten, migrations.RunPython.noop),
    ]

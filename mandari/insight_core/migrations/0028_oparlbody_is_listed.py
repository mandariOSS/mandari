# SPDX-License-Identifier: AGPL-3.0-or-later

from django.db import migrations, models

# Slug der Demo-Kommune aus setup_demo_environment
DEMO_BODY_SLUG = "musterstadt-demo"


def hide_demo_body(apps, schema_editor):
    OParlBody = apps.get_model("insight_core", "OParlBody")
    OParlBody.objects.filter(slug=DEMO_BODY_SLUG).update(is_listed=False)


class Migration(migrations.Migration):
    dependencies = [
        ("insight_core", "0027_schema_contract_ingestor_defaults"),
    ]

    operations = [
        migrations.AddField(
            model_name="oparlbody",
            name="is_listed",
            field=models.BooleanField(
                db_default=True,
                default=True,
                help_text=(
                    "Kommune erscheint in Kommunenauswahl, Übersichten, Sitemaps und öffentlichen Listen. "
                    "Ausgeschaltet bleibt sie per direkter URL erreichbar (z. B. Demo-Kommune)."
                ),
                verbose_name="In Listen anzeigen",
            ),
        ),
        migrations.RunPython(hide_demo_body, migrations.RunPython.noop),
    ]

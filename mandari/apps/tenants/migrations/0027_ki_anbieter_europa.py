# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eigene KI-Konfiguration der Organisation nur noch mit freigegebenen EU-Endpunkten (Issue #950).

1. Die Anbieterauswahl kennt „Plattform-Einstellung“ (Standard), die Vorlagen mit Verarbeitung in Europa und
   den eigenen Endpunkt. Organisationen mit „nebius“ oder „ovh“ stehen danach auf „Plattform-Einstellung“;
   mit eigenem Schlüssel ist ihre KI dann aus, bis jemand einen freigegebenen Anbieter wählt. „ovh“ wird
   zurückgesetzt, obwohl es wieder eine Vorlage dieses Namens gibt: Die frühere Einstellung ist nie gegen die
   neue Vorlage geprüft worden. Eine eingetragene Basis-URL bleibt stehen, wirkt aber nur, wenn ihr Host in
   KI_ERLAUBTE_HOSTS steht.
2. Neue Felder Anzeigename und Verarbeitungsort (Pflicht beim eigenen Endpunkt), mit Datenbank-
   Standardwert, damit ein älteres Image nach einem Rückfall weiterläuft.

Rückwärts ändert sich an den Daten nichts.
"""

from django.db import migrations, models

#: Frühere Anbieter; „ovh“ trotz neuer Vorlage (frühere Einstellung nie gegen sie geprüft)
ENTFALLENE_ANBIETER = ("nebius", "ovh")


def anbieter_umstellen(apps, schema_editor):
    Organization = apps.get_model("tenants", "Organization")
    Organization.objects.filter(ai_provider__in=ENTFALLENE_ANBIETER).update(ai_provider="")


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0026_work_neues_erscheinungsbild"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="ai_anzeigename",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Name des Anbieters in Hinweisen. Leer: Name der Vorlage; beim eigenen Endpunkt Pflicht.",
                max_length=100,
                verbose_name="KI-Anzeigename",
            ),
        ),
        migrations.AddField(
            model_name="organization",
            name="ai_verarbeitungsort",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text=(
                    "Etwa: Rechenzentren in Deutschland (EU). Leer: Angabe der Vorlage; beim eigenen Endpunkt Pflicht."
                ),
                max_length=200,
                verbose_name="KI-Verarbeitungsort",
            ),
        ),
        migrations.AlterField(
            model_name="organization",
            name="ai_base_url",
            field=models.URLField(
                blank=True,
                help_text=(
                    "Leer: Basis-URL der Vorlage. Beim eigenen Endpunkt Pflicht. Nur https, nur Hosts aus "
                    "KI_ERLAUBTE_HOSTS."
                ),
                verbose_name="KI API Base URL",
            ),
        ),
        migrations.AlterField(
            model_name="organization",
            name="ai_model",
            field=models.CharField(
                default="openai/gpt-oss-120b",
                help_text="Modellname beim gewählten Anbieter (nur mit eigenem KI API Key).",
                max_length=100,
                verbose_name="KI-Modell",
            ),
        ),
        migrations.AlterField(
            model_name="organization",
            name="ai_provider",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Plattform-Einstellung"),
                    ("stackit", "STACKIT AI Model Serving"),
                    ("ionos", "IONOS AI Model Hub"),
                    ("scaleway", "Scaleway Generative APIs"),
                    ("ovh", "OVHcloud AI Endpoints"),
                    ("deutschlandgpt", "DeutschlandGPT Platform API"),
                    ("eigener", "Eigener OpenAI-kompatibler Endpunkt"),
                ],
                default="",
                help_text=(
                    "Nur mit eigenem KI API Key: Vorlage oder eigener Endpunkt. Ohne eigenen Key gelten die "
                    "KI-Einstellungen der Plattform."
                ),
                max_length=20,
                verbose_name="KI-Anbieter",
            ),
        ),
        migrations.RunPython(anbieter_umstellen, migrations.RunPython.noop),
    ]

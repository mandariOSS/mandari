# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Anbieter frei konfigurierbar, nur freigegebene EU-Endpunkte (Issue #950).

1. KI-Einstellungen: neue Felder für Bürgerportal (Schalter, Modell, Antwortlänge), Ausweichmodell,
   Anzeigename und Verarbeitungsort. Alle mit Datenbank-Standardwert, damit ein älteres Image nach einem
   Rückfall auf dieselbe Datenbank weiterläuft (es legt die Zeile ohne diese Spalten an).
2. Die Anbieterauswahl kennt nur noch Vorlagen mit Verarbeitung in Europa und den eigenen Endpunkt;
   Standard ist „nicht eingerichtet“. Frühere Anbieter (nebius, anthropic, openai, mistral, ovh) werden zu
   „nicht eingerichtet“; die KI bleibt dann aus, bis jemand einen freigegebenen Anbieter wählt. Eine
   eingetragene Basis-URL bleibt stehen, wirkt aber nur, wenn ihr Host in KI_ERLAUBTE_HOSTS steht.
3. Der frühere Nebius-Schlüssel der Systemeinstellungen wird geleert (beide Spalten). Die Spalten bleiben,
   damit ein älteres Image weiterläuft; sie entfallen mit einer Folgeversion.

Rückwärts ändert sich an den Daten nichts (geleerte Schlüssel lassen sich nicht wiederherstellen).
"""

import contextlib

from django.db import migrations, models

#: Anbieter, die es nicht mehr gibt (Verarbeitung nicht zugesichert in Europa oder ohne Vorlage)
ENTFALLENE_ANBIETER = ("nebius", "anthropic", "openai", "mistral", "ovh")
#: Cache-Schlüssel der Singletons (AISettings.CACHE_KEY, SiteSettings.CACHE_KEY)
CACHE_KEYS = ("ai_settings", "site_settings")


def _vergiss_zwischenspeicher() -> None:
    """Zwischengespeicherte Instanzen tragen noch die alten Werte; ohne Cache verfallen sie nach 5 Minuten."""
    from django.core.cache import cache

    with contextlib.suppress(Exception):
        cache.delete_many(list(CACHE_KEYS))


def anbieter_umstellen(apps, schema_editor):
    AISettings = apps.get_model("common", "AISettings")
    SiteSettings = apps.get_model("common", "SiteSettings")
    AISettings.objects.filter(provider__in=ENTFALLENE_ANBIETER).update(provider="")
    SiteSettings.objects.update(nebius_api_key_encrypted=None, nebius_api_key_legacy="")
    _vergiss_zwischenspeicher()


class Migration(migrations.Migration):
    dependencies = [
        ("common", "0010_postausgang_mail"),
    ]

    operations = [
        migrations.AddField(
            model_name="aisettings",
            name="anzeigename",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Name des Anbieters in Hinweisen und in der Einwilligung. Leer: Name der Vorlage.",
                max_length=100,
                verbose_name="Anzeigename",
            ),
        ),
        migrations.AddField(
            model_name="aisettings",
            name="fallback_model",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text=(
                    "Optional. Wird im Bürgerportal einmal versucht, wenn das Modell nicht antwortet (HTTP 404, 408, "
                    "429, 5xx oder Zeitüberschreitung); gleicher Endpunkt, gleicher Schlüssel."
                ),
                max_length=100,
                verbose_name="Ausweichmodell",
            ),
        ),
        migrations.AddField(
            model_name="aisettings",
            name="insight_enabled",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="Zusammenfassungen, KI-Assistent und KI-Verortung im Bürgerportal.",
                verbose_name="KI im Bürgerportal aktiviert",
            ),
        ),
        migrations.AddField(
            model_name="aisettings",
            name="insight_max_output_tokens",
            field=models.PositiveIntegerField(
                db_default=16000,
                default=16000,
                help_text="Obergrenze für die Antwortlänge je KI-Aufruf im Bürgerportal (Zusammenfassungen, Chat).",
                verbose_name="Max. Output-Tokens (Bürgerportal)",
            ),
        ),
        migrations.AddField(
            model_name="aisettings",
            name="insight_model",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Leer: dasselbe Modell wie in Work.",
                max_length=100,
                verbose_name="Modell im Bürgerportal",
            ),
        ),
        migrations.AddField(
            model_name="aisettings",
            name="verarbeitungsort",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Etwa: Rechenzentren in Deutschland (EU). Leer: Angabe der Vorlage.",
                max_length=200,
                verbose_name="Verarbeitungsort",
            ),
        ),
        migrations.AlterField(
            model_name="aisettings",
            name="base_url",
            field=models.URLField(
                blank=True,
                help_text=(
                    "Leer: Basis-URL der Vorlage. Beim eigenen Endpunkt Pflicht, etwa https://…/v1. Nur https, nur "
                    "Hosts aus KI_ERLAUBTE_HOSTS."
                ),
                verbose_name="API Base URL",
            ),
        ),
        migrations.AlterField(
            model_name="aisettings",
            name="enabled",
            field=models.BooleanField(
                default=True,
                help_text="Schreibhilfe und Co-Editor in Work. Aus: Die KI in Work ist aus, auch mit Schlüssel.",
                verbose_name="KI in Work aktiviert",
            ),
        ),
        migrations.AlterField(
            model_name="aisettings",
            name="max_output_tokens",
            field=models.PositiveIntegerField(
                default=2000,
                help_text="Obergrenze für die Antwortlänge je KI-Aufruf in Work.",
                verbose_name="Max. Output-Tokens (Work)",
            ),
        ),
        migrations.AlterField(
            model_name="aisettings",
            name="model_name",
            field=models.CharField(
                default="openai/gpt-oss-120b",
                help_text="Modellname beim gewählten Anbieter, für Work und als Standard für das Bürgerportal.",
                max_length=100,
                verbose_name="Modell",
            ),
        ),
        migrations.AlterField(
            model_name="aisettings",
            name="provider",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Nicht eingerichtet (KI aus)"),
                    ("stackit", "STACKIT AI Model Serving"),
                    ("ionos", "IONOS AI Model Hub"),
                    ("scaleway", "Scaleway Generative APIs"),
                    ("eigener", "Eigener OpenAI-kompatibler Endpunkt"),
                ],
                default="",
                help_text=(
                    "Vorlage eines OpenAI-kompatiblen Anbieters oder eigener Endpunkt. Wirksam nur, wenn der Host in "
                    "KI_ERLAUBTE_HOSTS steht."
                ),
                max_length=20,
                verbose_name="KI-Anbieter",
            ),
        ),
        migrations.RunPython(anbieter_umstellen, migrations.RunPython.noop),
    ]

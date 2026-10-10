# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Migrationen common/0011 und tenants/0027 (Issue #950).

Frühere Anbieter werden zu „nicht eingerichtet“ bzw. „Plattform-Einstellung“, die Spalten des früheren
Nebius-Schlüssels werden geleert, freigegebene Anbieter bleiben, und der Rückweg läuft (ohne Datenänderung).
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps as django_apps
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.common.models import AISettings, SiteSettings, _encrypt_platform_secret
from apps.tenants.models import Organization

COMMON = importlib.import_module("apps.common.migrations.0011_ki_anbieter_europa")
TENANTS = importlib.import_module("apps.tenants.migrations.0027_ki_anbieter_europa")
VORHER = [("common", "0010_postausgang_mail"), ("tenants", "0026_work_neues_erscheinungsbild")]
NACHHER = [("common", "0011_ki_anbieter_europa"), ("tenants", "0027_ki_anbieter_europa")]


@pytest.fixture(autouse=True)
def _frischer_cache() -> None:
    """
    Kein zwischengespeichertes Objekt aus einem früheren Test: Dessen Zeile ist längst zurückgerollt, und
    ``get_settings()`` legte sie aus dem Cache heraus nicht neu an (``DoesNotExist`` je nach Testreihenfolge).
    """
    cache.delete_many([AISettings.CACHE_KEY, SiteSettings.CACHE_KEY])


def _nebius_spalten() -> tuple[bytes | None, str]:
    wert, alt = SiteSettings.objects.values_list("nebius_api_key_encrypted", "nebius_api_key_legacy").get(pk=1)
    return (bytes(wert) if wert is not None else None), alt


@pytest.mark.django_db
class TestDatenumstellung:
    @pytest.mark.parametrize("anbieter", ["nebius", "anthropic", "openai", "mistral", "ovh"])
    def test_fruehere_anbieter_werden_nicht_eingerichtet(self, anbieter: str) -> None:
        AISettings.get_settings()
        AISettings.objects.filter(pk=1).update(provider=anbieter, base_url="https://beispiel.example/v1")
        COMMON.anbieter_umstellen(django_apps, None)
        provider, base_url = AISettings.objects.values_list("provider", "base_url").get(pk=1)
        assert provider == ""
        assert base_url == "https://beispiel.example/v1"  # bleibt stehen, wirkt aber nicht

    @pytest.mark.parametrize("anbieter", ["stackit", "ionos", "scaleway", "deutschlandgpt", "eigener", ""])
    def test_freigegebene_anbieter_bleiben(self, anbieter: str) -> None:
        AISettings.get_settings()
        AISettings.objects.filter(pk=1).update(provider=anbieter)
        COMMON.anbieter_umstellen(django_apps, None)
        assert AISettings.objects.values_list("provider", flat=True).get(pk=1) == anbieter

    def test_nebius_spalten_werden_geleert(self) -> None:
        SiteSettings.get_settings()
        SiteSettings.objects.filter(pk=1).update(
            nebius_api_key_encrypted=_encrypt_platform_secret("nebius-testwert"), nebius_api_key_legacy="klartext"
        )
        cache.set(SiteSettings.CACHE_KEY, SiteSettings.objects.get(pk=1))
        COMMON.anbieter_umstellen(django_apps, None)
        assert _nebius_spalten() == (None, "")
        assert cache.get(SiteSettings.CACHE_KEY) is None

    @pytest.mark.parametrize("anbieter", ["nebius", "ovh"])
    def test_organisation_frueherer_anbieter_wird_plattform(self, org: Organization, anbieter: str) -> None:
        """Auch „ovh“: Die frühere Einstellung wird nicht stillschweigend zur neuen Vorlage gleichen Namens."""
        Organization.objects.filter(pk=org.pk).update(ai_provider=anbieter)
        TENANTS.anbieter_umstellen(django_apps, None)
        assert Organization.objects.values_list("ai_provider", flat=True).get(pk=org.pk) == ""

    def test_organisation_ionos_bleibt(self, org: Organization) -> None:
        Organization.objects.filter(pk=org.pk).update(ai_provider="ionos")
        TENANTS.anbieter_umstellen(django_apps, None)
        assert Organization.objects.values_list("ai_provider", flat=True).get(pk=org.pk) == "ionos"


@pytest.mark.django_db(transaction=True)
def test_migrationen_vor_und_zurueck(org: Organization) -> None:
    """Echter Lauf: Stand vor den Migrationen anlegen, vorwärts umstellen, Rückweg ohne Fehler."""
    executor = MigrationExecutor(connection)
    executor.migrate(VORHER)
    alt = executor.loader.project_state(VORHER).apps
    alt.get_model("common", "AISettings").objects.update_or_create(pk=1, defaults={"provider": "nebius"})
    alt.get_model("common", "SiteSettings").objects.update_or_create(
        pk=1, defaults={"nebius_api_key_encrypted": b"geheimtext", "nebius_api_key_legacy": "klartext"}
    )
    alt.get_model("tenants", "Organization").objects.filter(pk=org.pk).update(ai_provider="nebius")

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(NACHHER)
        cache.clear()
        assert AISettings.objects.values_list("provider", flat=True).get(pk=1) == ""
        ki = AISettings.objects.get(pk=1)
        assert (ki.insight_enabled, ki.insight_max_output_tokens, ki.fallback_model) == (False, 8192, "")
        assert _nebius_spalten() == (None, "")
        assert Organization.objects.values_list("ai_provider", flat=True).get(pk=org.pk) == ""

        executor = MigrationExecutor(connection)
        executor.migrate(VORHER)
        alt = executor.loader.project_state(VORHER).apps
        assert alt.get_model("common", "AISettings").objects.values_list("provider", flat=True).get(pk=1) == ""
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        cache.clear()

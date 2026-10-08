# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin-Formulare der KI-Konfiguration (Issue #950): KI-Einstellungen und Organisation.

Nur Anbieter mit Verarbeitung in Europa in der Auswahl, keine Adresse außerhalb von ``KI_ERLAUBTE_HOSTS``, beim
eigenen Endpunkt Basis-URL, Anzeigename und Verarbeitungsort als Pflicht.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.forms.models import model_to_dict
from django.test import Client, RequestFactory, override_settings

from apps.common.admin import AISettingsAdminForm
from apps.common.models import AISettings
from apps.tenants.models import Organization

pytestmark = pytest.mark.django_db

STACKIT = "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1"
ENTFALLEN = ("nebius", "anthropic", "openai", "mistral", "ovh")


@pytest.fixture(autouse=True)
def _frischer_cache() -> None:
    cache.delete(AISettings.CACHE_KEY)


def _ki_formular(**werte: Any) -> Any:
    ki = AISettings.get_settings()
    daten = {name: wert for name, wert in model_to_dict(ki).items() if wert is not None}
    daten.update({"api_key": "", **werte})
    return cast(Any, AISettingsAdminForm)(data=daten, instance=ki)


def _org_formular(org: Organization, **werte: Any) -> Any:
    model_admin = admin.site._registry[Organization]
    request = RequestFactory().get("/admin/")
    request.user = cast(Any, AnonymousUser())
    form_klasse = model_admin.get_form(request, org)
    daten = {
        name: ([getattr(o, "pk", o) for o in wert] if isinstance(wert, list) else wert)
        for name, wert in model_to_dict(org, fields=list(form_klasse.base_fields)).items()
        if wert is not None
    }
    daten.update({"ai_api_key": "", **werte})
    return form_klasse(data=daten, instance=org)


class TestKiEinstellungen:
    def test_auswahl_nur_europa(self) -> None:
        auswahl = {key for key, _label in cast(Any, AISettingsAdminForm)().fields["provider"].choices}
        assert auswahl == {"", "stackit", "ionos", "scaleway", "eigener"}
        assert not auswahl & set(ENTFALLEN)

    def test_stackit_gueltig(self) -> None:
        formular = _ki_formular(provider="stackit", base_url="")
        assert formular.is_valid(), formular.errors

    def test_gesperrte_adresse_formfehler(self) -> None:
        formular = _ki_formular(
            provider="eigener", base_url="https://api.openai.com/v1", anzeigename="X", verarbeitungsort="Y"
        )
        assert not formular.is_valid()
        assert "KI_ERLAUBTE_HOSTS" in str(formular.errors["base_url"])

    def test_vorlage_ausserhalb_der_positivliste(self) -> None:
        formular = _ki_formular(provider="ionos", base_url="")
        assert not formular.is_valid()
        assert "KI_ERLAUBTE_HOSTS" in str(formular.errors["provider"])
        label = dict(formular.fields["provider"].choices)["ionos"]
        assert "nicht freigegeben" in label

    @override_settings(KI_ERLAUBTE_HOSTS=["openai.inference.de-txl.ionos.com"])
    def test_vorlage_mit_freigabe(self) -> None:
        assert _ki_formular(provider="ionos", base_url="").is_valid()

    @pytest.mark.parametrize("fehlt", ["anzeigename", "verarbeitungsort", "base_url"])
    def test_eigener_endpunkt_pflichtangaben(self, fehlt: str) -> None:
        werte = {"provider": "eigener", "base_url": STACKIT, "anzeigename": "Eigen", "verarbeitungsort": "Berlin"}
        werte[fehlt] = ""
        formular = _ki_formular(**werte)
        assert not formular.is_valid()
        assert fehlt in formular.errors

    def test_eigener_endpunkt_wird_normalisiert(self) -> None:
        formular = _ki_formular(
            provider="eigener", base_url=STACKIT + "/", anzeigename="Eigen", verarbeitungsort="Berlin"
        )
        assert formular.is_valid(), formular.errors
        assert formular.save().base_url == STACKIT

    def test_seite_nennt_freigegebene_hosts(self, admin_client: Client) -> None:
        ki = AISettings.get_settings()
        inhalt = admin_client.get(f"/admin/common/aisettings/{ki.pk}/change/").content.decode()
        assert "api.openai-compat.model-serving.eu01.onstackit.cloud" in inhalt
        assert "Nebius" not in inhalt


class TestOrganisation:
    def test_auswahl_nur_europa(self, org: Organization) -> None:
        auswahl = {key for key, _label in _org_formular(org).fields["ai_provider"].choices}
        assert auswahl == {"", "stackit", "ionos", "scaleway", "eigener"}

    def test_gesperrte_adresse_formfehler(self, org: Organization) -> None:
        formular = _org_formular(
            org,
            ai_provider="eigener",
            ai_base_url="https://api.tokenfactory.nebius.com/v1/",
            ai_anzeigename="X",
            ai_verarbeitungsort="Y",
        )
        formular.is_valid()
        assert "KI_ERLAUBTE_HOSTS" in str(formular.errors.get("ai_base_url"))

    @pytest.mark.parametrize("fehlt", ["ai_anzeigename", "ai_verarbeitungsort"])
    def test_eigener_endpunkt_pflichtangaben(self, org: Organization, fehlt: str) -> None:
        werte = {
            "ai_provider": "eigener",
            "ai_base_url": STACKIT,
            "ai_anzeigename": "Eigen",
            "ai_verarbeitungsort": "Berlin",
        }
        werte[fehlt] = ""
        formular = _org_formular(org, **werte)
        formular.is_valid()
        assert fehlt in formular.errors

    def test_freigegebene_vorlage_ohne_fehler_an_den_ki_feldern(self, org: Organization) -> None:
        formular = _org_formular(org, ai_provider="stackit", ai_base_url="")
        formular.is_valid()
        assert not {"ai_provider", "ai_base_url", "ai_anzeigename", "ai_verarbeitungsort"} & set(formular.errors)

    def test_unveraenderte_ki_felder_werden_nicht_geprueft(self, org: Organization) -> None:
        """Eine inzwischen gesperrte, gespeicherte Adresse blockiert andere Änderungen an der Organisation nicht."""
        Organization.objects.filter(pk=org.pk).update(ai_provider="ionos")
        org.refresh_from_db()
        formular = _org_formular(org, description="Neue Beschreibung")
        formular.is_valid()
        assert "ai_provider" not in formular.errors

    def test_eigener_key_braucht_einen_anbieter(self, org: Organization) -> None:
        formular = _org_formular(org, ai_provider="", ai_api_key="org-testschluessel")
        formular.is_valid()
        assert "ai_provider" in formular.errors

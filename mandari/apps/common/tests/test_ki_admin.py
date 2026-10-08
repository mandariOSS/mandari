# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin-Formulare der KI-Konfiguration (Issue #950): KI-Einstellungen und Organisation.

Nur Anbieter mit Verarbeitung in Europa in der Auswahl, keine Adresse außerhalb von ``KI_ERLAUBTE_HOSTS``, beim
eigenen Endpunkt Basis-URL, Anzeigename und Verarbeitungsort als Pflicht. Eine Vorlage gilt nur für ihren Host; ein
gespeicherter Schlüssel geht nach einem Anbieterwechsel nie an den neuen Anbieter, und ein Schlüssel ohne Anbieter
lässt sich löschen statt die KI stillschweigend abzuschalten.
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
STACKIT_HOST = "api.openai-compat.model-serving.eu01.onstackit.cloud"
IONOS_HOST = "openai.inference.de-txl.ionos.com"
IONOS = f"https://{IONOS_HOST}/v1"
ENTFALLEN = ("nebius", "anthropic", "openai", "mistral", "ovh")
SCHLUESSEL = "admin-testschluessel-geheim-0123456789"
WECHSEL = "Bei einem Anbieterwechsel bitte den Schlüssel des neuen Anbieters eintragen."


@pytest.fixture(autouse=True)
def _frischer_cache() -> None:
    cache.delete(AISettings.CACHE_KEY)


def _ki_formular(**werte: Any) -> Any:
    ki = AISettings.get_settings()
    daten = {name: wert for name, wert in model_to_dict(ki).items() if wert is not None}
    daten.update({"api_key": "", **werte})
    return cast(Any, AISettingsAdminForm)(data=daten, instance=ki)


def _ki_mit_schluessel(provider: str, **werte: Any) -> AISettings:
    """Gespeicherte KI-Einstellungen mit Schlüssel, ohne Formularprüfung (Stand aus der Datenbank)."""
    ki = AISettings.get_settings()
    ki.provider = provider
    for name, wert in werte.items():
        setattr(ki, name, wert)
    ki.set_api_key(SCHLUESSEL)
    ki.save()
    cache.delete(AISettings.CACHE_KEY)
    return ki


def _org_mit_schluessel(org: Organization, provider: str, **werte: Any) -> Organization:
    """Organisation mit eigenem Schlüssel, ohne Formularprüfung (Stand aus der Datenbank)."""
    org.ai_provider = provider
    for name, wert in werte.items():
        setattr(org, name, wert)
    org.set_ai_api_key(SCHLUESSEL)
    org.save()
    org.refresh_from_db()
    return org


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

    @override_settings(KI_ERLAUBTE_HOSTS=[STACKIT_HOST, IONOS_HOST])
    def test_anbieterwechsel_verlangt_neuen_schluessel(self) -> None:
        _ki_mit_schluessel("stackit")
        formular = _ki_formular(provider="ionos", base_url="")
        assert not formular.is_valid()
        assert WECHSEL in str(formular.errors["api_key"])
        # Mit dem Schlüssel des neuen Anbieters gültig
        assert _ki_formular(provider="ionos", base_url="", api_key="neuer-schluessel").is_valid()

    @override_settings(KI_ERLAUBTE_HOSTS=[STACKIT_HOST, IONOS_HOST])
    def test_wechsel_ueber_die_basis_url_verlangt_neuen_schluessel(self) -> None:
        _ki_mit_schluessel("stackit")
        formular = _ki_formular(provider="eigener", base_url=IONOS, anzeigename="IONOS", verarbeitungsort="Berlin")
        assert not formular.is_valid()
        assert WECHSEL in str(formular.errors["api_key"])

    def test_gleicher_host_behaelt_den_schluessel(self) -> None:
        _ki_mit_schluessel("stackit")
        assert _ki_formular(model_name="anderes-modell").is_valid()
        formular = _ki_formular(provider="eigener", base_url=STACKIT, anzeigename="Eigen", verarbeitungsort="Berlin")
        assert formular.is_valid(), formular.errors
        assert formular.save().get_api_key() == SCHLUESSEL

    def test_nach_der_migration_neuer_schluessel(self) -> None:
        """Migration common/0011 setzt den Anbieter auf leer und lässt den früheren Schlüssel stehen."""
        _ki_mit_schluessel("")
        formular = _ki_formular(provider="stackit", base_url="")
        assert not formular.is_valid()
        assert WECHSEL in str(formular.errors["api_key"])

    def test_schluessel_ohne_anbieter(self) -> None:
        _ki_mit_schluessel("")
        formular = _ki_formular(provider="")
        assert not formular.is_valid()
        assert "Anbieter wählen oder den API Key löschen." in str(formular.errors["provider"])
        assert not _ki_formular(provider="", api_key="neuer-schluessel").is_valid()

    def test_schluessel_loeschen(self) -> None:
        _ki_mit_schluessel("")
        formular = _ki_formular(provider="", api_key_loeschen="on")
        assert formular.is_valid(), formular.errors
        ki = formular.save()
        ki.refresh_from_db()
        assert ki.api_key_encrypted is None and ki.get_api_key() == ""

    def test_loeschen_und_neuer_schluessel_zugleich(self) -> None:
        _ki_mit_schluessel("stackit")
        formular = _ki_formular(api_key="neuer-schluessel", api_key_loeschen="on")
        assert not formular.is_valid()
        assert "api_key_loeschen" in formular.errors

    @override_settings(KI_ERLAUBTE_HOSTS=[STACKIT_HOST, IONOS_HOST])
    def test_vorlage_mit_fremdem_host(self) -> None:
        formular = _ki_formular(provider="stackit", base_url=IONOS)
        assert not formular.is_valid()
        assert "„Eigener Endpunkt“ wählen" in str(formular.errors["base_url"])

    def test_vorlage_mit_anderem_pfad(self) -> None:
        formular = _ki_formular(provider="stackit", base_url=STACKIT + "/v2/")
        assert formular.is_valid(), formular.errors
        assert formular.save().base_url == STACKIT + "/v2"

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

    @override_settings(KI_ERLAUBTE_HOSTS=[STACKIT_HOST, IONOS_HOST])
    def test_anbieterwechsel_verlangt_neuen_schluessel(self, org: Organization) -> None:
        org = _org_mit_schluessel(org, "stackit")
        formular = _org_formular(org, ai_provider="ionos", ai_base_url="")
        formular.is_valid()
        assert WECHSEL in str(formular.errors.get("ai_api_key"))
        formular = _org_formular(org, ai_provider="ionos", ai_base_url="", ai_api_key="neuer-schluessel")
        formular.is_valid()
        assert not {"ai_api_key", "ai_provider", "ai_base_url"} & set(formular.errors)

    def test_nach_der_migration_neuer_schluessel(self, org: Organization) -> None:
        """Migration tenants/0027 setzt „nebius“ auf leer und lässt den eigenen Schlüssel stehen."""
        org = _org_mit_schluessel(org, "")
        formular = _org_formular(org, ai_provider="stackit", ai_base_url="")
        formular.is_valid()
        assert WECHSEL in str(formular.errors.get("ai_api_key"))

    def test_gleicher_anbieter_behaelt_den_schluessel(self, org: Organization) -> None:
        org = _org_mit_schluessel(org, "stackit")
        formular = _org_formular(org, ai_model="anderes-modell")
        formular.is_valid()
        assert not {"ai_api_key", "ai_provider", "ai_base_url"} & set(formular.errors)

    def test_eigener_key_ohne_anbieter_ist_loeschbar(self, org: Organization) -> None:
        org = _org_mit_schluessel(org, "")
        formular = _org_formular(org)
        formular.is_valid()
        assert "Anbieter wählen oder eigenen Key löschen." in str(formular.errors.get("ai_provider"))

        # Inaktiv, damit Kommune und Partei (Pflicht bei aktiven Organisationen) hier keine Rolle spielen
        formular = _org_formular(org, ai_api_key_loeschen="on", is_active=False, settings="{}")
        assert formular.is_valid(), formular.errors
        formular.save()
        org.refresh_from_db()
        assert org.ai_api_key_encrypted is None and org.get_ai_api_key() == ""

    def test_feld_zum_loeschen_im_admin(self, org: Organization) -> None:
        assert "ai_api_key_loeschen" in _org_formular(org).fields
        fieldsets = cast(Any, admin.site._registry[Organization]).fieldsets
        felder = [feld for _name, optionen in fieldsets for feld in optionen["fields"]]
        assert "ai_api_key_loeschen" in felder

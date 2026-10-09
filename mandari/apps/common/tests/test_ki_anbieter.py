# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zentrale KI-Konfiguration mit Positivliste erlaubter EU-Hosts (Issue #950).

- ``pruefe_basis_url`` lässt nur ``https``-Adressen mit Host aus ``KI_ERLAUBTE_HOSTS`` zu: exakter Vergleich,
  keine Tricks über Suffix, Präfix, Zugangsdaten in der Adresse oder anderen Port.
- ``KI_ERLAUBTE_HOSTS`` ersetzt den Standard; leer gilt der Standard.
- ``endpunkt_fuer_work``/``endpunkt_fuer_insight`` lösen aus den Einstellungen auf, prüfen jede Adresse bei
  jedem Aufruf und lesen nie einen Schlüssel aus der Umgebung.
- ``KiEndpunkt`` nennt den Schlüssel weder in ``repr`` noch in ``str``.
- Eine Vorlage gilt nur für ihren Host (Pfad darf abweichen), auch bei Werten direkt aus der Datenbank.
- Die Einwilligungskennung ändert sich mit Anbieter, Host, Anzeigename und Verarbeitungsort.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import override_settings
from mandari_dokumente import ki_hosts

from apps.common import ki_anbieter
from apps.common.ki_anbieter import (
    ANBIETER_VORLAGEN,
    LAENGENLIMIT_AUSGABE,
    LAENGENLIMIT_KONTEXT,
    KiEndpunkt,
    KiHinweis,
    endpunkt_fuer_insight,
    endpunkt_fuer_work,
    laengenlimit_hinweis,
    pruefe_basis_url,
)
from apps.common.models import AISettings

STACKIT = "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1"
IONOS_HOST = "openai.inference.de-txl.ionos.com"
IONOS = f"https://{IONOS_HOST}/v1"
STACKIT_HOST = "api.openai-compat.model-serving.eu01.onstackit.cloud"
SCHLUESSEL = "ki-testschluessel-geheim-0123456789"

ABGELEHNT = [
    pytest.param("http://api.openai-compat.model-serving.eu01.onstackit.cloud/v1", id="http"),
    pytest.param("https://api.tokenfactory.nebius.com/v1/", id="nebius"),
    pytest.param("https://api.anthropic.com/v1/", id="anthropic"),
    pytest.param("https://api.openai.com/v1/", id="openai"),
    pytest.param("https://api.mistral.ai/v1/", id="mistral"),
    pytest.param("https://oai.endpoints.kepler.ai.cloud.ovh.net/v1", id="ovh"),
    pytest.param("https://api.openai-compat.model-serving.eu01.onstackit.cloud.example.com/v1", id="suffix"),
    pytest.param("https://evil.api.openai-compat.model-serving.eu01.onstackit.cloud/v1", id="praefix"),
    pytest.param("https://api.openai-compat.model-serving.eu01.onstackit.cloud@evil.example/v1", id="userinfo"),
    pytest.param("https://api.openai-compat.model-serving.eu01.onstackit.cloud:8443/v1", id="port-8443"),
    pytest.param("https://api.openai-compat.model-serving.eu01.onstackit.cloud\\@evil.example/v1", id="backslash"),
    pytest.param("https://api.openai-compat.model-serving.eu01.onstackit.cloud /v1", id="leerraum"),
    pytest.param("", id="leer"),
]


@pytest.fixture(autouse=True)
def _frischer_cache() -> None:
    cache.delete(AISettings.CACHE_KEY)


def _ki(**werte: Any) -> AISettings:
    """KI-Einstellungen mit Schlüssel speichern (Standard: STACKIT, Work und Bürgerportal an)."""
    ki = AISettings.get_settings()
    felder = {"provider": "stackit", "enabled": True, "insight_enabled": True, "model_name": "modell-a", **werte}
    for name, wert in felder.items():
        setattr(ki, name, wert)
    ki.set_api_key(SCHLUESSEL)
    ki.save()
    return ki


# =============================================================================
# Positivliste
# =============================================================================


class TestPositivliste:
    def test_stackit_ist_erlaubt(self) -> None:
        assert pruefe_basis_url(STACKIT) == STACKIT
        assert pruefe_basis_url(STACKIT + "/") == STACKIT

    def test_port_443_und_schreibweise(self) -> None:
        url = "HTTPS://API.OPENAI-COMPAT.MODEL-SERVING.EU01.ONSTACKIT.CLOUD.:443/v1"
        assert pruefe_basis_url(url) == STACKIT

    @pytest.mark.parametrize("url", ABGELEHNT)
    def test_abgelehnt(self, url: str) -> None:
        with pytest.raises(ValidationError):
            pruefe_basis_url(url)
        assert not ki_hosts.ist_erlaubter_host(url, ki_hosts.STANDARD_ERLAUBTE_HOSTS)

    def test_hinweis_nennt_die_positivliste(self) -> None:
        with pytest.raises(ValidationError) as fehler:
            pruefe_basis_url("https://api.openai.com/v1/")
        assert "nicht freigegebener Endpunkt (KI_ERLAUBTE_HOSTS)" in str(fehler.value)

    def test_abfrage_und_fragment_abgelehnt(self) -> None:
        for url in (STACKIT + "?x=1", STACKIT + "#teil"):
            with pytest.raises(ValidationError):
                pruefe_basis_url(url)

    @override_settings(KI_ERLAUBTE_HOSTS=[IONOS_HOST])
    def test_umgebung_ersetzt_den_standard(self) -> None:
        assert pruefe_basis_url(IONOS) == IONOS
        with pytest.raises(ValidationError):
            pruefe_basis_url(STACKIT)
        assert ki_anbieter.vorlage_nutzbar("ionos") and not ki_anbieter.vorlage_nutzbar("stackit")

    @override_settings(KI_ERLAUBTE_HOSTS=[])
    def test_leer_gilt_der_standard(self) -> None:
        assert pruefe_basis_url(STACKIT) == STACKIT

    def test_aus_der_umgebungsvariable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KI_ERLAUBTE_HOSTS", f" {IONOS_HOST.upper()}. , ,api.scaleway.ai")
        assert ki_hosts.erlaubte_hosts_aus_umgebung() == (IONOS_HOST, "api.scaleway.ai")
        monkeypatch.setenv("KI_ERLAUBTE_HOSTS", " , ")
        assert ki_hosts.erlaubte_hosts_aus_umgebung() == ki_hosts.STANDARD_ERLAUBTE_HOSTS
        monkeypatch.delenv("KI_ERLAUBTE_HOSTS")
        assert ki_hosts.erlaubte_hosts_aus_umgebung() == ki_hosts.STANDARD_ERLAUBTE_HOSTS
        assert ki_hosts.erlaubte_hosts_aus_umgebung("") == ki_hosts.STANDARD_ERLAUBTE_HOSTS

    def test_keine_vorlage_fuer_anbieter_ausserhalb_europas(self) -> None:
        assert set(ANBIETER_VORLAGEN) == {"stackit", "ionos", "scaleway", "eigener"}
        for vorlage in ANBIETER_VORLAGEN.values():
            assert "nebius" not in vorlage.basis_url and "anthropic" not in vorlage.basis_url


# =============================================================================
# Endpunkt
# =============================================================================


class TestEndpunkt:
    def _endpunkt(self) -> KiEndpunkt:
        return KiEndpunkt(
            anbieter="stackit",
            anzeigename="STACKIT",
            verarbeitungsort="Deutschland",
            base_url=STACKIT,
            api_key=SCHLUESSEL,
            modell="m",
        )

    def test_schluessel_nie_in_repr_und_str(self) -> None:
        endpunkt = self._endpunkt()
        assert SCHLUESSEL not in repr(endpunkt) and SCHLUESSEL not in str(endpunkt)
        assert SCHLUESSEL not in repr(endpunkt.hinweis())
        assert "api.openai-compat.model-serving.eu01.onstackit.cloud" in repr(endpunkt)

    def test_adresse_der_chat_schnittstelle(self) -> None:
        assert self._endpunkt().chat_url == STACKIT + "/chat/completions"

    def test_einwilligungskennung_je_anbieter_host_name_und_ort(self) -> None:
        basis = KiHinweis(anbieter="stackit", anzeigename="STACKIT", verarbeitungsort="Deutschland", host="a.example")
        kennung = basis.einwilligungskennung
        for geaendert in (
            KiHinweis(anbieter="eigener", anzeigename="STACKIT", verarbeitungsort="Deutschland", host="a.example"),
            KiHinweis(anbieter="stackit", anzeigename="STACKIT", verarbeitungsort="Deutschland", host="b.example"),
            KiHinweis(anbieter="stackit", anzeigename="Anderer", verarbeitungsort="Deutschland", host="a.example"),
            KiHinweis(anbieter="stackit", anzeigename="STACKIT", verarbeitungsort="USA", host="a.example"),
        ):
            assert geaendert.einwilligungskennung != kennung
        assert KiHinweis(**vars(basis)).einwilligungskennung == kennung


class TestLaengenlimit:
    @pytest.mark.parametrize(
        "text",
        [
            '{"error": {"message": "max_tokens must be less than or equal to 8192"}}',
            '{"detail": "max_completion_tokens is too large"}',
            # vLLM: verlangte Antwortlänge passt nicht neben die Eingabe; senken hilft
            "'max_tokens' or 'max_completion_tokens' is too large: 16000. This model's maximum context length is "
            "131072 tokens and your request has 120000 input tokens (16000 > 131072 - 120000).",
        ],
    )
    def test_antwortlaenge(self, text: str) -> None:
        assert laengenlimit_hinweis(400, text) == LAENGENLIMIT_AUSGABE
        assert "Max. Output-Tokens" in LAENGENLIMIT_AUSGABE

    @pytest.mark.parametrize(
        "text",
        [
            "This model's maximum context length is 131072 tokens. However, your messages resulted in 140000 tokens.",
            '{"error": {"code": "context_length_exceeded"}}',
            "Input is too long for the context window of this model",
        ],
    )
    def test_eingabe_zu_lang(self, text: str) -> None:
        """Eine zu lange Eingabe empfiehlt Kürzen bzw. ein größeres Kontextfenster, nicht „Max. Output-Tokens“."""
        assert laengenlimit_hinweis(400, text) == LAENGENLIMIT_KONTEXT
        assert "Max. Output-Tokens" not in LAENGENLIMIT_KONTEXT and "kürzen" in LAENGENLIMIT_KONTEXT

    def test_andere_fehler_nicht(self) -> None:
        assert laengenlimit_hinweis(400, '{"error": "model not found"}') is None
        assert laengenlimit_hinweis(500, "max_tokens") is None
        assert laengenlimit_hinweis(400, "") is None

    def test_standard_der_antwortlaenge_im_buergerportal(self) -> None:
        # STACKIT dokumentiert für openai/gpt-oss-120b höchstens 8192 Tokens je Antwort
        assert AISettings().insight_max_output_tokens == 8192


@pytest.mark.django_db
class TestAufloesung:
    def test_ohne_einrichtung_aus(self) -> None:
        assert endpunkt_fuer_work() is None
        assert endpunkt_fuer_insight() is None

    def test_work_und_buergerportal(self) -> None:
        _ki(insight_model="modell-b", fallback_model="modell-c", insight_max_output_tokens=1234)
        work = endpunkt_fuer_work()
        insight = endpunkt_fuer_insight()
        assert work is not None and insight is not None
        assert (work.base_url, work.modell, work.api_key) == (STACKIT, "modell-a", SCHLUESSEL)
        assert (insight.modell, insight.ausweichmodell, insight.max_output_tokens) == ("modell-b", "modell-c", 1234)
        assert insight.anzeigename == "STACKIT AI Model Serving"
        assert insight.verarbeitungsort == "Rechenzentren in Deutschland und Österreich (EU)"

    def test_buergerportal_nur_mit_schalter(self) -> None:
        _ki(insight_enabled=False)
        assert endpunkt_fuer_insight() is None
        assert endpunkt_fuer_work() is not None

    def test_work_nur_mit_schalter(self) -> None:
        _ki(enabled=False)
        assert endpunkt_fuer_work() is None
        assert endpunkt_fuer_insight() is not None

    def test_nie_ein_schluessel_aus_der_umgebung(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEBIUS_API_KEY", "aus-der-umgebung")
        ki = _ki()
        ki.set_api_key("")
        ki.save()
        assert endpunkt_fuer_work() is None and endpunkt_fuer_insight() is None

    def test_gesperrter_host_wirkt_nicht_auch_aus_der_datenbank(self, caplog: pytest.LogCaptureFixture) -> None:
        _ki()
        AISettings.objects.filter(pk=1).update(provider="eigener", base_url="https://api.openai.com/v1")
        AISettings.objects.filter(pk=1).update(anzeigename="X", verarbeitungsort="Y")
        cache.delete(AISettings.CACHE_KEY)
        with caplog.at_level(logging.WARNING):
            assert endpunkt_fuer_work() is None and endpunkt_fuer_insight() is None
        assert "KI_ERLAUBTE_HOSTS" in caplog.text
        assert SCHLUESSEL not in caplog.text

    def test_vorlage_ausserhalb_der_positivliste_wirkt_nicht(self) -> None:
        _ki(provider="ionos")
        assert endpunkt_fuer_work() is None
        with override_settings(KI_ERLAUBTE_HOSTS=[IONOS_HOST]):
            endpunkt = endpunkt_fuer_work()
        assert endpunkt is not None and endpunkt.base_url == IONOS

    @override_settings(KI_ERLAUBTE_HOSTS=[STACKIT_HOST, IONOS_HOST])
    def test_vorlage_mit_fremdem_host_wirkt_nicht(self, caplog: pytest.LogCaptureFixture) -> None:
        """Sonst nennte die Einwilligung STACKIT, die Anfragen gingen aber an einen anderen Anbieter."""
        _ki()
        AISettings.objects.filter(pk=1).update(provider="stackit", base_url=IONOS)
        cache.delete(AISettings.CACHE_KEY)
        with caplog.at_level(logging.WARNING):
            assert endpunkt_fuer_work() is None and endpunkt_fuer_insight() is None
        assert "Eigener Endpunkt" in caplog.text and SCHLUESSEL not in caplog.text

    def test_vorlage_mit_anderem_pfad(self) -> None:
        _ki(base_url=STACKIT + "/v2")
        endpunkt = endpunkt_fuer_work()
        assert endpunkt is not None and endpunkt.base_url == STACKIT + "/v2"
        assert endpunkt.anzeigename == "STACKIT AI Model Serving"

    def test_frueherer_anbieter_wirkt_nicht(self) -> None:
        _ki()
        AISettings.objects.filter(pk=1).update(provider="nebius", base_url="")
        cache.delete(AISettings.CACHE_KEY)
        assert endpunkt_fuer_work() is None

    def test_eigener_endpunkt_braucht_name_und_ort(self) -> None:
        _ki(provider="eigener", base_url=STACKIT + "/")
        assert endpunkt_fuer_work() is None
        _ki(provider="eigener", base_url=STACKIT, anzeigename="Eigener Dienst", verarbeitungsort="Frankfurt")
        endpunkt = endpunkt_fuer_work()
        assert endpunkt is not None
        assert (endpunkt.anzeigename, endpunkt.verarbeitungsort) == ("Eigener Dienst", "Frankfurt")

    @override_settings(DEMO_INSTANCE=True)
    def test_demo_immer_aus(self) -> None:
        _ki()
        assert endpunkt_fuer_work() is None and endpunkt_fuer_insight() is None

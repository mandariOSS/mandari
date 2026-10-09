# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI im Bürgerportal nur über die zentrale KI-Konfiguration (Issue #950).

Zusammenfassung, KI-Assistent und KI-Verortung holen ihren Anbieter aus ``get_insight_provider``; ohne
Einrichtung keine Anfrage. Die Einwilligung im KI-Assistenten nennt Anbieter und Verarbeitungsort aus der
Konfiguration, gilt nur für diesen Anbieter, und ohne Endpunkt gibt es keinen Chat. Die Positivliste hat keinen
Standard: Ohne Host in ``KI_ERLAUBTE_HOSTS`` stellt keiner der drei Dienste eine Anfrage; die übrigen Tests
geben den Beispiel-Host ausdrücklich frei (Fixture ``_freigabe``).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common import ki_anbieter
from apps.common.ki_anbieter import endpunkt_fuer_insight
from apps.common.models import AISettings
from insight_ai.providers import NichtEingerichtet, OpenAIKompatiblerProvider
from insight_ai.services import chat_service, summarizer
from insight_core.models import OParlBody, OParlSource
from insight_core.services import georeferencing

pytestmark = pytest.mark.django_db

SCHLUESSEL = "buergerportal-testschluessel-geheim-0123456789"
STACKIT_HOST = "api.openai-compat.model-serving.eu01.onstackit.cloud"
CHAT = f"https://{STACKIT_HOST}/v1/chat/completions"


@pytest.fixture(autouse=True)
def _frischer_cache() -> None:
    cache.delete(AISettings.CACHE_KEY)


@pytest.fixture(autouse=True)
def _freigabe(settings: Any) -> None:
    """Die Positivliste hat keinen Standard: Die Tests geben den Beispiel-Host ausdrücklich frei."""
    settings.KI_ERLAUBTE_HOSTS = [STACKIT_HOST]


@pytest.fixture
def anfragen(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Jede HTTP-Anfrage an den KI-Anbieter landet hier statt im Netz."""
    gesendet: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesendet.append(request)
        inhalt = '[{"raw": "Hauptstraße", "type": "street", "normalized": "Hauptstraße"}]'
        return httpx.Response(200, json={"choices": [{"message": {"content": inhalt}}], "usage": {}})

    echter_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return echter_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return gesendet


def _einrichten(**werte: Any) -> None:
    ki = AISettings.get_settings()
    ki.provider = "stackit"
    ki.insight_enabled = True
    ki.insight_model = "modell-portal"
    for name, wert in werte.items():
        setattr(ki, name, wert)
    ki.set_api_key(SCHLUESSEL)
    ki.save()


@pytest.fixture
def besucher(client: Client) -> Client:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    kommune = OParlBody.objects.create(
        external_id="https://ris.example.org/body/1",
        source=source,
        name="Stadt Beispielstadt",
        display_name="Beispielstadt",
        slug="beispielstadt",
        last_sync=timezone.now(),
    )
    client.get(reverse("insight_core:insight:set_body", args=[kommune.id]))
    return client


def _einwilligen(besucher: Client) -> Any:
    """Wie die Chatseite: Seite laden, dann mit der Kennung des angezeigten Anbieters einwilligen."""
    kennung = besucher.get(reverse("insight_core:insight:chat")).context["ki_endpunkt"].einwilligungskennung
    return besucher.post(
        reverse("insight_core:insight:chat_message"),
        {"consent": True, "kennung": kennung},
        content_type="application/json",
    )


class TestAnbieterAusDerKonfiguration:
    def test_zusammenfassung(self) -> None:
        assert isinstance(summarizer.SummaryService().provider, NichtEingerichtet)
        assert summarizer.SummaryService().is_available() is False
        _einrichten()
        dienst = summarizer.SummaryService()
        assert isinstance(dienst.provider, OpenAIKompatiblerProvider)
        assert dienst.provider.model_name == "modell-portal"

    def test_assistent_ohne_einrichtung_ohne_anfrage(self, anfragen: list[httpx.Request]) -> None:
        with pytest.raises(ValueError, match="nicht eingerichtet"):
            chat_service.process_chat_message("Was ist geplant?", [], None)
        assert anfragen == []

    def test_assistent_fragt_den_konfigurierten_endpunkt(
        self, anfragen: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _einrichten()
        monkeypatch.setattr(chat_service, "build_rag_context", lambda query, body_id: ("", []))
        ergebnis = chat_service.process_chat_message("Was ist geplant?", [], None)
        assert ergebnis["response"]
        assert len(anfragen) == 1 and str(anfragen[0].url) == CHAT
        assert json.loads(anfragen[0].content)["model"] == "modell-portal"

    def test_verortung(self, anfragen: list[httpx.Request]) -> None:
        assert georeferencing.extract_locations_with_ai("Text", "Beispielstadt") == []
        assert anfragen == []
        _einrichten()
        orte = georeferencing.extract_locations_with_ai("Ausbau der Hauptstraße", "Beispielstadt")
        assert orte and orte[0]["raw"] == "Hauptstraße"
        assert len(anfragen) == 1 and str(anfragen[0].url) == CHAT

    def test_assistent_nutzt_den_uebergebenen_endpunkt(
        self, anfragen: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Der in der View geprüfte Endpunkt wird gefragt, ohne die Konfiguration erneut aufzulösen."""
        _einrichten()
        endpunkt = endpunkt_fuer_insight()
        assert endpunkt is not None

        def nicht_erneut() -> None:
            raise AssertionError("Endpunkt erneut aufgelöst")

        monkeypatch.setattr(chat_service, "get_insight_provider", nicht_erneut)
        monkeypatch.setattr(chat_service, "build_rag_context", lambda query, body_id: ("", []))
        ergebnis = chat_service.process_chat_message("Was ist geplant?", [], None, endpunkt=endpunkt)
        assert ergebnis["response"] and len(anfragen) == 1 and str(anfragen[0].url) == CHAT

    def test_kein_fester_anbieter_in_den_diensten(self) -> None:
        for modul in (summarizer, chat_service):
            assert not hasattr(modul, "NebiusProvider")
            assert hasattr(modul, "get_insight_provider")


class TestOhneFreigabeliste:
    """Vollständig eingerichtet und eingeschaltet, aber KI_ERLAUBTE_HOSTS leer: keine Anfrage, kein Chat."""

    @pytest.fixture(autouse=True)
    def _leer(self, settings: Any, db: Any) -> None:
        settings.KI_ERLAUBTE_HOSTS = []
        _einrichten()

    def test_zusammenfassung(self) -> None:
        dienst = summarizer.SummaryService()
        assert isinstance(dienst.provider, NichtEingerichtet) and dienst.is_available() is False

    def test_assistent(self, anfragen: list[httpx.Request]) -> None:
        with pytest.raises(ValueError, match="nicht eingerichtet"):
            chat_service.process_chat_message("Was ist geplant?", [], None)
        assert anfragen == []

    def test_verortung(self, anfragen: list[httpx.Request]) -> None:
        assert georeferencing.extract_locations_with_ai("Ausbau der Hauptstraße", "Beispielstadt") == []
        assert anfragen == []

    def test_chatseite_und_schnittstelle(self, besucher: Client, anfragen: list[httpx.Request]) -> None:
        inhalt = besucher.get(reverse("insight_core:insight:chat")).content.decode()
        assert "nicht eingerichtet" in inhalt and "chatApp()" not in inhalt
        api = reverse("insight_core:insight:chat_message")
        assert besucher.post(api, {"consent": True}, content_type="application/json").status_code == 503
        assert besucher.post(api, {"message": "Hallo"}, content_type="application/json").status_code == 503
        assert anfragen == []


class TestEinwilligung:
    def test_ohne_endpunkt_kein_chat(self, besucher: Client) -> None:
        inhalt = besucher.get(reverse("insight_core:insight:chat")).content.decode()
        assert "nicht eingerichtet" in inhalt
        assert "chatApp()" not in inhalt and "Nebius" not in inhalt

    def test_anbieter_und_ort_aus_der_konfiguration(self, besucher: Client) -> None:
        _einrichten(anzeigename="Testanbieter Europa", verarbeitungsort="Rechenzentrum in Testhausen (EU)")
        inhalt = besucher.get(reverse("insight_core:insight:chat")).content.decode()
        assert "Testanbieter Europa" in inhalt and "Rechenzentrum in Testhausen (EU)" in inhalt
        assert "Nebius" not in inhalt and "Kimi" not in inhalt
        assert SCHLUESSEL not in inhalt

    def test_vorlage_liefert_name_und_ort(self, besucher: Client) -> None:
        _einrichten()
        inhalt = besucher.get(reverse("insight_core:insight:chat")).content.decode()
        # „All models are operated in data centers in Germany and Austria“ (STACKIT)
        assert "STACKIT AI Model Serving" in inhalt and "Rechenzentren in Deutschland und Österreich (EU)" in inhalt

    def test_einwilligung_gilt_nur_fuer_den_anbieter(self, besucher: Client, anfragen: list[httpx.Request]) -> None:
        seite, api = reverse("insight_core:insight:chat"), reverse("insight_core:insight:chat_message")
        _einrichten()
        assert _einwilligen(besucher).status_code == 200
        assert besucher.get(seite).context["has_chat_consent"] is True

        # Anderer Anbieter (eigener Endpunkt): erneut fragen, keine Anfrage
        _einrichten(
            provider="eigener",
            base_url=CHAT.removesuffix("/chat/completions"),
            anzeigename="Neuer Anbieter",
            verarbeitungsort="Europa",
        )
        assert besucher.get(seite).context["has_chat_consent"] is False
        antwort = besucher.post(api, {"message": "Hallo"}, content_type="application/json")
        assert antwort.status_code == 403 and antwort.json()["error"] == "consent_required"

        # Nicht freigegebene Vorlage: kein Chat, auch keine Einwilligung
        AISettings.objects.filter(pk=1).update(provider="ionos", base_url="")
        cache.delete(AISettings.CACHE_KEY)
        assert besucher.post(api, {"consent": True}, content_type="application/json").status_code == 503
        assert besucher.post(api, {"message": "Hallo"}, content_type="application/json").status_code == 503
        assert anfragen == []

    def test_alte_einwilligung_ohne_anbieter_gilt_nicht(self, besucher: Client, anfragen: list[httpx.Request]) -> None:
        _einrichten()
        sitzung = besucher.session
        sitzung["chat_consent"] = True  # Einwilligung aus der Zeit vor Issue #950
        sitzung.save()
        antwort = besucher.post(
            reverse("insight_core:insight:chat_message"), {"message": "Hallo"}, content_type="application/json"
        )
        assert antwort.status_code == 403 and antwort.json()["error"] == "consent_required"
        assert anfragen == []

    def test_neue_frage_bei_geaendertem_ort_oder_namen(self, besucher: Client, anfragen: list[httpx.Request]) -> None:
        seite, api = reverse("insight_core:insight:chat"), reverse("insight_core:insight:chat_message")
        _einrichten()
        assert _einwilligen(besucher).status_code == 200
        assert besucher.get(seite).context["has_chat_consent"] is True

        _einrichten(verarbeitungsort="Rechenzentren in Testhausen (EU)")
        assert besucher.get(seite).context["has_chat_consent"] is False
        antwort = besucher.post(api, {"message": "Hallo"}, content_type="application/json")
        assert antwort.status_code == 403 and antwort.json()["error"] == "consent_required"

        assert _einwilligen(besucher).status_code == 200
        _einrichten(verarbeitungsort="Rechenzentren in Testhausen (EU)", anzeigename="Anderer Name")
        assert besucher.get(seite).context["has_chat_consent"] is False
        assert anfragen == []

    def test_endpunkt_je_anfrage_einmal_aufgeloest(
        self, besucher: Client, anfragen: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Einwilligung und KI-Aufruf beziehen sich auf denselben, einmal aufgelösten Endpunkt."""
        _einrichten()
        api = reverse("insight_core:insight:chat_message")
        assert _einwilligen(besucher).status_code == 200

        aufloesungen: list[str] = []
        echt = ki_anbieter._baue_endpunkt

        def zaehlen(**werte: Any) -> Any:
            aufloesungen.append(werte["quelle"])
            return echt(**werte)

        monkeypatch.setattr(ki_anbieter, "_baue_endpunkt", zaehlen)
        monkeypatch.setattr(chat_service, "build_rag_context", lambda query, body_id: ("", []))
        antwort = besucher.post(api, {"message": "Was ist geplant?"}, content_type="application/json")
        assert antwort.status_code == 200, antwort.content
        assert aufloesungen == ["KI-Einstellungen, Bürgerportal"]
        assert len(anfragen) == 1 and str(anfragen[0].url) == CHAT

    def test_keine_rohe_einwilligung_im_seitenkontext(self, besucher: Client) -> None:
        """Eine Kennung aus der Sitzung ist immer wahr; nur die Chatseite prüft die Einwilligung (Anbieter)."""
        sitzung = besucher.session
        sitzung["chat_consent"] = '["stackit", "fremd.example", "X", "Y"]'
        sitzung.save()
        kontext = besucher.get(reverse("insight_core:insight:portal_home")).context
        assert not kontext.get("has_chat_consent")

    def test_einwilligung_nur_fuer_den_angezeigten_anbieter(
        self, besucher: Client, anfragen: list[httpx.Request]
    ) -> None:
        """Wechselt der Anbieter zwischen Seitenaufruf und Zustimmung, gilt sie nicht für den neuen (Issue #950)."""
        seite, api = reverse("insight_core:insight:chat"), reverse("insight_core:insight:chat_message")
        _einrichten()
        angezeigt = besucher.get(seite).context["ki_endpunkt"].einwilligungskennung

        _einrichten(
            provider="eigener",
            base_url=CHAT.removesuffix("/chat/completions"),
            anzeigename="Neuer Anbieter",
            verarbeitungsort="Europa",
        )
        for werte in ({"consent": True, "kennung": angezeigt}, {"consent": True}):
            antwort = besucher.post(api, werte, content_type="application/json")
            assert antwort.status_code == 409 and antwort.json()["error"] == "consent_outdated"
        assert "chat_consent" not in besucher.session
        assert besucher.get(seite).context["has_chat_consent"] is False
        antwort = besucher.post(api, {"message": "Hallo"}, content_type="application/json")
        assert antwort.status_code == 403 and antwort.json()["error"] == "consent_required"

        # Nach dem Neuladen zeigt der Dialog den aktuellen Anbieter; dessen Kennung gilt
        neu = besucher.get(seite)
        assert "Neuer Anbieter" in neu.content.decode()
        assert neu.context["ki_endpunkt"].einwilligungskennung != angezeigt
        assert _einwilligen(besucher).status_code == 200
        assert besucher.get(seite).context["has_chat_consent"] is True
        assert anfragen == []

    def test_seite_schickt_die_kennung_des_angezeigten_anbieters(self, besucher: Client) -> None:
        _einrichten()
        antwort = besucher.get(reverse("insight_core:insight:chat"))
        inhalt = antwort.content.decode()
        kennung = antwort.context["ki_endpunkt"].einwilligungskennung
        assert f"consentKennung: '{kennung}'" in inhalt
        assert "JSON.stringify({ consent: true, kennung: this.consentKennung })" in inhalt
        assert "response.status === 409" in inhalt and "window.location.reload()" in inhalt
        # Prüfsumme statt Klartext: Der Host des Anbieters steht nicht im Quelltext
        assert "onstackit.cloud" not in inhalt and SCHLUESSEL not in inhalt

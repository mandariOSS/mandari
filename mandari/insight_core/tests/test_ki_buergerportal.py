# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI im Bürgerportal nur über die zentrale KI-Konfiguration (Issue #950).

Zusammenfassung, KI-Assistent und KI-Verortung holen ihren Anbieter aus ``get_insight_provider``; ohne
Einrichtung keine Anfrage. Die Einwilligung im KI-Assistenten nennt Anbieter und Verarbeitungsort aus der
Konfiguration, gilt nur für diesen Anbieter, und ohne Endpunkt gibt es keinen Chat.
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

from apps.common.models import AISettings
from insight_ai.providers import NichtEingerichtet, OpenAIKompatiblerProvider
from insight_ai.services import chat_service, summarizer
from insight_core.models import OParlBody, OParlSource
from insight_core.services import georeferencing

pytestmark = pytest.mark.django_db

SCHLUESSEL = "buergerportal-testschluessel-geheim-0123456789"
CHAT = "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1/chat/completions"


@pytest.fixture(autouse=True)
def _frischer_cache() -> None:
    cache.delete(AISettings.CACHE_KEY)


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

    def test_kein_fester_anbieter_in_den_diensten(self) -> None:
        for modul in (summarizer, chat_service):
            assert not hasattr(modul, "NebiusProvider")
            assert hasattr(modul, "get_insight_provider")


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
        assert "STACKIT AI Model Serving" in inhalt and "Rechenzentren in Deutschland (EU)" in inhalt

    def test_einwilligung_gilt_nur_fuer_den_anbieter(self, besucher: Client, anfragen: list[httpx.Request]) -> None:
        seite, api = reverse("insight_core:insight:chat"), reverse("insight_core:insight:chat_message")
        _einrichten()
        assert besucher.post(api, {"consent": True}, content_type="application/json").status_code == 200
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

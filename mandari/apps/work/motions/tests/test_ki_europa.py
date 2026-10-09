# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schreibhilfe und Co-Editor in Work nur über die zentrale KI-Konfiguration (Issue #950).

Kein fest eingebauter Anbieter, kein Rückfall auf einen Schlüssel aus der Umgebung, kein Rückfall von einer
ungültigen Organisationskonfiguration auf die Plattform, nur Hosts aus ``KI_ERLAUBTE_HOSTS``. HTTP läuft nur
über ``httpx.MockTransport``; gezählt wird jede Anfrage, die das Modul stellen würde.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from django.core.cache import cache

from apps.common.models import AISettings
from apps.tenants.models import Organization
from apps.work.motions import services
from apps.work.motions.services import MotionAIService

pytestmark = pytest.mark.django_db

STACKIT_CHAT = "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1/chat/completions"
SCHLUESSEL = "work-testschluessel-geheim-0123456789"
NACHRICHTEN = [{"role": "user", "content": "Bitte kürzen"}]


@pytest.fixture
def anfragen(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Jede HTTP-Anfrage aus dem Work-Dienst landet hier statt im Netz."""
    gesendet: list[httpx.Request] = []

    def antwort(request: httpx.Request) -> httpx.Response:
        gesendet.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Gekürzter Text"}}], "usage": {"total_tokens": 42}},
        )

    echter_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(antwort)
        return echter_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return gesendet


@pytest.fixture(autouse=True)
def _ohne_umgebungsschluessel(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ein früher üblicher Schlüssel in der Umgebung darf nie wirken
    monkeypatch.setenv("NEBIUS_API_KEY", "nebius-aus-der-umgebung")
    cache.delete(AISettings.CACHE_KEY)


def _plattform(*, enabled: bool = True, schluessel: str = SCHLUESSEL, **werte: Any) -> AISettings:
    ki = AISettings.get_settings()
    ki.provider = werte.pop("provider", "stackit")
    ki.enabled = enabled
    ki.model_name = werte.pop("model_name", "modell-work")
    for name, wert in werte.items():
        setattr(ki, name, wert)
    ki.set_api_key(schluessel)
    ki.save()
    return ki


def test_aus_wenn_work_abgeschaltet(org: Organization, anfragen: list[httpx.Request]) -> None:
    _plattform(enabled=False)
    dienst = MotionAIService(organization=org)
    assert dienst.is_available() is False
    antwort = dienst._call_api(NACHRICHTEN)
    assert not antwort.success and antwort.error == "KI ist nicht eingerichtet."
    assert anfragen == []


def test_aus_ohne_schluessel_trotz_umgebung(org: Organization, anfragen: list[httpx.Request]) -> None:
    _plattform(schluessel="")
    dienst = MotionAIService(organization=org)
    assert dienst.is_available() is False
    assert not dienst._call_api(NACHRICHTEN).success
    assert anfragen == []


def test_aus_ohne_anbieter(org: Organization, anfragen: list[httpx.Request]) -> None:
    _plattform(provider="")
    assert MotionAIService(organization=org).is_available() is False
    assert anfragen == []


def test_stackit_genau_eine_anfrage(
    org: Organization, anfragen: list[httpx.Request], caplog: pytest.LogCaptureFixture
) -> None:
    _plattform(max_output_tokens=512)
    dienst = MotionAIService(organization=org)
    assert dienst.is_available() is True
    with caplog.at_level(logging.INFO, logger="apps.work.motions.services"):
        antwort = dienst._call_api(NACHRICHTEN, max_tokens=2000)
    assert antwort.success and antwort.content == "Gekürzter Text" and antwort.total_tokens == 42
    assert len(anfragen) == 1
    anfrage = anfragen[0]
    assert str(anfrage.url) == STACKIT_CHAT
    assert anfrage.headers["Authorization"] == f"Bearer {SCHLUESSEL}"
    nutzlast = json.loads(anfrage.content)
    assert nutzlast["model"] == "modell-work" and nutzlast["max_tokens"] == 512
    assert (
        "KI-Aufruf: anbieter=stackit host=api.openai-compat.model-serving.eu01.onstackit.cloud modell=modell-work"
        in caplog.text
    )
    assert SCHLUESSEL not in caplog.text and "Bitte kürzen" not in caplog.text


def test_organisation_mit_gesperrter_adresse_ohne_rueckfall(org: Organization, anfragen: list[httpx.Request]) -> None:
    _plattform()  # Die Plattform wäre eingerichtet: trotzdem kein Rückfall
    org.set_ai_api_key("org-testschluessel-geheim")
    org.save()
    Organization.objects.filter(pk=org.pk).update(
        ai_provider="eigener",
        ai_base_url="https://api.openai.com/v1",
        ai_anzeigename="Fremd",
        ai_verarbeitungsort="unbekannt",
    )
    org.refresh_from_db()
    dienst = MotionAIService(organization=org)
    assert dienst.is_available() is False
    antwort = dienst._call_api(NACHRICHTEN)
    assert not antwort.success and antwort.error == "KI ist nicht eingerichtet."
    assert anfragen == []


def test_organisation_mit_eigenem_freigegebenem_endpunkt(org: Organization, anfragen: list[httpx.Request]) -> None:
    org.set_ai_api_key("org-testschluessel-geheim")
    org.ai_provider = "stackit"
    org.ai_model = "modell-org"
    org.save()
    antwort = MotionAIService(organization=org)._call_api(NACHRICHTEN)
    assert antwort.success
    assert len(anfragen) == 1
    assert anfragen[0].headers["Authorization"] == "Bearer org-testschluessel-geheim"
    assert json.loads(anfragen[0].content)["model"] == "modell-org"


def test_organisation_mit_ki_aus(org: Organization, anfragen: list[httpx.Request]) -> None:
    _plattform()
    org.ai_enabled = False
    org.save()
    dienst = MotionAIService(organization=org)
    assert dienst.is_available() is False
    assert not dienst._call_api(NACHRICHTEN).success
    assert dienst.chat_with_document("<p>Text</p>", "Fasse zusammen").success is False
    assert anfragen == []


def test_kein_anbieter_zwang_im_dienst() -> None:
    assert not hasattr(MotionAIService, "PROVIDER_DEFAULTS")
    assert not hasattr(services, "SiteSettings")


def test_laengenlimit_verstaendlich_im_protokoll(
    org: Organization, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _plattform(max_output_tokens=16000)
    fehlertext = "max_tokens must be <= 8192 (Anfrage: Bitte kürzen)"
    echter_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": {"message": fehlertext}})
        )
        return echter_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    with caplog.at_level(logging.WARNING):
        antwort = MotionAIService(organization=org)._call_api(NACHRICHTEN, max_tokens=16000)
    assert not antwort.success and antwort.error == "Die Anfrage ist für das KI-Modell zu lang."
    assert "Max. Output-Tokens" in caplog.text and "max_tokens=16000" in caplog.text
    assert "Bitte kürzen" not in caplog.text and SCHLUESSEL not in caplog.text


def test_zu_lange_eingabe_mit_eigenem_hinweis(
    org: Organization, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _plattform()
    fehlertext = "This model's maximum context length is 131072 tokens (Anfrage: Bitte kürzen)"
    echter_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": {"message": fehlertext}})
        )
        return echter_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    with caplog.at_level(logging.WARNING):
        antwort = MotionAIService(organization=org)._call_api(NACHRICHTEN)
    assert not antwort.success and antwort.error == "Die Anfrage ist für das KI-Modell zu lang."
    assert "Kontextfenster" in caplog.text and "Max. Output-Tokens" not in caplog.text
    assert "Bitte kürzen" not in caplog.text and SCHLUESSEL not in caplog.text

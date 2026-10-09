# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenAI-kompatibler Anbieter des Bürgerportals (Issue #950).

Anfrage an die konfigurierte Adresse, Antwort nur aus ``message.content`` (Denktext bleibt draußen), Ausweichmodell
nur nach 404/408/429/5xx/Zeitüberschreitung am selben Endpunkt mit demselben Schlüssel, Antwortlänge gekappt,
Schlüssel weder im Protokoll noch in ``repr``. HTTP nur über ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest
from django.core.cache import cache

from apps.common.ki_anbieter import KiEndpunkt
from apps.common.models import AISettings
from insight_ai.providers import (
    KiAnbieterError,
    NichtEingerichtet,
    OpenAIKompatiblerProvider,
    get_insight_provider,
)
from insight_ai.providers.base import ChatMessage

BASIS = "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1"
CHAT = BASIS + "/chat/completions"
SCHLUESSEL = "insight-testschluessel-geheim-0123456789"
FRAGE = [ChatMessage(role="user", content="Worum geht es?")]

Antwort = Callable[[httpx.Request], httpx.Response]


def _endpunkt(**werte: object) -> KiEndpunkt:
    felder: dict[str, object] = {
        "anbieter": "stackit",
        "anzeigename": "STACKIT AI Model Serving",
        "verarbeitungsort": "Rechenzentren in Deutschland (EU)",
        "base_url": BASIS,
        "api_key": SCHLUESSEL,
        "modell": "modell-haupt",
        "ausweichmodell": "modell-ausweich",
        "max_output_tokens": 16000,
        **werte,
    }
    return KiEndpunkt(**felder)  # type: ignore[arg-type]


def _anbieter(antwort: Antwort, **werte: object) -> tuple[OpenAIKompatiblerProvider, list[httpx.Request]]:
    gesendet: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gesendet.append(request)
        return antwort(request)

    return OpenAIKompatiblerProvider(_endpunkt(**werte), transport=httpx.MockTransport(handler)), gesendet


def _ok(inhalt: str = "Kurzfassung", **message: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": inhalt, **message}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        },
    )


def test_anfrage_an_die_konfigurierte_adresse() -> None:
    anbieter, gesendet = _anbieter(lambda request: _ok())
    antwort = anbieter.chat_completion(FRAGE, max_tokens=500, temperature=0.2)
    assert antwort.content == "Kurzfassung"
    assert (antwort.input_tokens, antwort.output_tokens, antwort.total_tokens) == (11, 7, 18)
    assert len(gesendet) == 1
    assert str(gesendet[0].url) == CHAT
    assert gesendet[0].headers["Authorization"] == f"Bearer {SCHLUESSEL}"
    nutzlast = json.loads(gesendet[0].content)
    assert nutzlast["model"] == "modell-haupt" and nutzlast["temperature"] == 0.2
    assert nutzlast["messages"] == [{"role": "user", "content": "Worum geht es?"}]


@pytest.mark.parametrize("feld", ["reasoning", "reasoning_content"])
def test_inhalt_ohne_denktext(feld: str) -> None:
    anbieter, _ = _anbieter(lambda request: _ok("Antwort", **{feld: "Langer interner Denktext"}))
    antwort = anbieter.chat_completion(FRAGE)
    assert antwort.content == "Antwort"
    assert "Denktext" not in antwort.content


def test_nur_denktext_ergibt_leere_antwort() -> None:
    anbieter, _ = _anbieter(lambda request: _ok("", reasoning_content="Nur gedacht"))
    assert anbieter.chat_completion(FRAGE).content == ""


@pytest.mark.parametrize("status", [404, 408, 429, 500, 503])
def test_ausweichmodell_am_selben_endpunkt(status: int) -> None:
    def antwort(request: httpx.Request) -> httpx.Response:
        modell = json.loads(request.content)["model"]
        return httpx.Response(status) if modell == "modell-haupt" else _ok("Vom Ausweichmodell")

    anbieter, gesendet = _anbieter(antwort)
    ergebnis = anbieter.chat_completion(FRAGE)
    assert ergebnis.content == "Vom Ausweichmodell" and ergebnis.model == "modell-ausweich"
    assert [json.loads(r.content)["model"] for r in gesendet] == ["modell-haupt", "modell-ausweich"]
    assert {r.url.host for r in gesendet} == {"api.openai-compat.model-serving.eu01.onstackit.cloud"}
    assert {r.headers["Authorization"] for r in gesendet} == {f"Bearer {SCHLUESSEL}"}


def test_ausweichmodell_nach_zeitueberschreitung() -> None:
    def antwort(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "modell-haupt":
            raise httpx.ReadTimeout("zu langsam", request=request)
        return _ok("Vom Ausweichmodell")

    anbieter, gesendet = _anbieter(antwort)
    assert anbieter.chat_completion(FRAGE).content == "Vom Ausweichmodell"
    assert len(gesendet) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_kein_ausweichversuch_bei_anderen_fehlern(status: int) -> None:
    anbieter, gesendet = _anbieter(lambda request: httpx.Response(status, text="Fehler mit Prompt-Echo"))
    with pytest.raises(KiAnbieterError) as fehler:
        anbieter.chat_completion(FRAGE)
    assert len(gesendet) == 1
    assert "Prompt-Echo" not in str(fehler.value) and SCHLUESSEL not in str(fehler.value)


def test_ausweichmodell_nur_einmal() -> None:
    anbieter, gesendet = _anbieter(lambda request: httpx.Response(503))
    with pytest.raises(KiAnbieterError):
        anbieter.chat_completion(FRAGE)
    assert len(gesendet) == 2


def test_ohne_ausweichmodell_kein_zweiter_versuch() -> None:
    anbieter, gesendet = _anbieter(lambda request: httpx.Response(404), ausweichmodell="")
    with pytest.raises(KiAnbieterError):
        anbieter.chat_completion(FRAGE)
    assert len(gesendet) == 1


def test_antwortlaenge_gekappt() -> None:
    anbieter, gesendet = _anbieter(lambda request: _ok(), max_output_tokens=4000)
    anbieter.chat_completion(FRAGE, max_tokens=32000)
    assert json.loads(gesendet[0].content)["max_tokens"] == 4000
    assert anbieter.max_output_tokens == 4000
    anbieter.chat_completion(FRAGE, max_tokens=100)
    assert json.loads(gesendet[1].content)["max_tokens"] == 100


def test_laengenlimit_verstaendlich_im_protokoll(caplog: pytest.LogCaptureFixture) -> None:
    fehlertext = "max_tokens must be <= 8192 (Anfrage: Worum geht es?)"
    anbieter, gesendet = _anbieter(lambda request: httpx.Response(400, json={"error": {"message": fehlertext}}))
    with caplog.at_level(logging.WARNING), pytest.raises(KiAnbieterError, match="Längenlimit"):
        anbieter.chat_completion(FRAGE, max_tokens=16000)
    assert len(gesendet) == 1  # kein Ausweichversuch
    assert "Max. Output-Tokens" in caplog.text and "max_tokens=16000" in caplog.text
    # Der Antworttext (kann Teile der Anfrage enthalten) und der Schlüssel bleiben draußen
    assert "Worum geht es" not in caplog.text and SCHLUESSEL not in caplog.text


def test_zu_lange_eingabe_mit_eigenem_hinweis(caplog: pytest.LogCaptureFixture) -> None:
    """Passt die Eingabe nicht ins Kontextfenster, hilft „Max. Output-Tokens“ nicht; der Hinweis sagt das."""
    fehlertext = "This model's maximum context length is 131072 tokens. However, your messages resulted in 140000"
    anbieter, gesendet = _anbieter(lambda request: httpx.Response(400, json={"error": {"message": fehlertext}}))
    with caplog.at_level(logging.WARNING), pytest.raises(KiAnbieterError, match="Längenlimit"):
        anbieter.chat_completion(FRAGE, max_tokens=1000)
    assert len(gesendet) == 1
    assert "Kontextfenster" in caplog.text and "Max. Output-Tokens" not in caplog.text
    assert "140000" not in caplog.text and SCHLUESSEL not in caplog.text


def test_anderer_fehler_400_ohne_laengenhinweis(caplog: pytest.LogCaptureFixture) -> None:
    anbieter, _ = _anbieter(lambda request: httpx.Response(400, json={"error": {"message": "unknown model"}}))
    with caplog.at_level(logging.WARNING), pytest.raises(KiAnbieterError) as fehler:
        anbieter.chat_completion(FRAGE)
    assert "Längenlimit" not in str(fehler.value) and "Max. Output-Tokens" not in caplog.text


def test_schluessel_weder_im_protokoll_noch_in_repr(caplog: pytest.LogCaptureFixture) -> None:
    def antwort(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "modell-haupt":
            return httpx.Response(500, text=SCHLUESSEL)
        return _ok(reasoning="Denktext")

    anbieter, _ = _anbieter(antwort)
    with caplog.at_level(logging.DEBUG):
        anbieter.chat_completion(FRAGE)
    assert SCHLUESSEL not in caplog.text
    assert "Worum geht es?" not in caplog.text
    assert SCHLUESSEL not in repr(anbieter)
    assert (
        "KI-Aufruf: anbieter=stackit host=api.openai-compat.model-serving.eu01.onstackit.cloud "
        "modell=modell-ausweich eingabe=11 ausgabe=7"
    ) in caplog.text


@pytest.mark.django_db
class TestAuswahl:
    @pytest.fixture(autouse=True)
    def _frischer_cache(self) -> None:
        cache.delete(AISettings.CACHE_KEY)

    def _ki(self, *, insight_enabled: bool) -> None:
        ki = AISettings.get_settings()
        ki.provider = "stackit"
        ki.insight_enabled = insight_enabled
        ki.insight_max_output_tokens = 2048
        ki.set_api_key(SCHLUESSEL)
        ki.save()

    def test_buergerportal_aus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gesendet: list[httpx.Request] = []
        echter_client = httpx.Client

        def client(**kwargs: object) -> httpx.Client:
            def handler(request: httpx.Request) -> httpx.Response:
                gesendet.append(request)
                return _ok()

            kwargs["transport"] = httpx.MockTransport(handler)
            return echter_client(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(httpx, "Client", client)
        self._ki(insight_enabled=False)
        anbieter = get_insight_provider()
        assert isinstance(anbieter, NichtEingerichtet)
        assert anbieter.is_available() is False
        with pytest.raises(KiAnbieterError):
            anbieter.chat_completion(FRAGE)
        assert gesendet == []

    def test_buergerportal_an(self) -> None:
        self._ki(insight_enabled=True)
        anbieter = get_insight_provider()
        assert isinstance(anbieter, OpenAIKompatiblerProvider)
        assert anbieter.is_available() is True
        assert anbieter.endpunkt.chat_url == CHAT
        assert anbieter.max_output_tokens == 2048

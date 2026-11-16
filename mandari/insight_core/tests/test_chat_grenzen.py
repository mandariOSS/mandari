# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Grenzen des KI-Assistenten bei Anfragen, die sich überlappen.

Nutzungsgrenze je Besucher und Tagesobergrenze zählen gebuchte Antworten; gebucht wird erst nach der Antwort. Die
Tests stellen die Überlappung ohne Threads nach: Der ersetzte Antwortdienst stellt, während die erste Antwort
„läuft“, selbst weitere Anfragen. Es geht kein Aufruf ins Netz.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.models import AISettings
from insight_ai.services import chat_service
from insight_core.models import ChatUsage, OParlBody, OParlSource
from insight_core.services import chat_guard

pytestmark = pytest.mark.django_db

STACKIT_HOST = "api.openai-compat.model-serving.eu01.onstackit.cloud"
ANTWORT = {
    "response": "Am Dienstag tagt der Rat.",
    "sources": [],
    "tokens_used": 900,
    "prompt_tokens": 700,
    "completion_tokens": 200,
    "rounds": 2,
}


@pytest.fixture(autouse=True)
def _eingerichtet(settings: Any) -> None:
    cache.clear()
    settings.KI_ERLAUBTE_HOSTS = [STACKIT_HOST]
    ki = AISettings.get_settings()
    ki.provider = "stackit"
    ki.insight_enabled = True
    ki.insight_model = "modell-portal"
    ki.set_api_key("buergerportal-testschluessel-geheim-0123456789")
    ki.save()


@pytest.fixture
def kommune() -> OParlBody:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    return OParlBody.objects.create(
        external_id="https://ris.example.org/body/1",
        source=source,
        name="Stadt Beispielstadt",
        display_name="Beispielstadt",
        slug="beispielstadt",
        last_sync=timezone.now(),
    )


def _besucher(kommune: OParlBody, adresse: str) -> Client:
    """Ein Besucher mit eigener Sitzung und Adresse, der in den KI-Assistenten eingewilligt hat."""
    client = Client(REMOTE_ADDR=adresse)
    client.get(reverse("insight_core:insight:set_body", args=[kommune.id]))
    kennung = client.get(reverse("insight_core:insight:chat")).context["ki_endpunkt"].einwilligungskennung
    einwilligung = client.post(
        reverse("insight_core:insight:chat_message"),
        {"consent": True, "kennung": kennung},
        content_type="application/json",
    )
    assert einwilligung.status_code == 200, einwilligung.content
    return client


def _fragen(client: Client, frage: str) -> Any:
    return client.post(
        reverse("insight_core:insight:chat_message"), {"message": frage}, content_type="application/json"
    )


def _dienst(monkeypatch: pytest.MonkeyPatch, waehrenddessen: Callable[[], None] | None = None) -> list[str]:
    """Ersetzt den Antwortdienst; ``waehrenddessen`` läuft einmal, solange die erste Antwort noch nicht gebucht ist."""
    aufrufe: list[str] = []

    def antworten(message: str, **kwargs: Any) -> dict[str, Any]:
        aufrufe.append(message)
        if len(aufrufe) == 1 and waehrenddessen is not None:
            waehrenddessen()
        return dict(ANTWORT)

    monkeypatch.setattr(chat_service, "process_chat_message", antworten)
    return aufrufe


def test_zweite_frage_desselben_besuchers_wartet_auf_die_laufende(
    kommune: OParlBody, monkeypatch: pytest.MonkeyPatch
) -> None:
    besucher = _besucher(kommune, "198.51.100.7")
    ueberlappend: list[Any] = []
    aufrufe = _dienst(
        monkeypatch,
        lambda: ueberlappend.extend(_fragen(besucher, f"Was beschließt der Rat zum Thema {n}?") for n in range(7)),
    )

    erste = _fragen(besucher, "Wann tagt der Rat das nächste Mal?")

    assert erste.status_code == 200, erste.content
    # Keine der sieben Anfragen, die während der ersten eintrafen, ging an den Anbieter
    assert [antwort.status_code for antwort in ueberlappend] == [429] * 7
    assert {antwort.json()["error"] for antwort in ueberlappend} == {"answer_in_progress"}
    assert aufrufe == ["Wann tagt der Rat das nächste Mal?"]
    assert ChatUsage.objects.filter(filter_result="passed").count() == 1
    # Nach der Antwort ist der Besucher wieder frei
    assert _fragen(besucher, "Und wann tagt der Bauausschuss?").status_code == 200
    assert len(aufrufe) == 2


def test_gleiche_adresse_mit_anderer_sitzung_wartet_ebenfalls(
    kommune: OParlBody, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Nutzungsgrenze zählt Sitzung und Adresse; eine neue Sitzung von derselben Adresse umgeht sie nicht."""
    erster, zweiter = _besucher(kommune, "198.51.100.8"), _besucher(kommune, "198.51.100.8")
    ueberlappend: list[Any] = []
    aufrufe = _dienst(monkeypatch, lambda: ueberlappend.append(_fragen(zweiter, "Was ist im Bauausschuss geplant?")))

    assert _fragen(erster, "Wann tagt der Rat das nächste Mal?").status_code == 200
    assert [antwort.status_code for antwort in ueberlappend] == [429] and len(aufrufe) == 1


def test_tagesobergrenze_zaehlt_laufende_antworten_mit(
    kommune: OParlBody, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """Ein anderer Besucher mit anderer Adresse bekommt keine Antwort, solange die laufende die Grenze ausschöpft."""
    settings.INSIGHT_CHAT_DAILY_MAX_CALLS = 3
    erster, zweiter = _besucher(kommune, "198.51.100.9"), _besucher(kommune, "203.0.113.4")
    ueberlappend: list[Any] = []
    aufrufe = _dienst(monkeypatch, lambda: ueberlappend.append(_fragen(zweiter, "Was ist im Bauausschuss geplant?")))

    assert _fragen(erster, "Wann tagt der Rat das nächste Mal?").status_code == 200

    assert [antwort.status_code for antwort in ueberlappend] == [503]
    assert ueberlappend[0].json()["error"] == "daily_budget_reached"
    assert aufrufe == ["Wann tagt der Rat das nächste Mal?"]
    # Gebucht sind zwei von drei Modellaufrufen: Nacheinander bekommt der zweite Besucher seine Antwort
    assert _fragen(zweiter, "Was ist im Bauausschuss geplant?").status_code == 200


def test_ohne_ueberlappung_bleibt_die_tagesobergrenze_wie_bisher(
    kommune: OParlBody, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.INSIGHT_CHAT_DAILY_MAX_CALLS = 2
    erster, zweiter = _besucher(kommune, "198.51.100.10"), _besucher(kommune, "203.0.113.5")
    aufrufe = _dienst(monkeypatch)

    assert _fragen(erster, "Wann tagt der Rat das nächste Mal?").status_code == 200
    gesperrt = _fragen(zweiter, "Was ist im Bauausschuss geplant?")

    assert gesperrt.status_code == 503 and gesperrt.json()["error"] == "daily_budget_reached"
    assert len(aufrufe) == 1


def test_hoechstens_so_viele_antworten_laufen_zugleich(
    kommune: OParlBody, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.INSIGHT_CHAT_MAX_CONCURRENT = 1
    erster, zweiter = _besucher(kommune, "198.51.100.11"), _besucher(kommune, "203.0.113.6")
    ueberlappend: list[Any] = []
    aufrufe = _dienst(monkeypatch, lambda: ueberlappend.append(_fragen(zweiter, "Was ist im Bauausschuss geplant?")))

    assert _fragen(erster, "Wann tagt der Rat das nächste Mal?").status_code == 200

    assert [antwort.status_code for antwort in ueberlappend] == [503]
    assert ueberlappend[0].json()["error"] == "busy" and len(aufrufe) == 1
    # Der Platz ist nach der Antwort wieder frei
    assert _fragen(zweiter, "Was ist im Bauausschuss geplant?").status_code == 200


def test_sperre_und_platz_sind_nach_einem_fehler_wieder_frei(
    kommune: OParlBody, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    settings.INSIGHT_CHAT_MAX_CONCURRENT = 1
    besucher = _besucher(kommune, "198.51.100.12")

    def kaputt(message: str, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("Anbieter weg")

    monkeypatch.setattr(chat_service, "process_chat_message", kaputt)
    assert _fragen(besucher, "Wann tagt der Rat das nächste Mal?").status_code == 500

    aufrufe = _dienst(monkeypatch)
    assert _fragen(besucher, "Wann tagt der Bauausschuss das nächste Mal?").status_code == 200
    assert len(aufrufe) == 1


def test_platz_rechnet_die_anderen_laufenden_antworten_ein(settings: Any) -> None:
    settings.INSIGHT_CHAT_MAX_CONCURRENT = 3
    settings.INSIGHT_CHAT_MAX_TOOL_ROUNDS = 4

    erster, zweiter, dritter = chat_guard.platz_belegen(), chat_guard.platz_belegen(), chat_guard.platz_belegen()

    assert erster is not None and zweiter is not None and dritter is not None
    assert (erster.aufrufe_vorbelegt, zweiter.aufrufe_vorbelegt, dritter.aufrufe_vorbelegt) == (0, 5, 10)
    assert dritter.token_vorbelegt == 10 * chat_guard.TOKEN_JE_AUFRUF
    assert chat_guard.platz_belegen() is None
    chat_guard.freigeben([zweiter.schluessel])
    vierter = chat_guard.platz_belegen()
    assert vierter is not None and vierter.schluessel == zweiter.schluessel

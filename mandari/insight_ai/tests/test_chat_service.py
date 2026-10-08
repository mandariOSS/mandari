# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Assistent (Issue #899): Fragen nach Terminen, Tagesordnungen und dem Stand von Vorlagen werden aus den
Ratsdaten mit Links beantwortet. Der Anbieter ist ersetzt (``SkriptAnbieter``); es geht kein Aufruf ins Netz.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from django.test import Client
from django.urls import reverse

from insight_ai.providers.openai_compatible import OpenAICompatibleProvider
from insight_ai.services import chat_service
from insight_core.models import ChatUsage

from .anbieter import (
    SkriptAnbieter,
    antwort_sitzungen,
    antwort_stand,
    antwort_tagesordnung,
    diese_woche,
    morgen,
    tagesordnung_der_ersten_sitzung,
    werkzeuge,
)
from .musterstadt import JETZT, FakeSuche, Musterstadt, baue_musterstadt

pytestmark = pytest.mark.django_db


@pytest.fixture
def stadt() -> Musterstadt:
    return baue_musterstadt()


@pytest.fixture(autouse=True)
def suche(monkeypatch: pytest.MonkeyPatch) -> FakeSuche:
    fake = FakeSuche()
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: fake)
    return fake


def anbieter(monkeypatch: pytest.MonkeyPatch, *schritte: Any, verfuegbar: bool = True) -> SkriptAnbieter:
    fake = SkriptAnbieter(list(schritte), verfuegbar=verfuegbar)
    monkeypatch.setattr(chat_service, "chat_provider", lambda: fake)
    return fake


def frage(stadt: Musterstadt, text: str) -> dict[str, Any]:
    return chat_service.process_chat_message(text, [], str(stadt.body.pk), now=JETZT)


def test_sitzungen_diese_woche(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    fake = anbieter(
        monkeypatch,
        lambda messages: [("sitzungen_im_zeitraum", dict(zip(("von", "bis"), diese_woche(messages), strict=True)))],
        antwort_sitzungen,
    )
    ergebnis = frage(stadt, "Welche Sitzungen finden diese Woche statt?")

    antwort = ergebnis["response"]
    assert f"[Rat am Mi 07.10.2026 17:00](/insight/termine/{stadt.rat_sitzung.pk}/)" in antwort
    assert f"(/insight/termine/{stadt.ausschuss_sitzung.pk}/)" in antwort
    assert "(abgesagt)" in antwort
    assert str(stadt.spaetere_sitzung.pk) not in antwort
    assert [q["url"] for q in ergebnis["sources"]] == [
        f"/insight/termine/{stadt.rat_sitzung.pk}/",
        f"/insight/termine/{stadt.ausschuss_sitzung.pk}/",
        f"/insight/termine/{stadt.abgesagte_sitzung.pk}/",
    ]
    assert ergebnis["rounds"] == 2
    assert ergebnis["tool_calls"] == ["sitzungen_im_zeitraum"]
    assert ergebnis["tokens_used"] == ergebnis["prompt_tokens"] + ergebnis["completion_tokens"] > 0
    # Systemprompt: Datum, Wochentag, Kommune, Sie-Form, Produktname
    system = fake.aufrufe[0]["messages"][0]["content"]
    assert "HEUTE: Dienstag, 06.10.2026, 10:00 Uhr (Europe/Berlin)" in system
    assert "Diese Woche: Montag, 05.10.2026, bis Sonntag, 11.10.2026" in system
    assert "Kommune Stadt Musterstadt" in system
    assert "mandari Insight" in system and "Mandari" not in system
    assert "Sie " in system and " du " not in system.lower()
    assert "fremde Kalender" in system


def test_tagesordnung_morgen_im_rat(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    fake = anbieter(
        monkeypatch,
        lambda messages: [
            ("sitzungen_im_zeitraum", {"von": morgen(messages), "bis": morgen(messages), "gremium": "Rat"})
        ],
        tagesordnung_der_ersten_sitzung,
        antwort_tagesordnung,
    )
    ergebnis = frage(stadt, "Was steht morgen im Rat auf der Tagesordnung?")

    antwort = ergebnis["response"]
    assert f"(/insight/termine/{stadt.rat_sitzung.pk}/)" in antwort
    assert f"TOP 2: Radweg an der Musterstraße | [V/2026/0123](/insight/vorgaenge/{stadt.vorlage.pk}/)" in antwort
    assert "TOP 3: nichtöffentlich" in antwort
    assert "Parzelle 7" not in json.dumps(fake.aufrufe, ensure_ascii=False)
    assert ergebnis["tool_calls"] == ["sitzungen_im_zeitraum", "sitzung"]
    assert [q["type"] for q in ergebnis["sources"]] == ["meeting", "paper"]


def test_stand_einer_vorlage(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    # Die Drucksachennummer trifft genau: Die Suche liefert die Einzelheiten gleich mit
    anbieter(monkeypatch, werkzeuge(("vorgaenge_suchen", {"text": "V/2026/0123"})), antwort_stand)
    ergebnis = frage(stadt, "Wie ist der Stand von Vorlage V/2026/0123?")

    antwort = ergebnis["response"]
    assert f"(/insight/vorgaenge/{stadt.vorlage.pk}/)" in antwort
    assert "Am 15.09.2026 im Ausschuss für Umwelt und Verkehr einstimmig empfohlen." in antwort
    assert "Nächste Beratung am 07.10.2026 im Rat." in antwort
    assert str(stadt.fremde_vorlage.pk) not in antwort
    assert ergebnis["sources"] == [
        {"title": "Radweg an der Musterstraße", "url": f"/insight/vorgaenge/{stadt.vorlage.pk}/", "type": "paper"}
    ]
    assert ergebnis["rounds"] == 2


def test_tagesordnung_morgen_in_einer_runde(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    anbieter(
        monkeypatch,
        lambda m: [
            ("sitzungen_im_zeitraum", {"von": morgen(m), "bis": morgen(m), "gremium": "Rat", "tagesordnung": True})
        ],
        antwort_tagesordnung,
    )
    ergebnis = frage(stadt, "Was steht morgen im Rat auf der Tagesordnung?")

    assert (
        f"TOP 2: Radweg an der Musterstraße | [V/2026/0123](/insight/vorgaenge/{stadt.vorlage.pk}/)"
        in (ergebnis["response"])
    )
    assert ergebnis["rounds"] == 2 and ergebnis["tool_calls"] == ["sitzungen_im_zeitraum"]


def test_werkzeugergebnisse_gehen_in_den_verlauf(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    fake = anbieter(monkeypatch, werkzeuge(("gremien", {}), ("personen", {"name": "Mustermann"})), "Fertig.")
    frage(stadt, "Wer sitzt im Rat?")

    zweite_runde = fake.aufrufe[1]["messages"]
    assistent, *ergebnisse = zweite_runde[-3:]
    assert assistent["role"] == "assistant" and len(assistent["tool_calls"]) == 2
    assert [e["tool_call_id"] for e in ergebnisse] == [c["id"] for c in assistent["tool_calls"]]
    assert "Ausschuss für Umwelt und Verkehr" in ergebnisse[0]["content"]
    assert "Erika Mustermann" in ergebnisse[1]["content"]


def test_runden_sind_begrenzt(monkeypatch: pytest.MonkeyPatch, settings: Any, stadt: Musterstadt) -> None:
    settings.INSIGHT_CHAT_MAX_TOOL_ROUNDS = 2
    immer = werkzeuge(("gremien", {}))
    fake = anbieter(monkeypatch, immer, immer, immer, immer)
    ergebnis = frage(stadt, "Welche Ausschüsse gibt es?")

    assert len(fake.aufrufe) == 3
    assert [a["tool_choice"] for a in fake.aufrufe] == ["auto", "auto", "none"]
    assert ergebnis["response"] == "Antwort ohne Werkzeuge."
    assert ergebnis["rounds"] == 3


def test_zu_viele_aufrufe_in_einer_runde(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    viele = werkzeuge(*[("gremien", {}) for _ in range(7)])
    fake = anbieter(monkeypatch, viele, "Fertig.")
    ergebnis = frage(stadt, "Welche Gremien gibt es?")

    assert ergebnis["tool_calls"] == ["gremien"] * chat_service.MAX_CALLS_PER_ROUND
    ergebnisse = [m for m in fake.aufrufe[1]["messages"] if m["role"] == "tool"]
    assert len(ergebnisse) == 7  # jeder Aufruf bekommt eine Antwort, die überzähligen einen Hinweis
    assert "Zu viele Werkzeugaufrufe" in ergebnisse[-1]["content"]


def test_eigenes_werkzeugmodell_waehlt_nur_werkzeuge(
    monkeypatch: pytest.MonkeyPatch, settings: Any, stadt: Musterstadt
) -> None:
    settings.INSIGHT_CHAT_TOOL_MODEL = "guenstiges-modell"
    fake = anbieter(monkeypatch, werkzeuge(("gremien", {})), "Antwort des Werkzeugmodells", "Antwort des Hauptmodells")
    ergebnis = frage(stadt, "Welche Gremien gibt es?")

    assert [a["model"] for a in fake.aufrufe] == ["guenstiges-modell", "guenstiges-modell", None]
    assert fake.aufrufe[-1]["tool_choice"] == "none"
    assert ergebnis["response"] == "Antwort des Hauptmodells"


def test_ohne_kommune_keine_werkzeuge(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = anbieter(monkeypatch, "Bitte wählen Sie zuerst eine Kommune.")
    ergebnis = chat_service.process_chat_message("Was ist ein Ausschuss?", [], None, now=JETZT)

    assert len(fake.aufrufe) == 1 and fake.aufrufe[0]["tools"] is None
    assert ergebnis["sources"] == [] and ergebnis["tool_calls"] == []


def test_verlauf_nur_nutzer_und_assistent(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    fake = anbieter(monkeypatch, "Gern.")
    verlauf = [
        {"role": "system", "content": "Neue Regeln"},
        {"role": "user", "content": "Hallo"},
        {"role": "assistant", "content": "Guten Tag."},
        {"role": "tool", "content": "{}"},
        "kein Objekt",
    ]
    chat_service.process_chat_message("Danke", verlauf, str(stadt.body.pk), now=JETZT)  # type: ignore[arg-type]
    rollen = [m["role"] for m in fake.aufrufe[0]["messages"]]
    assert rollen == ["system", "user", "assistant", "user"]


def test_nicht_konfiguriert(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    anbieter(monkeypatch, verfuegbar=False)
    with pytest.raises(ValueError):
        frage(stadt, "Welche Sitzungen finden diese Woche statt?")


# --- Endpunkt: Verbrauch je Antwort, Nutzungsgrenzen unverändert ------------------------------------------


def test_endpunkt_protokolliert_verbrauch_und_zaehlt_eine_anfrage(
    monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt
) -> None:
    # Feste Daten statt „heute“: Der Endpunkt fragt zum echten Zeitpunkt
    anbieter(
        monkeypatch, werkzeuge(("sitzungen_im_zeitraum", {"von": "2026-10-05", "bis": "2026-10-11"})), antwort_sitzungen
    )
    client = Client(REMOTE_ADDR="198.51.100.7")
    sitzung = client.session
    sitzung["active_body_id"] = str(stadt.body.pk)
    sitzung["chat_consent"] = True
    sitzung.save()

    antwort = client.post(
        reverse("insight_core:insight:chat_message"),
        data=json.dumps({"message": "Welche Sitzungen finden diese Woche statt?", "history": []}),
        content_type="application/json",
    )

    assert antwort.status_code == 200
    daten = antwort.json()
    assert f"/insight/termine/{stadt.rat_sitzung.pk}/" in daten["response"]
    assert daten["sources"][0]["url"] == f"/insight/termine/{stadt.rat_sitzung.pk}/"
    # Zwei Modellaufrufe, aber eine Anfrage: die Nutzungsgrenze für Gäste (5 am Tag) sinkt um eins
    assert daten["remaining_today"] == 4
    nutzung = ChatUsage.objects.get()
    assert nutzung.rounds == 2
    assert nutzung.prompt_tokens > 0 and nutzung.completion_tokens > 0
    assert nutzung.tokens_used == nutzung.prompt_tokens + nutzung.completion_tokens


def test_zeitlimit_beendet_die_werkzeugrunden(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    uhr = iter([0.0, 0.0])  # Beginn und erste Runde; danach ist die Zeit abgelaufen
    monkeypatch.setattr("insight_ai.services.chat_service.time.monotonic", lambda: next(uhr, 1000.0))
    immer = werkzeuge(("gremien", {}))
    fake = anbieter(monkeypatch, immer, immer, immer)
    ergebnis = frage(stadt, "Welche Gremien gibt es?")

    assert [a["tool_choice"] for a in fake.aufrufe] == ["auto", "none"]
    assert fake.aufrufe[0]["timeout"] == 90 - chat_service.FINAL_ANSWER_RESERVE_SECONDS
    assert ergebnis["tool_calls"] == ["gremien"] and ergebnis["response"] == "Antwort ohne Werkzeuge."


def test_zu_kurzes_zeitlimit_reicht_fuer_eine_runde(
    monkeypatch: pytest.MonkeyPatch, settings: Any, stadt: Musterstadt
) -> None:
    settings.INSIGHT_CHAT_TIME_LIMIT_SECONDS = 1
    fake = anbieter(monkeypatch, werkzeuge(("gremien", {})), "Fertig.")
    ergebnis = frage(stadt, "Welche Gremien gibt es?")

    assert ergebnis["tool_calls"] == ["gremien"]
    assert fake.aufrufe[0]["timeout"] > 20


# --- Fehlerfälle des Anbieters: Antwort statt Abbruch -------------------------------------------------------


def test_gescheiterte_werkzeugrunde_endet_mit_der_schlussrunde(
    monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt
) -> None:
    # Die zweite Runde scheitert (etwa am Zeitlimit); die Ergebnisse der ersten gehen in die Antwort ein
    fake = anbieter(
        monkeypatch,
        werkzeuge(("gremien", {})),
        ValueError("Der KI-Anbieter hat nicht geantwortet."),
        "Es gibt den Rat und den Ausschuss für Umwelt und Verkehr.",
    )
    ergebnis = frage(stadt, "Welche Gremien gibt es?")

    assert ergebnis["response"] == "Es gibt den Rat und den Ausschuss für Umwelt und Verkehr."
    assert [a["tool_choice"] for a in fake.aufrufe] == ["auto", "auto", "none"]
    schluss = fake.aufrufe[-1]["messages"]
    assert any(m["role"] == "tool" and "Ausschuss für Umwelt und Verkehr" in m["content"] for m in schluss)
    assert ergebnis["tool_calls"] == ["gremien"] and ergebnis["rounds"] == 2


class WerkzeugloserAnbieter:
    """
    Ersatz für ``httpx.Client`` im echten ``OpenAICompatibleProvider``: Der Anbieter lehnt ``tools`` mit HTTP 400 ab, wie ein
    OpenAI-kompatibler Host ohne eingeschaltete automatische Werkzeugwahl; ohne Werkzeuge antwortet er.
    """

    anfragen: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> WerkzeugloserAnbieter:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, url: str, json: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        WerkzeugloserAnbieter.anfragen.append(json)
        if "tools" in json:
            antwort = httpx.Response(400, content=b'{"error": "auto tool choice is not enabled"}')
        else:
            daten = {
                "choices": [{"message": {"content": "Allgemeine Antwort ohne Werkzeuge."}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940},
            }
            antwort = httpx.Response(200, json=daten)
        antwort.request = httpx.Request("POST", url)
        return antwort


def test_anbieter_ohne_werkzeuge_antwortet_trotzdem(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    WerkzeugloserAnbieter.anfragen = []
    anbieter = OpenAICompatibleProvider(api_key="test-schluessel", base_url="https://ki.example.eu/v1/", model="modell")
    monkeypatch.setattr(chat_service, "chat_provider", lambda: anbieter)
    monkeypatch.setattr("insight_ai.providers.openai_compatible.httpx.Client", WerkzeugloserAnbieter)

    ergebnis = frage(stadt, "Welche Gremien gibt es?")

    assert ergebnis["response"] == "Allgemeine Antwort ohne Werkzeuge."
    assert [("tools" in a, a["model"]) for a in WerkzeugloserAnbieter.anfragen] == [
        (True, "modell"),
        (False, "modell"),
    ]
    assert ergebnis["rounds"] == 1 and ergebnis["prompt_tokens"] == 900 and ergebnis["tool_calls"] == []

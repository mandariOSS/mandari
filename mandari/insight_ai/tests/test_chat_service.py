# SPDX-License-Identifier: AGPL-3.0-or-later
"""
KI-Assistent (Issue #899): Fragen nach Terminen, Tagesordnungen und dem Stand von Vorlagen werden aus den
Ratsdaten mit Links beantwortet. Der Anbieter ist ersetzt (``SkriptAnbieter``) oder antwortet über
``httpx.MockTransport``; es geht kein Aufruf ins Netz. Anbieter ist immer der KI-Endpunkt des Bürgerportals aus der
zentralen KI-Konfiguration (``get_insight_provider``, Issue #950).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.common.ki_anbieter import KiEndpunkt, endpunkt_fuer_insight
from apps.common.models import AISettings
from insight_ai.providers import OpenAIKompatiblerProvider
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

STACKIT_HOST = "api.openai-compat.model-serving.eu01.onstackit.cloud"


@pytest.fixture
def stadt() -> Musterstadt:
    return baue_musterstadt()


@pytest.fixture(autouse=True)
def suche(monkeypatch: pytest.MonkeyPatch) -> FakeSuche:
    fake = FakeSuche()
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: fake)
    return fake


def anbieter(monkeypatch: pytest.MonkeyPatch, *schritte: Any, verfuegbar: bool = True) -> SkriptAnbieter:
    """Ersetzt den Anbieter, ob aus der Konfiguration aufgelöst oder für den Endpunkt der View gebaut."""
    fake = SkriptAnbieter(list(schritte), verfuegbar=verfuegbar)
    monkeypatch.setattr(chat_service, "get_insight_provider", lambda: fake)

    def fuer_endpunkt(endpunkt: KiEndpunkt) -> SkriptAnbieter:
        fake.endpunkte.append(endpunkt)
        return fake

    monkeypatch.setattr(chat_service, "OpenAIKompatiblerProvider", fuer_endpunkt)
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


def test_leere_antwort_fuehrt_zur_schlussrunde(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    fake = anbieter(monkeypatch, werkzeuge(("gremien", {})), "   ", "Es gibt den Rat.")
    ergebnis = frage(stadt, "Welche Gremien gibt es?")

    assert [a["tool_choice"] for a in fake.aufrufe] == ["auto", "auto", "none"]
    assert ergebnis["response"] == "Es gibt den Rat."


def test_nicht_eingerichtet_ohne_anfrage(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    # Ohne KI-Einstellungen liefert die zentrale Konfiguration keinen Endpunkt: keine Anfrage, fester Fehler
    cache.delete(AISettings.CACHE_KEY)
    gesendet: list[httpx.Request] = []
    echter_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        gesendet.append(request)
        return httpx.Response(500)

    def client(**kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return echter_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    with pytest.raises(ValueError, match="nicht eingerichtet"):
        frage(stadt, "Welche Gremien gibt es?")
    assert gesendet == []


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


@pytest.fixture(autouse=True)
def _freigabe(settings: Any) -> None:
    """Die Positivliste hat keinen Standard: Die Tests geben den Host der Vorlage ausdrücklich frei."""
    settings.KI_ERLAUBTE_HOSTS = [STACKIT_HOST]


def _ki_einrichten() -> None:
    """KI im Bürgerportal eingerichtet (Vorlage mit Host aus der Positivliste), wie im Admin."""
    cache.delete(AISettings.CACHE_KEY)
    ki = AISettings.get_settings()
    ki.provider = "stackit"
    ki.insight_enabled = True
    ki.insight_model = "modell-portal"
    ki.set_api_key("chat-testschluessel-geheim-0123456789")
    ki.save()


def _besucher_mit_einwilligung(stadt: Musterstadt, adresse: str = "198.51.100.7") -> tuple[Client, KiEndpunkt]:
    """Gast mit gewählter Kommune und Einwilligung für den eingerichteten Anbieter."""
    _ki_einrichten()
    endpunkt = endpunkt_fuer_insight()
    assert endpunkt is not None
    client = Client(REMOTE_ADDR=adresse)
    sitzung = client.session
    sitzung["active_body_id"] = str(stadt.body.pk)
    sitzung["chat_consent"] = endpunkt.hinweis().einwilligungskennung
    sitzung.save()
    return client, endpunkt


def _fragen(client: Client, text: str) -> Any:
    return client.post(
        reverse("insight_core:insight:chat_message"),
        data=json.dumps({"message": text, "history": []}),
        content_type="application/json",
    )


def test_endpunkt_protokolliert_verbrauch_und_zaehlt_eine_anfrage(
    monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt
) -> None:
    # Feste Daten statt „heute“: Der Endpunkt fragt zum echten Zeitpunkt
    fake = anbieter(
        monkeypatch, werkzeuge(("sitzungen_im_zeitraum", {"von": "2026-10-05", "bis": "2026-10-11"})), antwort_sitzungen
    )
    client, endpunkt = _besucher_mit_einwilligung(stadt)

    antwort = _fragen(client, "Welche Sitzungen finden diese Woche statt?")

    assert antwort.status_code == 200
    # Gefragt wird genau der Endpunkt, für den die Einwilligung gilt (Issue #950), nicht ein neu aufgelöster
    assert fake.endpunkte == [endpunkt]
    daten = antwort.json()
    assert f"/insight/termine/{stadt.rat_sitzung.pk}/" in daten["response"]
    assert daten["sources"][0]["url"] == f"/insight/termine/{stadt.rat_sitzung.pk}/"
    # Zwei Modellaufrufe, aber eine Anfrage: die Nutzungsgrenze für Gäste (5 am Tag) sinkt um eins
    assert daten["remaining_today"] == 4
    nutzung = ChatUsage.objects.get()
    assert nutzung.rounds == 2
    assert nutzung.prompt_tokens > 0 and nutzung.completion_tokens > 0
    assert nutzung.tokens_used == nutzung.prompt_tokens + nutzung.completion_tokens


# --- Kostenbremse: Tagesobergrenze aller Antworten zusammen -------------------------------------------------


def _nutzung(rounds: int, tokens: int, adresse: str = "203.0.113.9", **werte: Any) -> ChatUsage:
    return ChatUsage.objects.create(
        session_key="fremd",
        ip_address=adresse,
        message="Frage",
        filter_result="passed",
        tokens_used=tokens,
        rounds=rounds,
        **werte,
    )


@pytest.mark.parametrize(
    ("aufrufe", "token"),
    [(3, 10), (1, 5_000)],
    ids=["modellaufrufe", "token"],
)
def test_tagesobergrenze_stoppt_vor_dem_anbieter(
    monkeypatch: pytest.MonkeyPatch, settings: Any, stadt: Musterstadt, aufrufe: int, token: int
) -> None:
    """Andere Besucher haben das Tageskontingent verbraucht: keine Anfrage an den Anbieter, keine Buchung."""
    settings.INSIGHT_CHAT_DAILY_MAX_CALLS = 3
    settings.INSIGHT_CHAT_DAILY_MAX_TOKENS = 5_000
    fake = anbieter(monkeypatch, "Antwort.")
    client, _ = _besucher_mit_einwilligung(stadt)
    _nutzung(aufrufe, token)

    antwort = _fragen(client, "Welche Gremien gibt es?")

    assert antwort.status_code == 503
    assert antwort.json()["error"] == "daily_budget_reached" and "morgen" in antwort.json()["message"]
    assert fake.aufrufe == [] and ChatUsage.objects.count() == 1


def test_tagesobergrenze_zaehlt_nur_heute(monkeypatch: pytest.MonkeyPatch, settings: Any, stadt: Musterstadt) -> None:
    settings.INSIGHT_CHAT_DAILY_MAX_CALLS = 3
    settings.INSIGHT_CHAT_DAILY_MAX_TOKENS = 5_000
    fake = anbieter(monkeypatch, "Antwort.")
    client, _ = _besucher_mit_einwilligung(stadt)
    gestern = _nutzung(100, 1_000_000)
    ChatUsage.objects.filter(pk=gestern.pk).update(created_at=timezone.now() - timedelta(days=1, hours=1))
    _nutzung(1, 100)

    antwort = _fragen(client, "Welche Gremien gibt es?")

    assert antwort.status_code == 200 and len(fake.aufrufe) == 1


def test_tagesobergrenze_unter_der_grenze(monkeypatch: pytest.MonkeyPatch, settings: Any, stadt: Musterstadt) -> None:
    settings.INSIGHT_CHAT_DAILY_MAX_CALLS = 3
    settings.INSIGHT_CHAT_DAILY_MAX_TOKENS = 5_000
    fake = anbieter(monkeypatch, "Antwort.")
    client, _ = _besucher_mit_einwilligung(stadt)
    _nutzung(2, 4_999)

    assert _fragen(client, "Welche Gremien gibt es?").status_code == 200
    assert len(fake.aufrufe) == 1
    # Die neue Antwort zählt mit: Jetzt ist die Grenze erreicht
    assert _fragen(client, "Und welche Ausschüsse?").status_code == 503
    assert len(fake.aufrufe) == 1


def test_gescheiterte_antwort_bucht_ihren_verbrauch(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    """Scheitert die Schlussrunde nach einer Werkzeugrunde, zählen Anfrage und Verbrauch trotzdem."""
    anbieter(
        monkeypatch,
        werkzeuge(("gremien", {})),
        ValueError("Der KI-Anbieter hat nicht geantwortet."),
        ValueError("Der KI-Anbieter hat nicht geantwortet."),
    )
    client, _ = _besucher_mit_einwilligung(stadt)

    antwort = _fragen(client, "Welche Gremien gibt es?")

    assert antwort.status_code == 503 and antwort.json()["error"] == "ai_unavailable"
    nutzung = ChatUsage.objects.get()
    assert nutzung.filter_result == "passed" and nutzung.rounds == 1 and nutzung.tokens_used > 0
    assert nutzung.tokens_used == nutzung.prompt_tokens + nutzung.completion_tokens


def test_ohne_modellaufruf_gescheitert_keine_buchung(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    """Antwortet der Anbieter gar nicht, kostet das nichts und zählt nicht gegen die Nutzungsgrenze."""
    anbieter(monkeypatch, ValueError("weg"), ValueError("weg"))
    client, _ = _besucher_mit_einwilligung(stadt)

    assert _fragen(client, "Welche Gremien gibt es?").status_code == 503
    assert not ChatUsage.objects.exists()


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


def werkzeugloser_anbieter(anfragen: list[dict[str, Any]]) -> OpenAIKompatiblerProvider:
    """
    Echter ``OpenAIKompatiblerProvider`` mit ``httpx.MockTransport``: Der Anbieter lehnt ``tools`` mit HTTP 400 ab, wie
    ein OpenAI-kompatibler Host ohne eingeschaltete automatische Werkzeugwahl; ohne Werkzeuge antwortet er.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        nutzlast = json.loads(request.content)
        anfragen.append(nutzlast)
        if "tools" in nutzlast:
            return httpx.Response(400, json={"error": "auto tool choice is not enabled"})
        daten = {
            "choices": [{"message": {"content": "Allgemeine Antwort ohne Werkzeuge."}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940},
        }
        return httpx.Response(200, json=daten)

    endpunkt = KiEndpunkt(
        anbieter="stackit",
        anzeigename="STACKIT AI Model Serving",
        verarbeitungsort="Rechenzentren in Deutschland (EU)",
        base_url=f"https://{STACKIT_HOST}/v1",
        api_key="test-schluessel",
        modell="modell",
    )
    return OpenAIKompatiblerProvider(endpunkt, transport=httpx.MockTransport(handler))


def test_anbieter_ohne_werkzeuge_antwortet_trotzdem(monkeypatch: pytest.MonkeyPatch, stadt: Musterstadt) -> None:
    anfragen: list[dict[str, Any]] = []
    gewaehlt = werkzeugloser_anbieter(anfragen)
    monkeypatch.setattr(chat_service, "get_insight_provider", lambda: gewaehlt)

    ergebnis = frage(stadt, "Welche Gremien gibt es?")

    assert ergebnis["response"] == "Allgemeine Antwort ohne Werkzeuge."
    assert [("tools" in a, a["model"]) for a in anfragen] == [(True, "modell"), (False, "modell")]
    assert ergebnis["rounds"] == 1 and ergebnis["prompt_tokens"] == 900 and ergebnis["tool_calls"] == []

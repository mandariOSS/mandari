# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ablauf der Live-Übertragungen (Issue #915): Zeitfenster, Zustandsautomat mit Ereignissen, Standbild nach dem Ende,
Aussetzer, Leseaufträge, Entprellung, Leseschleife ohne Bildspeicher, Schalter aus = keine Anfragen.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import models
from PIL import Image

from apps.events import leases
from apps.events.models import Event, Task
from hub.live import services
from hub.live.anbieter import AnbieterError, Phase, StreamInfo, StreamStatus
from hub.live.lesung import Lesung
from hub.live.models import (
    Broadcast,
    BroadcastLog,
    BroadcastSection,
    BroadcastSpeech,
    BroadcastStatus,
    LogKind,
    SpeechAssignment,
)
from hub.live.profil import lade_profil, vorlage
from hub.live.tests.bilder import einblendung
from hub.live.tests.conftest import Welt, kennung
from insight_core.models import OParlMeeting

pytestmark = pytest.mark.django_db

PROFIL = lade_profil(vorlage("balken_unten_dreizeilig"))
LIVE = StreamStatus(online=True, phase=Phase.LIVE, zuschauer=12)
VORHER = StreamStatus(online=True, phase=Phase.VORHER, tafeltext="Die Sitzung beginnt um 16:15")
NACHHER = StreamStatus(online=True, phase=Phase.NACHHER, tafeltext="Die Sitzung wurde um 18:25 beendet")
OFFLINE = StreamStatus(online=False, phase=Phase.UNBEKANNT)


class Ersatzanbieter:
    """Anbieter ohne Netz: liefert den eingestellten Status und zählt Abrufe."""

    code = "3q"
    name = "3Q"
    player_ursprung: tuple[str, ...] = ("https://playout.3qsdn.com",)

    def __init__(self) -> None:
        self.naechster: StreamStatus | Exception = VORHER
        self.abrufe = 0

    def kennung_gueltig(self, kennung: str) -> bool:
        return True

    def aufloesen(self, kennung: str) -> StreamInfo:
        self.abrufe += 1
        return StreamInfo("63961", "https://cdn.example/63961/live.m3u8", f"https://playout.3qsdn.com/embed/{kennung}")

    def status(self, info: StreamInfo) -> StreamStatus:
        self.abrufe += 1
        if isinstance(self.naechster, Exception):
            raise self.naechster
        return self.naechster

    def einbettung_url(self, kennung: str) -> str | None:
        return f"https://playout.3qsdn.com/embed/{kennung}"


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> Ersatzanbieter:
    ersatz = Ersatzanbieter()
    monkeypatch.setattr(services, "anbieter", lambda code: ersatz)
    return ersatz


def _typen() -> list[str]:
    return list(Event.objects.order_by("id").values_list("type", flat=True))


def _schritt(welt: Welt, adapter: Ersatzanbieter, status: StreamStatus, jetzt: datetime) -> Broadcast:
    adapter.naechster = status
    broadcast = services.abfragen(welt.quelle, jetzt, adapter)
    assert broadcast is not None
    return broadcast


def _sitzung(welt: Welt, start: datetime, **felder: Any) -> OParlMeeting:
    sitzung = OParlMeeting.objects.create(external_id=kennung("meetings"), body=welt.body, start=start, **felder)
    sitzung.organizations.set([welt.rat])
    return sitzung


# =============================================================================
# Zeitfenster und Zustände
# =============================================================================


def test_zeitfenster_aus_den_oparl_sitzungen(welt: Welt) -> None:
    jetzt = welt.jetzt
    bald = _sitzung(welt, jetzt + timedelta(minutes=40))
    _sitzung(welt, jetzt + timedelta(hours=2))
    _sitzung(welt, jetzt - timedelta(hours=11))
    _sitzung(welt, jetzt, cancelled=True)
    _sitzung(welt, jetzt, deleted=True)
    assert services.sitzungen_im_fenster(welt.quelle, jetzt) == [welt.sitzung, bald]


def test_geplant_live_beendet_mit_ereignissen(welt: Welt, adapter: Ersatzanbieter) -> None:
    jetzt = welt.jetzt
    broadcast = _schritt(welt, adapter, VORHER, jetzt)
    assert broadcast.status == BroadcastStatus.GEPLANT and _typen() == []

    broadcast = _schritt(welt, adapter, LIVE, jetzt + timedelta(minutes=1))
    assert broadcast.status == BroadcastStatus.LIVE
    assert broadcast.started_at == jetzt + timedelta(minutes=1)
    assert broadcast.stream["hls_url"] == "https://cdn.example/63961/live.m3u8"
    assert _typen() == ["ris.broadcast.started"]
    gestartet = Event.objects.get()
    assert gestartet.aggregate_type == "Broadcast" and gestartet.aggregate_id == broadcast.pk
    assert gestartet.visibility == "oeffentlich"
    assert gestartet.payload == {
        "broadcast": str(broadcast.pk),
        "meeting": str(welt.sitzung.pk),
        "organization": str(welt.rat.pk),
    }
    assert Task.objects.filter(queue="live", task_path=services.LESEAUFTRAG).count() == 1, "Leseauftrag eingereiht"

    _schritt(welt, adapter, LIVE, jetzt + timedelta(minutes=2))
    assert Task.objects.filter(queue="live").count() == 1, "höchstens ein wartender Auftrag je Übertragung"

    broadcast = _schritt(welt, adapter, NACHHER, jetzt + timedelta(minutes=90))
    assert broadcast.status == BroadcastStatus.BEENDET
    assert broadcast.ended_at == jetzt + timedelta(minutes=90)
    assert _typen() == ["ris.broadcast.started", "ris.broadcast.ended"]
    assert Event.objects.last().payload["reason"] == "anbieter"  # type: ignore[union-attr]

    # Nach dem Ende sendet der Stream ein Standbild weiter (online, Tafel „post“): bleibt beendet
    broadcast = _schritt(welt, adapter, NACHHER, jetzt + timedelta(minutes=95))
    assert broadcast.status == BroadcastStatus.BEENDET
    assert len(_typen()) == 2
    zustaende = BroadcastLog.objects.filter(kind=LogKind.ZUSTAND).order_by("at").values_list("data", flat=True)
    assert [z["nach"] for z in zustaende] == ["live", "beendet"]


def test_aussetzer_und_wiederaufnahme(welt: Welt, adapter: Ersatzanbieter) -> None:
    jetzt = welt.jetzt
    _schritt(welt, adapter, LIVE, jetzt)
    broadcast = _schritt(welt, adapter, OFFLINE, jetzt + timedelta(minutes=5))
    assert broadcast.status == BroadcastStatus.LIVE and broadcast.offline_since == jetzt + timedelta(minutes=5)
    broadcast = _schritt(welt, adapter, VORHER, jetzt + timedelta(minutes=15))
    assert broadcast.status == BroadcastStatus.LIVE, "kurze Pause beendet nichts"
    broadcast = _schritt(welt, adapter, OFFLINE, jetzt + timedelta(minutes=21))
    assert broadcast.status == BroadcastStatus.BEENDET
    assert broadcast.ended_at == jetzt + timedelta(minutes=5), "Ende = Beginn des Aussetzers"
    assert Event.objects.last().payload["reason"] == "ohne_signal"  # type: ignore[union-attr]

    broadcast = _schritt(welt, adapter, LIVE, jetzt + timedelta(minutes=40))
    assert broadcast.status == BroadcastStatus.LIVE and broadcast.ended_at is None
    assert broadcast.started_at == jetzt, "Beginn bleibt"
    assert _typen() == ["ris.broadcast.started", "ris.broadcast.ended", "ris.broadcast.started"]


def test_nicht_uebertragen_und_ende_des_zeitfensters(welt: Welt, adapter: Ersatzanbieter) -> None:
    jetzt = welt.jetzt
    alt = _sitzung(welt, jetzt - timedelta(hours=3, minutes=1))
    _schritt(welt, adapter, VORHER, jetzt)
    assert Broadcast.objects.get(meeting=alt).status == BroadcastStatus.NICHT_UEBERTRAGEN

    lang = _sitzung(welt, jetzt - timedelta(hours=9, minutes=59))
    laufend = Broadcast.objects.create(source=welt.quelle, meeting=lang, status=BroadcastStatus.LIVE, started_at=jetzt)
    services.abfragen(welt.quelle, jetzt + timedelta(minutes=2), adapter)
    laufend.refresh_from_db()
    assert laufend.status == BroadcastStatus.BEENDET
    assert Event.objects.filter(type="ris.broadcast.ended", payload__reason="zeitfenster").count() == 1


def test_fehler_beim_anbieter_wird_protokolliert(welt: Welt, adapter: Ersatzanbieter) -> None:
    adapter.naechster = AnbieterError("Zeitgrenze")
    broadcast = services.abfragen(welt.quelle, welt.jetzt, adapter)
    assert broadcast is not None and broadcast.status == BroadcastStatus.GEPLANT
    fehler = BroadcastLog.objects.get(kind=LogKind.FEHLER)
    assert fehler.data == {"wo": "status", "fehler": "Zeitgrenze"}


def test_abfragen_protokolliert_ohne_bilder(welt: Welt, adapter: Ersatzanbieter) -> None:
    adapter.naechster = StreamStatus(online=True, phase=Phase.LIVE, zuschauer=3, roh={"IsOnline": True})
    services.abfragen(welt.quelle, welt.jetzt, adapter)
    abfrage = BroadcastLog.objects.get(kind=LogKind.ABFRAGE)
    assert abfrage.data["zuschauer"] == 3 and abfrage.data["phase"] == "live"


def test_schalter_aus_keine_anfragen(welt: Welt, adapter: Ersatzanbieter, settings: Any) -> None:
    settings.LIVE_UEBERTRAGUNG_AKTIV = False
    assert services.alle_abfragen(welt.jetzt) == 0
    assert adapter.abrufe == 0
    broadcast = Broadcast.objects.create(source=welt.quelle, meeting=welt.sitzung, status=BroadcastStatus.LIVE)
    assert services.lesen(broadcast.pk, bildquelle=lambda url: pytest.fail("kein Abruf")) == 0
    assert not services.einreihen(broadcast)


def test_alle_abfragen_nur_aktive_quellen(welt: Welt, adapter: Ersatzanbieter) -> None:
    assert services.alle_abfragen(welt.jetzt) == 1
    welt.quelle.active = False
    welt.quelle.save()
    assert services.alle_abfragen(welt.jetzt) == 0


# =============================================================================
# Entprellung, Abschnitte, Wortmeldungen
# =============================================================================


@pytest.fixture
def laufend(welt: Welt) -> Broadcast:
    return Broadcast.objects.create(
        source=welt.quelle,
        meeting=welt.sitzung,
        status=BroadcastStatus.LIVE,
        started_at=welt.jetzt,
        stream={"stream_id": "63961", "hls_url": "https://cdn.example/live.m3u8", "embed_url": None},
    )


def _lesung(top: str | None = "5", name: str | None = "Erika Muster", fraktion: str | None = "Fraktion A") -> Lesung:
    return Lesung(balken=True, top=top, titel="Neubau einer Grundschule", name=name, fraktion=fraktion)


def test_wechsel_erst_nach_zwei_gleichen_lesungen(welt: Welt, laufend: Broadcast) -> None:
    jetzt = welt.jetzt
    erste = services.lesung_verarbeiten(laufend.pk, _lesung(), jetzt, PROFIL)
    assert erste.live and erste.neuer_abschnitt is None and erste.neue_wortmeldung is None
    assert not BroadcastSection.objects.exists() and not BroadcastSpeech.objects.exists()

    zweite = services.lesung_verarbeiten(laufend.pk, _lesung(), jetzt + timedelta(seconds=10), PROFIL)
    abschnitt = zweite.neuer_abschnitt
    assert abschnitt is not None and abschnitt.agenda_item == welt.top5 and abschnitt.number == "5"
    assert abschnitt.title_similarity == 1.0
    wortmeldung = zweite.neue_wortmeldung
    assert wortmeldung is not None
    assert (wortmeldung.person, wortmeldung.assignment) == (welt.muster, SpeechAssignment.EINDEUTIG)
    assert (wortmeldung.faction_read, wortmeldung.readings, wortmeldung.section) == ("Fraktion A", 2, abschnitt)
    assert _typen() == ["ris.broadcast.agenda_item_started", "ris.broadcast.speaker_changed"]
    top_ereignis, sprecher_ereignis = Event.objects.order_by("id")
    assert top_ereignis.payload["agenda_item"] == str(welt.top5.pk) and top_ereignis.payload["number"] == "5"
    assert sprecher_ereignis.visibility == "intern"
    assert sprecher_ereignis.payload["person"] == str(welt.muster.pk)
    assert "Muster" not in json.dumps(sprecher_ereignis.payload), "keine gelesenen Texte im Ereignis"

    services.lesung_verarbeiten(laufend.pk, _lesung(), jetzt + timedelta(seconds=20), PROFIL)
    wortmeldung.refresh_from_db()
    assert wortmeldung.readings == 3, "gleiche Lesung zählt nur hoch"
    assert BroadcastSection.objects.count() == 1 and BroadcastSpeech.objects.count() == 1


def test_einzelne_fehllesung_wechselt_nicht(welt: Welt, laufend: Broadcast) -> None:
    jetzt = welt.jetzt
    for sekunde, top in enumerate(["5", "5", "6", "5", "8", "5"]):
        services.lesung_verarbeiten(laufend.pk, _lesung(top=top), jetzt + timedelta(seconds=sekunde), PROFIL)
    assert list(BroadcastSection.objects.values_list("number", flat=True)) == ["5"]


def test_neuer_top_schliesst_den_vorigen(welt: Welt, laufend: Broadcast) -> None:
    jetzt = welt.jetzt
    for sekunde, top in enumerate(["5", "5", "5.1", "5.1"]):
        services.lesung_verarbeiten(laufend.pk, _lesung(top=top), jetzt + timedelta(seconds=sekunde), PROFIL)
    erster, zweiter = BroadcastSection.objects.order_by("started_at")
    assert erster.ended_at == zweiter.started_at
    assert zweiter.agenda_item == welt.top51
    laufend.refresh_from_db()
    assert laufend.current_section == zweiter


def test_top_reihenfolge_ist_nicht_monoton(welt: Welt, laufend: Broadcast) -> None:
    """Zurück zu einem früheren TOP (vertagt, wieder aufgerufen): neuer Abschnitt, nichts wird verworfen."""
    jetzt = welt.jetzt
    for sekunde, top in enumerate(["5", "5", "1", "1", "5.1", "5.1", "1", "1"]):
        services.lesung_verarbeiten(laufend.pk, _lesung(top=top), jetzt + timedelta(seconds=sekunde * 10), PROFIL)
    abschnitte = list(BroadcastSection.objects.order_by("started_at"))
    assert [a.number for a in abschnitte] == ["5", "1", "5.1", "1"]
    assert [a.agenda_item for a in abschnitte] == [welt.top5, welt.top1, welt.top51, welt.top1]
    assert all(a.ended_at == b.started_at for a, b in zip(abschnitte, abschnitte[1:], strict=False))
    assert abschnitte[-1].ended_at is None
    assert Event.objects.filter(type="ris.broadcast.agenda_item_started").count() == 4


def test_lesarten_einer_fraktion_sind_keine_neue_wortmeldung(welt: Welt, laufend: Broadcast) -> None:
    """Umlautfehler und abgeschnittene Bezeichnungen: dieselbe Person bleibt am Wort, keine Dubletten."""
    jetzt = welt.jetzt
    lesarten = ["Fraktion Grün-Süd", "Fraktion Grün-Süd", "Fraktion Grun-Sud", "Fraktion Gru�n-Su�d"]
    for sekunde, fraktion in enumerate(lesarten):
        services.lesung_verarbeiten(
            laufend.pk, _lesung(fraktion=fraktion), jetzt + timedelta(seconds=sekunde * 10), PROFIL
        )
    wortmeldung = BroadcastSpeech.objects.get()
    assert (wortmeldung.faction_read, wortmeldung.readings) == ("Fraktion Grün-Süd", 4)

    # Andere Person, lange Bezeichnung zuerst vollständig, dann abgeschnitten gelesen
    beginn = jetzt + timedelta(minutes=1)
    for sekunde, fraktion in enumerate(
        [
            "Internationale Fraktion Beispielpartei",
            "Internationale Fraktion Beispielpartei",
            "Internationale Fraktion Bei",
        ]
    ):
        services.lesung_verarbeiten(
            laufend.pk,
            _lesung(name="Max Beispiel", fraktion=fraktion),
            beginn + timedelta(seconds=sekunde * 10),
            PROFIL,
        )
    zweite = BroadcastSpeech.objects.exclude(pk=wortmeldung.pk).get()
    assert (zweite.faction_read, zweite.readings) == ("Internationale Fraktion Beispielpartei", 3)
    assert set(BroadcastSpeech.objects.values_list("faction_read", flat=True)) == {
        "Fraktion Grün-Süd",
        "Internationale Fraktion Beispielpartei",
    }


def test_stream_ohne_hls_adresse_wird_neu_aufgeloest(welt: Welt, adapter: Ersatzanbieter) -> None:
    broadcast = Broadcast.objects.create(
        source=welt.quelle, meeting=welt.sitzung, stream={"stream_id": "63961", "hls_url": None, "embed_url": None}
    )
    _schritt(welt, adapter, LIVE, welt.jetzt)
    broadcast.refresh_from_db()
    assert broadcast.stream["hls_url"] == "https://cdn.example/63961/live.m3u8"
    assert Task.objects.filter(queue="live", task_path=services.LESEAUFTRAG).count() == 1


def test_funktion_und_unbekannte_person(welt: Welt, laufend: Broadcast) -> None:
    jetzt = welt.jetzt
    lesung = Lesung(balken=True, top=None, name="Gisela Gast", fraktion=None, funktion="Bürgermeisterin")
    services.lesung_verarbeiten(laufend.pk, lesung, jetzt, PROFIL)
    services.lesung_verarbeiten(laufend.pk, lesung, jetzt + timedelta(seconds=10), PROFIL)
    wortmeldung = BroadcastSpeech.objects.get()
    assert (wortmeldung.function_read, wortmeldung.faction_read) == ("Bürgermeisterin", "")
    assert (wortmeldung.person, wortmeldung.assignment, wortmeldung.section) == (None, SpeechAssignment.KEINE, None)


def test_ohne_einblendung_und_nach_dem_ende(welt: Welt, laufend: Broadcast) -> None:
    ergebnis = services.lesung_verarbeiten(laufend.pk, Lesung(balken=False), welt.jetzt, PROFIL)
    assert ergebnis.live
    laufend.refresh_from_db()
    assert (laufend.frames_read, laufend.frames_without_overlay) == (1, 1)
    laufend.status = BroadcastStatus.BEENDET
    laufend.save()
    assert not services.lesung_verarbeiten(laufend.pk, _lesung(), welt.jetzt, PROFIL).live


def test_abschnitt_von_hand(welt: Welt, laufend: Broadcast) -> None:
    abschnitt = services.abschnitt_von_hand(laufend, "TOP 1", welt.jetzt)
    assert (abschnitt.agenda_item, abschnitt.origin) == (welt.top1, "hand")
    assert Event.objects.get().payload["origin"] == "hand"


# =============================================================================
# Leseschleife
# =============================================================================


class Uhr:
    def __init__(self) -> None:
        self.zeit = 0.0

    def __call__(self) -> float:
        return self.zeit

    def schlafen(self, sekunden: float) -> None:
        self.zeit += sekunden


def _erkenner() -> Iterator[str]:
    while True:
        yield from ("TOP 5", "Erika Muster", "Fraktion A", "Neubau einer Grundschule")


def test_leseschleife_im_takt_ohne_bildspeicher(welt: Welt, laufend: Broadcast) -> None:
    uhr = Uhr()
    texte = _erkenner()
    abgerufen: list[str] = []

    def bildquelle(url: str) -> Image.Image:
        abgerufen.append(url)
        return einblendung()

    gelesen = services.lesen(
        laufend.pk,
        dauer=50,
        bildquelle=bildquelle,
        erkenner=lambda bild, psm: next(texte),
        schlafen=uhr.schlafen,
        uhr=uhr,
    )
    assert gelesen == 5, "alle 10 Sekunden ein Bild"
    assert abgerufen == ["https://cdn.example/live.m3u8"] * 5
    assert BroadcastSection.objects.get().agenda_item == welt.top5
    assert BroadcastSpeech.objects.get().readings == 5
    lesungen = list(BroadcastLog.objects.filter(kind=LogKind.LESUNG).values_list("data", flat=True))
    assert len(lesungen) == 5
    assert lesungen[0]["breite"] == 1920 and lesungen[0]["name"] == "Erika Muster"
    assert all(isinstance(wert, str | int | float | bool | type(None)) for d in lesungen for wert in d.values())
    assert leases.acquire(f"live.lesen.{laufend.pk}", "fremd"), "Sperre wieder frei"


def test_leseschleife_bildfehler_und_sperre(welt: Welt, laufend: Broadcast) -> None:
    uhr = Uhr()

    def kaputt(url: str) -> Image.Image:
        raise AnbieterError("HTTP 404")

    assert services.lesen(laufend.pk, dauer=20, bildquelle=kaputt, schlafen=uhr.schlafen, uhr=uhr) == 0
    assert BroadcastLog.objects.filter(kind=LogKind.FEHLER).count() == 2

    assert leases.acquire(f"live.lesen.{laufend.pk}", "anderer-auftrag", ttl=timedelta(minutes=5))
    assert services.lesen(laufend.pk, bildquelle=lambda url: pytest.fail("kein zweiter Leser")) == 0


def test_keine_bild_oder_tonfelder_in_den_modellen() -> None:
    """Bild und Ton werden nie gespeichert: Die App hat keine Datei- oder Binärfelder."""
    from django.apps import apps

    for modell in apps.get_app_config("hub_live").get_models():
        for feld in modell._meta.get_fields():
            assert not isinstance(feld, models.BinaryField | models.FileField), f"{modell.__name__}.{feld.name}"


def test_protokoll_aufraeumen(welt: Welt) -> None:
    alt = BroadcastLog.objects.create(source=welt.quelle, at=welt.jetzt - timedelta(days=91), kind=LogKind.ABFRAGE)
    neu = BroadcastLog.objects.create(source=welt.quelle, at=welt.jetzt - timedelta(days=89), kind=LogKind.ABFRAGE)
    assert services.protokoll_aufraeumen(welt.jetzt) == 1
    assert not BroadcastLog.objects.filter(pk=alt.pk).exists() and BroadcastLog.objects.filter(pk=neu.pk).exists()

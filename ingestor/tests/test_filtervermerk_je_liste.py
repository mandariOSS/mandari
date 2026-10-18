# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vermerk „filtert nicht“ je Liste und je Quelle (Issue #1041).

Befund aus dem Betrieb am 16.10.2026: ``body/1/location?modified_since=…`` antwortet bei der Stadt Hamm und bei der
Bezirksregierung Köln (beide SD.NET RIM) mit HTTP 400, die Stadt Münster beantwortet den Filter bei ``organization``
und ``agendaitem`` mit HTTP 401. Der Client vermerkte daraufhin den ganzen Server als „filtert nicht“, prozessweit, und
der Orchestrator schrieb neu vermerkte Server in die ``sync_config`` der gerade laufenden Quelle. Folge: Hamm lief ganz
ohne Filter, ``paper`` und ``meeting`` brachen nach 60 Sekunden mit HTTP 504 ab; der Server der Bezirksregierung stand
in der ``sync_config`` von Münster und Hamm.

Jetzt gilt der Vermerk für die eine Liste und nur in der Quelle, bei der er entstand, mit täglicher Prüfung. Die
Abgleiche laufen über ``SyncOrchestrator.sync_body_url`` wie im Daemon, Antworten aus ``httpx.MockTransport``, keine
Abrufe. Der Speicher hält die ``sync_config`` je Quelle und schreibt über die echten Methoden von ``DatabaseStorage``.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid5

import httpx
import pytest
from mandari_oparl import IdBases
from mandari_oparl.utils import parse_datetime

from src.storage.database import DatabaseStorage
from src.sync.orchestrator import SyncOrchestrator, SyncResult
from tests.test_aenderungsfenster import (  # noqa: F401 - _ohne_nebenlaeufe: autouse-Fixture
    Speicher,
    _ohne_nebenlaeufe,
    _orchestrator,
)

#: Gemeinsamer Server zweier SD.NET-Quellen (Bezirksregierung Köln und Stadt Hamm, nachgebildet)
RIM = "rim.example"
MUENSTER = "oparl.muenster.example"
GREMIEN = "mandant.gremien.example"
TYPEN = {
    "organization": "Organization",
    "person": "Person",
    "meeting": "Meeting",
    "paper": "Paper",
    "location": "Location",
    "agendaitem": "AgendaItem",
}
#: Feld der Liste im Body-Objekt (``OParlProcessor.process_body``)
BODY_FELD = {"location": "locationList", "agendaitem": "agendaItem"}
UNFILTERED_LISTS = "unfiltered_lists"
UNFILTERED_HOSTS = "unfiltered_hosts"
LEGACY = "modified_since_unsupported_hosts"
OPARL = "https://schema.oparl.org/1.1/"


class Ris:
    """
    OParl-Schnittstelle einer Quelle mit einer Kommune: filtert mit ``modified_since`` und blättert in Seiten zu zwei
    Objekten. ``lehnt_ab``: Listen, die den Filter mit diesem Statuscode ablehnen; ``ungefiltert_504``: Listen, die
    ohne Filter nach der Zeitgrenze mit HTTP 504 abbrechen (Hamm: ``paper``, ``meeting``); ``verliert_filter``:
    ``links.next`` ohne ``modified_since`` (ALLRIS, Münster).
    """

    def __init__(self, host: str, pfad: str = "", listen: tuple[str, ...] = tuple(TYPEN)) -> None:
        self.host = host
        self.pfad = f"{pfad}/oparl"
        self.basis = f"https://{host}{self.pfad}"
        self.body = f"{self.basis}/body/1"
        alt = (datetime.now(UTC) - timedelta(days=3)).isoformat()
        self.listen: dict[str, list[dict[str, Any]]] = {
            name: [
                {"id": f"{self.basis}/{name}/{i}", "type": f"{OPARL}{TYPEN[name]}", "modified": alt} for i in range(3)
            ]
            for name in listen
        }
        self.lehnt_ab: dict[str, int] = {}
        self.ungefiltert_504: set[str] = set()
        self.verliert_filter = False
        #: Liste -> Ereignis, gesetzt bei der ersten gefilterten Anfrage an sie
        self.gefiltert_gesehen: dict[str, asyncio.Event] = {}
        #: Liste -> Ereignis, auf das die Antwort an diese Liste wartet
        self.wartet_auf: dict[str, asyncio.Event] = {}
        self.anfragen: list[httpx.URL] = []

    def liste(self, name: str) -> str:
        return f"{self.body}/{name}"

    def body_objekt(self) -> dict[str, Any]:
        daten: dict[str, Any] = {"id": self.body, "type": f"{OPARL}Body", "name": self.host}
        for name in self.listen:
            daten[BODY_FELD.get(name, name)] = self.liste(name)
        return daten

    async def antworten(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request.url)
        if str(request.url) == self.body:
            return httpx.Response(200, json=self.body_objekt())
        name = request.url.path.rsplit("/", 1)[1]
        params = dict(request.url.params)
        gefiltert = "modified_since" in params
        if gefiltert and name in self.gefiltert_gesehen:
            self.gefiltert_gesehen[name].set()
        if name in self.wartet_auf:
            await self.wartet_auf[name].wait()
        objekte = self.listen.get(name)
        if objekte is None:
            return httpx.Response(404)
        if gefiltert and name in self.lehnt_ab:
            return httpx.Response(self.lehnt_ab[name])
        if not gefiltert and name in self.ungefiltert_504:
            return httpx.Response(504)
        if gefiltert:
            seit = parse_datetime(params["modified_since"])
            assert seit is not None
            objekte = [o for o in objekte if parse_datetime(o["modified"]) >= seit]
        seite = int(params.get("page", "1"))
        links: dict[str, str] = {}
        if seite * 2 < len(objekte):
            weiter = {**params, "page": str(seite + 1)}
            if self.verliert_filter:
                weiter.pop("modified_since", None)
            links["next"] = f"https://{self.host}{request.url.path}?{urlencode(weiter)}"
        return httpx.Response(200, json={"data": objekte[(seite - 1) * 2 : seite * 2], "links": links})

    def filter_je_anfrage(self, name: str) -> list[bool]:
        """Für jede Anfrage an die Liste: mit ``modified_since``?"""
        return ["modified_since" in url.params for url in self.anfragen if url.path.endswith(f"/body/1/{name}")]


class Netz:
    """Leitet jede Anfrage an die nachgebildete Quelle ihres Servers und Pfades."""

    def __init__(self, *quellen: Ris) -> None:
        self.quellen = quellen

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        for ris in self.quellen:
            if request.url.host == ris.host and request.url.path.startswith(ris.pfad + "/"):
                return await ris.antworten(request)
        return httpx.Response(404)


class QuellenSpeicher(Speicher):
    """
    Speicher mit ``sync_config`` je Quelle (Adresse -> Wörterbuch) wie die Tabelle der Quellen. Geschrieben wird über
    die echten Methoden von ``DatabaseStorage``, nur ihre Zeilensperre (``_change_sync_config``) ist nachgebildet.
    """

    def __init__(self) -> None:
        super().__init__()
        self.id_bases = IdBases()
        self.quellen: dict[str, dict[str, Any]] = {}
        self.versionen: dict[str, str] = {}
        self.schreibvorgaenge = 0
        self._db = DatabaseStorage.__new__(DatabaseStorage)
        self._db._change_sync_config = self._aendern  # type: ignore[method-assign]

    async def _aendern(self, where: Any, change: Callable[[dict[str, Any]], bool]) -> None:
        url = where.right.value  # OParlSource.url == source_url
        kopie = copy.deepcopy(self.quellen.setdefault(url, {}))
        if change(kopie):
            self.quellen[url] = kopie
            self.schreibvorgaenge += 1

    async def get_source_by_url(self, url: str) -> Any:
        return SimpleNamespace(
            url=url,
            sync_config=copy.deepcopy(self.quellen.get(url)),
            user_agent=None,
            last_successful_full_sync=None,
            oparl_version=self.versionen.get(url),
        )

    async def upsert_source(self, url: str, **kwargs: Any) -> UUID:
        return uuid5(UUID(int=0), url)

    async def update_source_sync_time(self, source_id: UUID, **kwargs: Any) -> None:
        return None

    async def record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        return None

    async def save_filter_notes(self, *args: Any, **kwargs: Any) -> None:
        await self._db.save_filter_notes(*args, **kwargs)

    async def set_modified_since_check(self, *args: Any) -> None:
        await self._db.set_modified_since_check(*args)

    # Schnittstelle bis Issue #1041 (prozessweiter Zwischenspeicher je Server): bleibt nachgebildet, damit diese Tests
    # gegen den alten Stand mit fachlichen Befunden scheitern und nicht an fehlenden Methoden (Gegenprobe)
    async def get_modified_since_unsupported_hosts(self) -> set[str]:
        return {host for config in self.quellen.values() for host in config.get(LEGACY, [])}

    async def add_modified_since_unsupported_hosts(self, source_url: str, hosts: set[str]) -> None:
        config = self.quellen.setdefault(source_url, {})
        config[LEGACY] = sorted(set(config.get(LEGACY, [])) | set(hosts))


@pytest.fixture
def netz(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Netz]]:
    """Leitet den HTTP-Client des Ingestors auf die nachgebildeten Quellen um."""
    echter_client = httpx.AsyncClient

    def einrichten(*quellen: Ris) -> Netz:
        verteiler = Netz(*quellen)
        monkeypatch.setattr(
            "src.client.oparl_client.httpx.AsyncClient",
            lambda *args, **kwargs: echter_client(transport=httpx.MockTransport(verteiler), **kwargs),
        )
        return verteiler

    yield einrichten


def _bereit(speicher: QuellenSpeicher, *quellen: Ris) -> SyncOrchestrator:
    """Orchestrator wie im Daemon; jede Kommune vor einer Stunde vollständig abgeglichen (kurzes Fenster)."""
    for ris in quellen:
        speicher.begonnen[ris.body] = datetime.now(UTC) - timedelta(hours=1)
    return _orchestrator(speicher)


async def _abgleich(orchestrator: SyncOrchestrator, ris: Ris) -> SyncResult:
    ris.anfragen.clear()
    return await orchestrator.sync_body_url(ris.body)


def _vermerkte_listen(speicher: QuellenSpeicher, ris: Ris) -> set[str]:
    return set(speicher.quellen.get(ris.body, {}).get(UNFILTERED_LISTS, {}))


def _hamm() -> Ris:
    """Stadt Hamm (SD.NET RIM): ``location`` lehnt den Filter mit HTTP 400 ab, ohne Filter 504 bei paper/meeting."""
    ris = Ris(RIM, "/hamm", listen=("organization", "person", "meeting", "paper", "location"))
    ris.lehnt_ab["location"] = 400
    ris.ungefiltert_504 = {"paper", "meeting"}
    return ris


# ---------------------------------------------------------------------------
# Akzeptanzkriterien
# ---------------------------------------------------------------------------


async def test_400_auf_einer_liste_laesst_die_uebrigen_listen_gefiltert(netz):
    """Hamm: Nur ``location`` läuft ohne Filter; Vorlagen und Sitzungen bleiben gefiltert, kein 504."""
    hamm = _hamm()
    netz(hamm)
    speicher = QuellenSpeicher()
    orchestrator = _bereit(speicher, hamm)

    # Erster Lauf: location einmal mit Filter (400), dann ohne (zwei Seiten). Zweiter Lauf: gleich ohne Filter
    for lauf, location in enumerate(([True, False, False], [False, False])):
        result = await _abgleich(orchestrator, hamm)
        assert result.success and result.errors == [], (lauf, result.errors)
        for name in ("organization", "person", "meeting", "paper"):
            assert hamm.filter_je_anfrage(name) == [True], (lauf, name)
        assert hamm.filter_je_anfrage("location") == location, lauf

    # Vermerkt ist nur location, in der sync_config der Quelle
    assert speicher.quellen[hamm.body].keys() == {UNFILTERED_LISTS}
    assert speicher.quellen[hamm.body][UNFILTERED_LISTS].keys() == {hamm.liste("location")}
    assert speicher.quellen[hamm.body][UNFILTERED_LISTS][hamm.liste("location")]["reason"] == "HTTP 400"


async def test_kein_vermerk_in_fremden_quellen(netz):
    """
    Bezirksregierung und Münster laufen parallel, die Bezirksregierung stößt auf HTTP 400, während Münster läuft. Der
    Vermerk landet nur bei der Bezirksregierung; Hamm auf demselben Server läuft danach mit allen Listen gefiltert.
    """
    bezreg = Ris(RIM, "/bezreg", listen=("organization", "person", "meeting", "paper", "location"))
    bezreg.lehnt_ab["location"] = 400
    muenster = Ris(MUENSTER)
    hamm = _hamm()
    hamm.lehnt_ab.clear()  # filtert jede Liste
    netz(bezreg, muenster, hamm)
    speicher = QuellenSpeicher()
    orchestrator = _bereit(speicher, bezreg, muenster, hamm)

    # Münster beendet seinen Lauf erst, nachdem die Bezirksregierung den Filter von location angefragt hat
    gesehen = bezreg.gefiltert_gesehen["location"] = asyncio.Event()
    muenster.wartet_auf["location"] = gesehen
    ergebnisse = await asyncio.wait_for(
        asyncio.gather(_abgleich(orchestrator, bezreg), _abgleich(orchestrator, muenster)), timeout=30
    )
    assert all(result.success and result.errors == [] for result in ergebnisse)
    for config in (speicher.quellen.get(muenster.body, {}), speicher.quellen.get(hamm.body, {})):
        assert not {UNFILTERED_LISTS, UNFILTERED_HOSTS, LEGACY} & set(config), config

    # Hamm auf demselben Server: jede Liste mit Filter, kein 504 bei Vorlagen und Sitzungen
    result = await _abgleich(orchestrator, hamm)
    assert result.success and result.errors == [], result.errors
    for name in hamm.listen:
        assert hamm.filter_je_anfrage(name) == [True], name
    assert not {UNFILTERED_LISTS, UNFILTERED_HOSTS, LEGACY} & set(speicher.quellen.get(hamm.body, {}))

    # Der Vermerk steht bei der Quelle, bei der er entstand
    assert speicher.quellen[bezreg.body].keys() == {UNFILTERED_LISTS}
    assert _vermerkte_listen(speicher, bezreg) == {bezreg.liste("location")}


async def test_alte_serververmerke_werden_uebernommen(netz):
    """
    Bestand in Produktion: Hamm trägt seinen eigenen Server (nach dem 400 bei location) und den Server einer anderen
    Quelle. Beide fallen weg, Vorlagen und Sitzungen laufen gefiltert, location wird je Liste neu vermerkt. Eine
    Quelle mit OParl 1.0, die den Filter nachweislich ignoriert, behält ihren Server. Ein zweiter Lauf ändert nichts.
    """
    hamm = _hamm()
    gremien = Ris(GREMIEN, listen=("organization", "paper"))
    netz(hamm, gremien)
    speicher = QuellenSpeicher()
    speicher.quellen[hamm.body] = {LEGACY: [RIM, MUENSTER], "probe": {"system": "oparl"}}
    speicher.quellen[gremien.body] = {LEGACY: [GREMIEN]}
    speicher.versionen[gremien.body] = "1.0"
    orchestrator = _bereit(speicher, hamm, gremien)

    result = await _abgleich(orchestrator, hamm)
    assert result.success and result.errors == [], result.errors
    assert hamm.filter_je_anfrage("paper") == [True] and hamm.filter_je_anfrage("meeting") == [True]
    assert speicher.quellen[hamm.body].keys() == {"probe", UNFILTERED_LISTS}
    assert speicher.quellen[hamm.body][UNFILTERED_LISTS].keys() == {hamm.liste("location")}

    result = await _abgleich(orchestrator, gremien)
    assert result.success and result.errors == [], result.errors
    assert gremien.filter_je_anfrage("paper") == [False, False]  # zwei Seiten, nie mit Filter
    assert LEGACY not in speicher.quellen[gremien.body]
    assert set(speicher.quellen[gremien.body][UNFILTERED_HOSTS]) == {GREMIEN}

    # Idempotent: ohne alten Schlüssel gibt es nichts mehr zu übernehmen und nichts zu schreiben
    vorher = copy.deepcopy(speicher.quellen)
    schreibvorgaenge = speicher.schreibvorgaenge
    for ris in (hamm, gremien):
        assert (await _abgleich(orchestrator, ris)).success
    assert speicher.quellen == vorher
    assert speicher.schreibvorgaenge == schreibvorgaenge


async def test_vermerk_wird_einmal_am_tag_neu_geprueft(netz):
    """Vor einer Stunde geprüft: ohne Filter. Vor über einem Tag: mit Filter; filtert der Server jetzt, endet er."""
    hamm = _hamm()
    netz(hamm)
    speicher = QuellenSpeicher()
    orchestrator = _bereit(speicher, hamm)
    location = hamm.liste("location")

    def vermerkt_vor(alter: timedelta) -> None:
        geprueft = (datetime.now(UTC) - alter).isoformat(timespec="seconds")
        speicher.quellen[hamm.body] = {UNFILTERED_LISTS: {location: {"reason": "HTTP 400", "checked_at": geprueft}}}

    # Vor einer Stunde geprüft: keine Anfrage mit Filter (zwei Seiten ohne), nichts zu schreiben
    vermerkt_vor(timedelta(hours=1))
    vorher = copy.deepcopy(speicher.quellen)
    assert (await _abgleich(orchestrator, hamm)).success
    assert hamm.filter_je_anfrage("location") == [False, False]
    assert speicher.quellen == vorher

    # Vor mehr als einem Tag geprüft und weiter abgelehnt: mit Filter (400), dann ohne; Vermerk gilt wieder einen Tag
    vermerkt_vor(timedelta(days=1, minutes=5))
    jetzt = datetime.now(UTC).replace(microsecond=0)
    assert (await _abgleich(orchestrator, hamm)).success
    assert hamm.filter_je_anfrage("location") == [True, False, False]
    geprueft = datetime.fromisoformat(speicher.quellen[hamm.body][UNFILTERED_LISTS][location]["checked_at"])
    assert geprueft >= jetzt

    # Der Server filtert die Liste inzwischen: eine Anfrage mit Filter, der Vermerk endet
    hamm.lehnt_ab.clear()
    vermerkt_vor(timedelta(days=2))
    result = await _abgleich(orchestrator, hamm)
    assert result.success and result.errors == [], result.errors
    assert hamm.filter_je_anfrage("location") == [True]
    assert UNFILTERED_LISTS not in speicher.quellen[hamm.body]


async def test_muenster_401_mit_carry_modified_since(netz):
    """
    Münster: ``organization`` und ``agendaitem`` antworten auf den Filter mit HTTP 401 (33-mal je Nacht), Vorlagen
    filtert die Quelle, verliert den Filter aber in ``links.next`` (``carry_modified_since``). Nach dem ersten Lauf
    kommen die beiden Listen ohne Filter und ohne 401; die Vorlagen bleiben auf jeder Seite gefiltert.
    """
    muenster = Ris(MUENSTER)
    muenster.lehnt_ab = {"organization": 401, "agendaitem": 401}
    muenster.verliert_filter = True
    neu = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    for objekt in muenster.listen["paper"]:
        objekt["modified"] = neu
    netz(muenster)
    speicher = QuellenSpeicher()
    speicher.quellen[muenster.body] = {"carry_modified_since": True}
    orchestrator = _bereit(speicher, muenster)

    result = await _abgleich(orchestrator, muenster)
    assert result.success and result.errors == [], result.errors
    assert muenster.filter_je_anfrage("organization") == [True, False, False]  # 401, dann zwei Seiten ohne Filter
    assert muenster.filter_je_anfrage("paper") == [True, True]  # Folgeseite mit angehängtem Filter

    # Zweiter Lauf: keine Anfrage mit 401 mehr, die übrigen Listen weiter gefiltert
    result = await _abgleich(orchestrator, muenster)
    assert result.success and result.errors == [], result.errors
    assert muenster.filter_je_anfrage("organization") == [False, False]
    assert muenster.filter_je_anfrage("agendaitem") == [False, False]
    assert muenster.filter_je_anfrage("paper") == [True, True]
    for name in ("person", "meeting", "location"):
        assert muenster.filter_je_anfrage(name) == [True], name
    assert speicher.quellen[muenster.body].keys() == {"carry_modified_since", UNFILTERED_LISTS}
    assert _vermerkte_listen(speicher, muenster) == {muenster.liste("organization"), muenster.liste("agendaitem")}


# ---------------------------------------------------------------------------
# Vermerke aus sync_config (FilterNotes)
# ---------------------------------------------------------------------------


def test_vermerke_aus_sync_config():
    from src.client.filter_notes import UNFILTERED_LIST_RECHECK, FilterNotes

    jetzt = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    liste = "https://rim.example/oparl/body/1/location"
    notes = FilterNotes.from_sync_config(
        {
            UNFILTERED_LISTS: {liste: {"reason": "HTTP 400", "checked_at": "2026-10-16T11:00:00+00:00"}, "x": "kaputt"},
            UNFILTERED_HOSTS: "kein Wörterbuch",
        },
        "https://rim.example/oparl/body/1",
    )
    assert set(notes.lists) == {liste, "x"} and notes.hosts == {}
    assert not notes.dirty and not notes.legacy
    assert notes.skip_filter(liste, RIM, now=jetzt)
    assert not notes.skip_filter(liste, RIM, now=jetzt + UNFILTERED_LIST_RECHECK)  # Prüfung fällig
    assert not notes.skip_filter("x", RIM, now=jetzt)  # ungültiger Zeitpunkt: gleich prüfen
    assert not notes.skip_filter(f"{liste}-andere", RIM, now=jetzt)
    assert notes.unfiltered("x", RIM)

    # carry_modified_since hebt den Vermerk für den Server auf, nicht den für die Liste
    notes.note_host(RIM, "Filter fehlt in den Listen-Links", now=jetzt)
    assert notes.skip_filter(f"{liste}-andere", RIM, now=jetzt)
    assert not notes.skip_filter(f"{liste}-andere", RIM, carry=True, now=jetzt)
    assert notes.skip_filter(liste, RIM, carry=True, now=jetzt)
    assert FilterNotes.from_sync_config(None, "x").lists == {}


@pytest.mark.parametrize(
    ("config", "version", "bleibt"),
    [
        ({LEGACY: [RIM, MUENSTER]}, None, set()),  # eigener Server ohne Nachweis, fremder Server
        ({LEGACY: [RIM]}, "1.0", {RIM}),  # OParl 1.0, eigener Server
        ({LEGACY: [MUENSTER]}, "1.0", set()),  # OParl 1.0, aber fremder Server
        ({LEGACY: [RIM], "modified_since_check": {"host": RIM, "result": "ignored"}}, None, {RIM}),
        ({LEGACY: [RIM], "modified_since_check": {"host": RIM, "result": "unsupported"}}, None, set()),
        ({LEGACY: [RIM, ""], "modified_since_check": {"host": MUENSTER, "result": "ignored"}}, None, set()),
        ({LEGACY: "kaputt"}, "1.0", set()),
    ],
)
def test_uebernahme_alter_serververmerke(config, version, bleibt):
    from src.client.filter_notes import FilterNotes

    notes = FilterNotes.from_sync_config(config, f"https://{RIM}/oparl/system", version)
    assert notes.legacy and notes.dirty
    assert set(notes.hosts) == bleibt
    assert set(notes.changed_hosts) == bleibt
    assert notes.lists == {}

    # Nach dem Speichern (alter Schlüssel gelöscht, Server übernommen) gibt es nichts mehr zu übernehmen
    gespeichert = {key: value for key, value in config.items() if key != LEGACY}
    if bleibt:
        gespeichert[UNFILTERED_HOSTS] = {host: notes.hosts[host].to_entry() for host in bleibt}
    erneut = FilterNotes.from_sync_config(gespeichert, f"https://{RIM}/oparl/system", version)
    assert not erneut.dirty and set(erneut.hosts) == bleibt

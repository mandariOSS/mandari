# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wahlperioden aus ``legislativeTermList`` (Issue #1453).

Beobachtet am Bestand (17.11.2026): leere Listen, auch als JSON-Array ohne Hülle, HTTP 401 und HTTP 503. Die Liste
kommt leise (kein Befund am Host, kein Schutzschalter, keine Lücke), höchstens einmal am Tag, im Vollabgleich jedes Mal;
Löschmarken werden übernommen. Antworten aus ``httpx.MockTransport``, keine Abrufe.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from src.client.oparl_client import OParlClient
from src.config import settings
from src.storage.database import ForeignObjectError, legislative_term_lists_of
from src.sync.orchestrator import SyncOrchestrator
from src.sync.processor import OParlProcessor
from src.sync.wahlperioden import PRUEFABSTAND, WahlperiodenListen, lies_liste

HOST = "ris.example.org"
LISTE = f"https://{HOST}/oparl/body/1/legislativeterm"
QUELLE = f"https://{HOST}/oparl/system"


def _wahlperiode(nummer: int, **felder: Any) -> dict[str, Any]:
    return {
        "id": f"https://{HOST}/oparl/legislativeterm/{nummer}",
        "type": "https://schema.oparl.org/1.1/LegislativeTerm",
        "name": f"Wahlperiode {nummer}",
        "startDate": f"20{nummer:02d}-11-01",
        **felder,
    }


@pytest.fixture(autouse=True)
def _schutzschalter_an(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Wie im Betrieb: Schutzschalter und Serie von Serverfehlern zählen mit, die Liste soll beide nicht berühren."""
    monkeypatch.setattr(settings, "circuit_breaker_enabled", True)
    monkeypatch.setattr(settings, "circuit_breaker_failure_threshold", 1)
    monkeypatch.setattr(settings, "oparl_server_error_series_threshold", 1)
    yield


@asynccontextmanager
async def _client(antworten: dict[str, Any]) -> AsyncIterator[tuple[OParlClient, list[str]]]:
    """Antwort je Adresse: Statuscode oder JSON (Objekt oder Array)."""
    angefragt: list[str] = []

    def antwort(request: httpx.Request) -> httpx.Response:
        angefragt.append(str(request.url))
        wert = antworten.get(str(request.url), 404)
        return httpx.Response(wert) if isinstance(wert, int) else httpx.Response(200, json=wert)

    async with OParlClient(max_concurrent=2, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(antwort))
        yield client, angefragt


@pytest.mark.parametrize(
    ("antwort", "erwartet"),
    [
        ({"data": [_wahlperiode(20)], "links": {}}, "1 gelesen"),
        ([_wahlperiode(20), _wahlperiode(25)], "2 gelesen"),
        ({"data": [], "links": {}}, "leer"),
        ([], "leer"),
    ],
    ids=["liste", "array-ohne-huelle", "leere-liste", "leeres-array"],
)
async def test_liste_und_array_werden_gelesen(antwort: Any, erwartet: str) -> None:
    async with _client({LISTE: antwort}) as (client, _):
        gelesen = await lies_liste(client, LISTE)

    assert gelesen.ergebnis == erwartet
    assert all(eintrag["id"].startswith(f"https://{HOST}/") for eintrag in gelesen.eintraege)


@pytest.mark.parametrize(("status", "erwartet"), [(401, "HTTP 401"), (503, "HTTP 503"), (404, "HTTP 404, Liste fehlt")])
async def test_fehler_ohne_befund_am_host_und_ohne_schutzschalter(status: int, erwartet: str) -> None:
    async with _client({LISTE: status}) as (client, angefragt):
        for _ in range(3):
            gelesen = await lies_liste(client, LISTE)
        befunde = client.host_findings()
        offen = client._get_circuit_breaker(LISTE).is_open

    assert gelesen.eintraege == [] and gelesen.ergebnis == erwartet
    assert len(angefragt) == 3, "eine Anfrage je Prüfung, keine Wiederholung"
    assert befunde == []
    assert not offen


async def test_folgeseiten_und_abbruch_mittendrin() -> None:
    zweite = f"{LISTE}?page=2"
    dritte = f"{LISTE}?page=3"
    async with _client(
        {
            LISTE: {"data": [_wahlperiode(14)], "links": {"next": zweite}},
            zweite: {"data": [_wahlperiode(20)], "links": {"next": dritte}},
            dritte: 503,
        }
    ) as (client, _):
        gelesen = await lies_liste(client, LISTE)

    assert [eintrag["name"] for eintrag in gelesen.eintraege] == ["Wahlperiode 14", "Wahlperiode 20"]
    assert gelesen.ergebnis == "2 gelesen, dann HTTP 503"


async def test_liste_im_kreis_endet() -> None:
    async with _client({LISTE: {"data": [_wahlperiode(20)], "links": {"next": LISTE}}}) as (client, angefragt):
        gelesen = await lies_liste(client, LISTE)

    assert len(angefragt) == 1 and gelesen.ergebnis == "1 gelesen"


def test_hoechstens_einmal_am_tag_im_vollabgleich_immer() -> None:
    jetzt = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    listen = WahlperiodenListen(QUELLE, {LISTE: jetzt - timedelta(hours=3)})

    assert not listen.faellig(LISTE, full=False, now=jetzt)
    assert listen.faellig(LISTE, full=True, now=jetzt)
    assert listen.faellig(LISTE, full=False, now=jetzt + PRUEFABSTAND)
    assert listen.faellig(f"{LISTE}/andere", full=False, now=jetzt), "nie geprüft"
    # Ein Zeitpunkt in der Zukunft (Uhr verstellt) sperrt die Liste nicht
    assert WahlperiodenListen(QUELLE, {LISTE: jetzt + timedelta(hours=1)}).faellig(LISTE, full=False, now=jetzt)


def test_vermerk_wird_gelesen() -> None:
    gespeichert = {"legislative_term_lists": {LISTE: {"checked_at": "2026-11-20T10:00:00+00:00", "result": "leer"}}}

    assert legislative_term_lists_of(gespeichert) == {LISTE: datetime(2026, 11, 20, 10, 0, tzinfo=UTC)}
    assert legislative_term_lists_of({"legislative_term_lists": {LISTE: "kaputt"}}) == {LISTE: None}
    assert legislative_term_lists_of(None) == {}


class Speicher:
    """Was der Abgleich mit den Wahlperioden der Liste anfängt."""

    def __init__(self, fremd: frozenset[str] = frozenset()) -> None:
        self.fremd = fremd
        self.geschrieben: list[str] = []
        self.markiert: list[tuple[str, datetime | None]] = []
        self.vermerke: list[tuple[str, str, str]] = []

    async def upsert_legislative_term(self, term: Any, body_id: UUID) -> UUID:
        if term.external_id in self.fremd:
            raise ForeignObjectError("legislativeterm", term.external_id)
        self.geschrieben.append(term.external_id)
        return uuid4()

    async def mark_entity_deleted(
        self, entity_type: str, external_id: str, modified: datetime | None = None, *, body_id: UUID
    ) -> UUID | None:
        assert entity_type == "legislativeterm"
        self.markiert.append((external_id, modified))
        return uuid4()

    async def note_legislative_term_list(self, source_url: str, list_url: str, result: str) -> None:
        self.vermerke.append((source_url, list_url, result))


def _orchestrator(speicher: Speicher) -> SyncOrchestrator:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = speicher  # type: ignore[assignment]
    orchestrator.processor = OParlProcessor()
    return orchestrator


async def test_abgleich_uebernimmt_wahlperioden_und_loeschmarken() -> None:
    speicher = Speicher(fremd=frozenset({_wahlperiode(9)["id"]}))
    geloescht = _wahlperiode(14, deleted=True, modified="2026-11-01T08:00:00+01:00")
    stats: dict[str, Any] = {"errors": [], "incomplete": []}
    async with _client({LISTE: [_wahlperiode(20), geloescht, _wahlperiode(9)]}) as (client, _):
        await _orchestrator(speicher)._sync_legislative_term_list(
            client, WahlperiodenListen(QUELLE), LISTE, uuid4(), False, stats
        )

    assert speicher.geschrieben == [_wahlperiode(20)["id"]]
    assert speicher.markiert == [(geloescht["id"], datetime(2026, 11, 1, 7, 0, tzinfo=UTC))]
    assert speicher.vermerke == [(QUELLE, LISTE, "3 gelesen")]
    assert stats == {"errors": [], "incomplete": [], "legislative_terms": 2}


@pytest.mark.parametrize("status", [401, 503])
async def test_gestoerte_liste_ist_keine_luecke(status: int) -> None:
    speicher = Speicher()
    stats: dict[str, Any] = {"errors": [], "incomplete": []}
    async with _client({LISTE: status}) as (client, _):
        await _orchestrator(speicher)._sync_legislative_term_list(
            client, WahlperiodenListen(QUELLE), LISTE, uuid4(), False, stats
        )

    assert stats["incomplete"] == [] and stats["errors"] == []
    assert speicher.vermerke == [(QUELLE, LISTE, f"HTTP {status}")]


async def test_vor_kurzem_gepruefte_liste_wird_nicht_angefragt() -> None:
    speicher = Speicher()
    listen = WahlperiodenListen(QUELLE, {LISTE: datetime.now(UTC) - timedelta(hours=2)})
    async with _client({LISTE: [_wahlperiode(20)]}) as (client, angefragt):
        await _orchestrator(speicher)._sync_legislative_term_list(client, listen, LISTE, uuid4(), False, {})
        assert angefragt == []
        await _orchestrator(speicher)._sync_legislative_term_list(client, listen, LISTE, uuid4(), True, {})

    assert angefragt == [LISTE], "der Vollabgleich fragt trotzdem an"

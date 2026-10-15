# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weniger Abrufe je Lauf (Issue #1025): Änderungsfenster seit dem letzten vollständigen Abgleich und Listen, die es
bei der Quelle nicht gibt.

- Inkrementelle Läufe fragen je Kommune nach Änderungen seit dem Beginn ihres letzten vollständig erfolgreichen
  Abgleichs abzüglich Sicherheitsabstand (Standard 2 Stunden), nicht mehr nach allem aus sieben Tagen. Ein Lauf
  mit Lücke schreibt den Zeitpunkt nicht fort. Ohne Zeitpunkt bleibt es bei sieben Tagen.
- Antwortet eine Liste schon auf der ersten Seite mit HTTP 404 oder einem OParl-Fehlerobjekt, wird sie vermerkt
  und bis zum nächsten Vollabgleich nicht mehr angefragt. Das zählt als „nicht vorhanden“, nicht als Lücke.

Antworten aus ``httpx.MockTransport`` (aufgezeichnetes Verhalten der Quellen nachgebildet), keine Abrufe.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import UUID

import httpx
import pytest
from mandari_oparl.utils import parse_datetime

from src.client.oparl_client import ListFetchError, ListMissingError, OParlClient
from src.client.source_options import SourceFetchOptions
from src.config import settings
from src.storage.database import body_sync_started_of, missing_lists_of
from src.sync.orchestrator import MissingLists, SyncOrchestrator, modified_since_for
from src.sync.processor import OParlProcessor

HOST = "ris.example.org"
BASE = f"https://{HOST}/oparl"
BODY = f"{BASE}/body/1"
SOURCE = f"{BASE}/system"
LISTEN = ("organization", "person", "meeting", "paper")
FEHLEROBJEKT = {"type": "https://schema.oparl.org/1.1/Error", "message": "Requested class doesn't exist."}


# ---------------------------------------------------------------------------
# Nachgebildete Quelle und Speicher
# ---------------------------------------------------------------------------


class Quelle:
    """
    OParl-Schnittstelle mit Listen je Kommune; filtert mit ``modified_since`` (Objekte ab dem Zeitpunkt) und
    blättert in Seiten zu zwei Objekten. Je Pfad lässt sich eine Antwort fest einstellen.
    """

    def __init__(self, geaendert: datetime) -> None:
        self.anfragen: list[httpx.URL] = []
        self.listen: dict[str, list[dict[str, Any]]] = {}
        for name in LISTEN:
            typ = name.capitalize()
            self.listen[f"/oparl/body/1/{name}"] = [
                {
                    "id": f"{BASE}/{name}/{i}",
                    "type": f"https://schema.oparl.org/1.1/{typ}",
                    "name": f"{typ} {i}",
                    "modified": geaendert.isoformat(),
                }
                for i in range(3)
            ]
        #: Pfad -> feste Antwort (Statuscode oder JSON) für jede Anfrage
        self.fest: dict[str, Any] = {}
        #: Pfad -> Statuscode nur für Anfragen mit modified_since
        self.mit_filter: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request.url)
        pfad = request.url.path
        params = dict(request.url.params)
        if "modified_since" in params and pfad in self.mit_filter:
            return httpx.Response(self.mit_filter[pfad])
        if pfad in self.fest:
            antwort = self.fest[pfad]
            return httpx.Response(antwort) if isinstance(antwort, int) else httpx.Response(200, json=antwort)
        objekte = self.listen.get(pfad)
        if objekte is None:
            return httpx.Response(404)
        seit = params.get("modified_since")
        if seit:
            grenze = parse_datetime(seit)
            assert grenze is not None and grenze.tzinfo is not None
            objekte = [o for o in objekte if parse_datetime(o["modified"]) >= grenze]
        seite = int(params.get("page", "1"))
        teil = objekte[(seite - 1) * 2 : seite * 2]
        links: dict[str, str] = {}
        if seite * 2 < len(objekte):
            links["next"] = f"https://{HOST}{pfad}?{urlencode({**params, 'page': str(seite + 1)})}"
        return httpx.Response(200, json={"data": teil, "links": links})

    def anfragen_je_liste(self) -> Counter[str]:
        return Counter(url.path.rsplit("/", 1)[1] for url in self.anfragen)

    def filter_der_anfragen(self, liste: str) -> list[str | None]:
        return [
            (parse_qs(urlparse(str(url)).query).get("modified_since") or [None])[0]
            for url in self.anfragen
            if url.path.endswith(f"/{liste}")
        ]


class Speicher:
    """Was ``_sync_body`` vom Speicher braucht; hält Zeitpunkt und Vermerke wie ``sync_config``."""

    def __init__(self) -> None:
        self.begonnen: dict[str, datetime] = {}
        self.fehlend: set[str] = set()
        self.modified: dict[str, datetime | None] = {}
        self.vermerke: list[tuple[str, bool]] = []

    async def upsert_body(self, body: Any, source_id: UUID) -> UUID:
        return UUID(int=1)

    async def get_body_last_sync(self, body_id: UUID) -> datetime | None:
        return None

    async def update_body_sync_time(self, body_id: UUID) -> None:
        return None

    async def get_body_sync_started(self, source_id: UUID, body_external_id: str) -> datetime | None:
        return self.begonnen.get(body_external_id)

    async def set_body_sync_started(self, source_id: UUID, body_external_id: str, started_at: datetime) -> None:
        self.begonnen[body_external_id] = started_at

    async def set_list_missing(self, source_url: str, list_url: str, missing: bool) -> None:
        self.vermerke.append((list_url, missing))
        if missing:
            self.fehlend.add(list_url)
        else:
            self.fehlend.discard(list_url)

    async def batch_check_entities_exist(self, entity_type: str, external_ids: list[str]) -> dict[str, Any]:
        return {eid: self.modified.get(eid) for eid in external_ids}

    async def get_entity_content_hashes(self, entity_type: str, external_ids: list[str]) -> dict[str, str | None]:
        return {}

    async def get_person_ids_by_external_ids(self, ids: list[str]) -> dict[str, UUID]:
        return {}

    async def get_organization_ids_by_external_ids(self, ids: list[str]) -> dict[str, UUID]:
        return {}


def _orchestrator(speicher: Speicher) -> SyncOrchestrator:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = speicher  # type: ignore[assignment]
    orchestrator.processor = OParlProcessor()
    orchestrator.max_concurrent = 2
    orchestrator._parallel_mode = True

    async def speichern(entity: Any, body_id: UUID, entity_type: str, body_name: str | None = None) -> bool:
        speicher.modified[entity.external_id] = entity.oparl_modified
        return True

    orchestrator._store_entity = speichern  # type: ignore[method-assign]
    return orchestrator


def _kommune(**listen: str) -> dict[str, Any]:
    daten: dict[str, Any] = {"id": BODY, "type": "https://schema.oparl.org/1.1/Body", "name": "Musterstadt"}
    for name in LISTEN:
        daten[name] = f"{BODY}/{name}"
    daten.update(listen)
    return daten


@asynccontextmanager
async def _client(quelle: Quelle) -> AsyncIterator[OParlClient]:
    async with OParlClient(max_concurrent=2, wait_time=0) as client:
        assert client._client is not None
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(quelle))
        yield client


async def _lauf(
    quelle: Quelle,
    speicher: Speicher,
    *,
    full: bool = False,
    kommune: dict[str, Any] | None = None,
    options: SourceFetchOptions | None = None,
) -> dict[str, Any]:
    quelle.anfragen.clear()
    listen = MissingLists(SOURCE, set(speicher.fehlend))
    async with _client(quelle) as client:
        return await _orchestrator(speicher)._sync_body(
            client=client,
            body_data=kommune or _kommune(),
            source_id=UUID(int=9),
            full=full,
            options=options,
            missing_lists=listen,
        )


@pytest.fixture(autouse=True)
def _ohne_nebenlaeufe(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keine Texterkennung, kein Suchindex; Befund „filtert nicht“ des Hosts je Test leer."""
    monkeypatch.setattr(settings, "text_extraction_enabled", False)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", False)
    monkeypatch.setattr(settings, "oparl_max_retries", 1)
    vorher = OParlClient.get_modified_since_unsupported()
    OParlClient._modified_since_unsupported.clear()
    yield
    OParlClient._modified_since_unsupported.clear()
    OParlClient._modified_since_unsupported.update(vorher)


# ---------------------------------------------------------------------------
# Änderungsfenster
# ---------------------------------------------------------------------------


def test_fenster_ab_beginn_des_letzten_vollstaendigen_abgleichs():
    beginn = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    jetzt = beginn + timedelta(minutes=10)
    assert modified_since_for(beginn, timedelta(hours=2), now=jetzt) == datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
    # Gespeicherter Zeitpunkt ohne Zone gilt als UTC
    assert modified_since_for(beginn.replace(tzinfo=None), timedelta(0), now=jetzt) == beginn
    # Ein Zeitpunkt in der Zukunft (falsche Uhr) zählt als jetzt
    assert modified_since_for(jetzt + timedelta(days=1), timedelta(hours=2), now=jetzt) == jetzt - timedelta(hours=2)


def test_ohne_zeitpunkt_wie_bisher_sieben_tage_ab_mitternacht():
    jetzt = datetime(2026, 10, 15, 14, 30).astimezone()
    seit = modified_since_for(None, timedelta(hours=2), now=jetzt)
    assert seit.tzinfo is None  # naive Ortszeit wie bisher
    assert seit == datetime(2026, 10, 8, 0, 0)


@pytest.mark.parametrize(("wert", "minuten"), [(None, 120), (30, 30), (0, 0), (-5, 120), (True, 120), ("60", 120)])
def test_sicherheitsabstand_je_quelle(wert, minuten):
    config = {} if wert is None else {"modified_since_margin_minutes": wert}
    assert SourceFetchOptions.from_sync_config(config).modified_since_margin() == timedelta(minutes=minuten)


def test_standard_des_sicherheitsabstands_aus_den_einstellungen(monkeypatch):
    monkeypatch.setattr(settings, "modified_since_margin_minutes", 45)
    assert SourceFetchOptions().modified_since_margin() == timedelta(minutes=45)


def test_zeitpunkt_und_vermerke_aus_sync_config():
    config = {
        "body_sync_started": {BODY: "2026-10-15T10:00:00+00:00", "x": "kaputt"},
        "missing_lists": [f"{BODY}/membership", 7, ""],
    }
    assert body_sync_started_of(config, BODY) == datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    assert body_sync_started_of(config, "x") is None
    assert body_sync_started_of(config, "fehlt") is None
    assert body_sync_started_of(None, BODY) is None
    assert missing_lists_of(config) == {f"{BODY}/membership"}
    assert missing_lists_of({"missing_lists": "x"}) == set()


async def test_lauf_ohne_aenderungen_fragt_je_liste_genau_eine_seite_ab():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()

    # Erster Lauf ohne Zeitpunkt: sieben Tage, alle drei Objekte je Liste auf zwei Seiten
    vorher = datetime.now(UTC)
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == []
    assert quelle.anfragen_je_liste() == dict.fromkeys(LISTEN, 2)
    beginn = speicher.begonnen[BODY]
    assert vorher <= beginn <= datetime.now(UTC)

    # Zweiter Lauf: seit Beginn des ersten abzüglich zwei Stunden, nichts geändert, je Liste eine Seite
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == []
    assert quelle.anfragen_je_liste() == dict.fromkeys(LISTEN, 1)
    erwartet = (beginn - timedelta(hours=2)).isoformat(timespec="seconds")
    assert set(quelle.filter_der_anfragen("paper")) == {erwartet}
    # Der zweite Lauf ist vollständig und wird der neue Zeitpunkt
    assert speicher.begonnen[BODY] > beginn


async def test_sicherheitsabstand_der_quelle_gilt():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()
    beginn = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn
    options = SourceFetchOptions.from_sync_config({"modified_since_margin_minutes": 30})
    await _lauf(quelle, speicher, options=options)
    assert set(quelle.filter_der_anfragen("meeting")) == {(beginn - timedelta(minutes=30)).isoformat()}


async def test_lauf_mit_luecke_schreibt_den_zeitpunkt_nicht_fort():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    speicher = Speicher()
    beginn = datetime.now(UTC) - timedelta(hours=1)
    speicher.begonnen[BODY] = beginn

    quelle.fest["/oparl/body/1/paper"] = 500
    stats = await _lauf(quelle, speicher)
    assert stats["incomplete"] == ["paper"]
    assert speicher.begonnen[BODY] == beginn

    # Der nächste Lauf fragt weiter ab dem alten Zeitpunkt
    del quelle.fest["/oparl/body/1/paper"]
    await _lauf(quelle, speicher)
    assert set(quelle.filter_der_anfragen("paper")) == {(beginn - timedelta(hours=2)).isoformat(timespec="seconds")}
    assert speicher.begonnen[BODY] > beginn


async def test_unlesbare_mitgliedschaften_brechen_die_kommune_nicht_ab():
    """Bisher endete der Lauf an einer Mitgliedschaftsliste mit Fehler; Sitzungen und Vorlagen blieben stehen."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    quelle.fest["/oparl/body/1/membership"] = 500
    speicher = Speicher()
    stats = await _lauf(quelle, speicher, kommune=_kommune(membership=f"{BODY}/membership"))
    assert stats["incomplete"] == ["membership"]
    assert stats["meetings"] == 3 and stats["papers"] == 3
    assert BODY not in speicher.begonnen


async def test_vollabgleich_ist_ein_gueltiger_zeitpunkt():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    speicher = Speicher()
    vorher = datetime.now(UTC)
    stats = await _lauf(quelle, speicher, full=True)
    assert stats["incomplete"] == []
    assert speicher.begonnen[BODY] >= vorher
    assert all(f is None for f in quelle.filter_der_anfragen("paper"))


# ---------------------------------------------------------------------------
# Listen, die es bei der Quelle nicht gibt
# ---------------------------------------------------------------------------


async def test_fehlende_liste_bis_zum_vollabgleich_nicht_mehr_angefragt():
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=3))
    quelle.fest["/oparl/body/1/agendaItem"] = FEHLEROBJEKT  # gibt es nicht (Fehlerobjekt mit HTTP 200)
    kommune = _kommune(membership=f"{BODY}/membership", agendaItem=f"{BODY}/agendaItem")  # membership: 404
    speicher = Speicher()

    # Erster Lauf: beide Listen mit Filter, dann einmal ohne, dann vermerkt; kein Fehler, keine Lücke
    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert stats["incomplete"] == [] and stats["errors"] == []
    assert speicher.fehlend == {f"{BODY}/membership", f"{BODY}/agendaItem"}
    assert stats["missing_lists"] == sorted(speicher.fehlend)
    assert quelle.anfragen_je_liste()["membership"] == 2
    assert BODY in speicher.begonnen  # vollständig

    # Inkrementelle Läufe fragen sie nicht mehr an
    for _ in range(2):
        stats = await _lauf(quelle, speicher, kommune=kommune)
        zaehler = quelle.anfragen_je_liste()
        assert zaehler["membership"] == 0 and zaehler["agendaItem"] == 0
        assert stats["incomplete"] == []

    # Der Vollabgleich prüft wieder: Die Mitgliedschaften gibt es jetzt, die Tagesordnungspunkte weiter nicht
    quelle.listen["/oparl/body/1/membership"] = []
    stats = await _lauf(quelle, speicher, full=True, kommune=kommune)
    assert quelle.anfragen_je_liste()["membership"] == 1
    assert quelle.anfragen_je_liste()["agendaItem"] == 1
    assert speicher.fehlend == {f"{BODY}/agendaItem"}
    assert stats["incomplete"] == []

    # Danach fragt der inkrementelle Lauf die Mitgliedschaften wieder an
    await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.anfragen_je_liste()["membership"] == 1


async def test_404_nur_mit_filter_ist_keine_fehlende_liste():
    """Bezirksregierung Köln: membership?modified_since=… antwortet 404, ohne Filter kommt die Liste."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    quelle.listen["/oparl/body/1/membership"] = [
        {
            "id": f"{BASE}/membership/1",
            "type": "https://schema.oparl.org/1.1/Membership",
            "modified": "2025-01-01T10:00:00+01:00",
        }
    ]
    quelle.mit_filter["/oparl/body/1/membership"] = 404
    kommune = _kommune(membership=f"{BODY}/membership")
    speicher = Speicher()

    stats = await _lauf(quelle, speicher, kommune=kommune)
    assert stats["incomplete"] == [] and stats["memberships"] == 1
    assert speicher.fehlend == set()
    assert quelle.filter_der_anfragen("membership")[0] is not None
    assert quelle.filter_der_anfragen("membership")[1:] == [None]

    # Im selben Prozess ohne die gefilterte Anfrage, die anderen Listen weiter mit Filter
    await _lauf(quelle, speicher, kommune=kommune)
    assert quelle.filter_der_anfragen("membership") == [None]
    assert all(f is not None for f in quelle.filter_der_anfragen("paper"))


async def test_alle_listen_404_ist_eine_stoerung_und_kein_vermerk():
    quelle = Quelle(geaendert=datetime.now(UTC))
    for name in LISTEN:
        quelle.fest[f"/oparl/body/1/{name}"] = 404
    speicher = Speicher()
    stats = await _lauf(quelle, speicher)
    assert speicher.fehlend == set()
    assert "lists" in stats["incomplete"]
    assert BODY not in speicher.begonnen


async def test_client_meldet_fehlende_liste_nur_auf_wunsch():
    quelle = Quelle(geaendert=datetime.now(UTC))
    quelle.fest["/oparl/body/1/agendaItem"] = FEHLEROBJEKT
    async with _client(quelle) as client:
        with pytest.raises(ListMissingError) as fehlt:
            async for _ in client.fetch_list(f"{BODY}/membership", report_missing=True):
                pass
        assert fehlt.value.reason == "HTTP 404"
        with pytest.raises(ListMissingError):
            async for _ in client.fetch_list(f"{BODY}/agendaItem", report_missing=True):
                pass
        # Ohne report_missing wie bisher: 404 ist ein Listenfehler, ein Fehlerobjekt eine leere Liste
        with pytest.raises(ListFetchError):
            async for _ in client.fetch_list(f"{BODY}/membership"):
                pass
        assert [page async for page in client.fetch_list(f"{BODY}/agendaItem")] == []
        # Kein Fehlvermerk am Host
        assert client.host_health[HOST].failed_lists == [f"{BODY}/membership"]


async def test_404_loest_den_schutzschalter_nicht_aus(monkeypatch):
    monkeypatch.setattr(settings, "circuit_breaker_enabled", True)
    quelle = Quelle(geaendert=datetime.now(UTC))
    async with _client(quelle) as client:
        for i in range(settings.circuit_breaker_failure_threshold + 2):
            ergebnis = await client.fetch(f"{BASE}/gibt-es-nicht/{i}", use_cache=False)
            assert ergebnis.status_code == 404
        ergebnis = await client.fetch(f"{BODY}/paper", use_cache=False)
        assert ergebnis.status_code == 200 and ergebnis.error is None

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ALLRIS-3-Adapter von Ende zu Ende (Issue #121): Läufer, Adapter und OParlProcessor gegen die Testseiten, ohne Netz.

Ein Fake-Fetcher liefert zu jeder Adresse die passende Testseite (``fixtures/scrapers/allris3``): den Kalender
Oktober 2023, je Sitzung mit Tagesordnung die Seite einer anderen Instanz, Tagesordnungspunkte, Drucksachen und
Gremien aus derselben Instanz wie die Sitzung. Die Rohbytes gibt er wie ``PoliteFetcher`` als ``last_content`` mit,
den Text so, wie ein HTTP-Client ihn nach der Deklaration (ISO-8859-1) liest. Die robots-Tests und der Test der
Anmeldeseite laufen durch den echten ``PoliteFetcher`` (MockTransport).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid5

import httpx
import pytest

from src.client.oparl_client import ERROR_KIND_ROBOTS_BLOCKED
from src.config import settings
from src.scrapers import get_adapter, politeness
from src.scrapers import runner as runner_module
from src.scrapers.allris3 import Allris3Adapter, decode_page, parse_meeting, parse_paper_list
from src.scrapers.base import CrawlWindow, ScraperConfig
from src.scrapers.politeness import PoliteFetcher
from src.scrapers.runner import ScraperSyncRunner
from src.sync.orchestrator import SyncOrchestrator
from src.sync.processor import OParlProcessor
from tests.test_sessionnet_mehrere_koerperschaften import FakeOrchestrator, FakeStorage

FIXTURES = Path(__file__).parent / "fixtures" / "scrapers" / "allris3"
BASE_URL = "https://ris.example.org/bi/"
BODY_ID = "https://ris.example.org/bi/"
BODY_UUID = uuid5(UUID(int=0), BODY_ID)
OKTOBER = CrawlWindow(start=date(2023, 10, 1), end=date(2023, 10, 31))

#: Sitzungen des Kalenders mit Tagesordnung -> Testseite (je eine andere Instanz)
TO010 = {
    1002655: "altona/to010.html",
    1002680: "bergedorf/to010.html",
    1002713: "eimsbuettel/to010.html",
    1002646: "hamburg_mitte/to010.html",
    1002626: "hamburg_nord/to010_pa.html",
}
CALENDAR_MEETINGS = 14
NEWEST_PAPER = 1016851

SITZUNG = f"{BASE_URL}to010.asp?SILFDNR=1002655"
#: Sitzung im Fenster, die der Kalender nicht mehr nennt
SITZUNG_ALT = f"{BASE_URL}to010.asp?SILFDNR=999"
#: Gremium im Bestand ohne Sitzung im Fenster: ALLRIS 3 hat keine Gremienliste, also kein Löschabgleich
GREMIUM_ALT = f"{BASE_URL}au020.asp?AULFDNR=999"


def _query(url: str) -> dict[str, str]:
    return {key.upper(): values[0] for key, values in parse_qs(urlparse(url).query).items()}


class AllrisFetcher:
    """PoliteFetcher-Ersatz: Testseite je Adresse; ``overrides`` ersetzt Antworten (Status, Bytes) je Adresse."""

    source_name = "fixture"

    def __init__(self, overrides: Callable[[str], tuple[int, bytes] | None] | None = None) -> None:
        self.requests: list[str] = []
        self.overrides = overrides
        self.last_status: int | None = None
        self.last_content: bytes | None = None
        self.last_url: str | None = None
        self.instance_of_top: dict[int, str] = {}
        self.instance_of_paper: dict[int, str] = {}
        self.instance_of_committee: dict[int, str] = {}
        for path in TO010.values():
            instance = path.split("/")[0]
            meeting = parse_meeting(decode_page((FIXTURES / path).read_bytes()))
            if meeting.committee is not None and meeting.committee[0] == "au":
                self.instance_of_committee[meeting.committee[1]] = instance
            for row in meeting.rows:
                if row.tolfdnr is not None:
                    self.instance_of_top[row.tolfdnr] = instance
                if row.volfdnr is not None:
                    self.instance_of_paper[row.volfdnr] = instance
        listed = parse_paper_list(decode_page((FIXTURES / "hamburg_nord/vo040.html").read_bytes()))
        for row in listed:
            self.instance_of_paper.setdefault(row["volfdnr"], "hamburg_nord")

    def route(self, url: str) -> str | None:
        name = urlparse(url).path.rsplit("/", 1)[-1].removesuffix(".asp")
        query = _query(url)
        if name == "si010_e":
            return "hamburg_nord/si010_e.html" if (query.get("MM"), query.get("YY")) == ("10", "2023") else None
        if name == "to010":
            return TO010.get(int(query["SILFDNR"]))
        if name == "to020":
            return f"{self.instance_of_top[int(query['TOLFDNR'])]}/to020.html"
        if name == "vo020":
            instance = self.instance_of_paper.get(int(query["VOLFDNR"]))
            # Lücken der Nummern (rückwärts gezählt): ALLRIS gibt nichts heraus
            return f"{instance}/vo020.html" if instance else "synthetisch/vo020_nicht_oeffentlich.html"
        if name == "au020":
            return f"{self.instance_of_committee[int(query['AULFDNR'])]}/au020.html"
        return {
            "vo040": "hamburg_nord/vo040.html",
            "fr010": "hamburg_nord/fr010.html",
            "fr020": "hamburg_nord/fr020.html",
            "pa020": "synthetisch/pa020.html",
        }.get(name)

    async def fetch_text(self, url: str) -> str | None:
        self.requests.append(url)
        self.last_url = url
        answer = self.overrides(url) if self.overrides is not None else None
        if answer is None:
            path = self.route(url)
            answer = (200, (FIXTURES / path).read_bytes()) if path else (404, b"")
        self.last_status, data = answer
        if self.last_status != 200:
            self.last_content = None
            return None
        self.last_content = data
        return data.decode("latin-1")

    async def is_allowed(self, url: str) -> bool:
        return True

    def pages(self, name: str) -> list[str]:
        return [url for url in self.requests if f"/{name}.asp" in url]


class Quelle:
    id = UUID(int=2)
    url = BASE_URL
    name = "ALLRIS 3 (Fixture)"
    user_agent = None

    def __init__(self, scraper_state: dict[str, Any] | None = None, **scraper: Any) -> None:
        self.sync_config = {
            "source_type": "scraper:allris3",
            "scraper": {"base_url": BASE_URL, "body_name": "Musterstadt", "rate_limit_seconds": 0, **scraper},
            "scraper_state": scraper_state or {},
        }


class Orchestrator(FakeOrchestrator):
    """Hält Fehler der Quelle fest, statt den Test abzubrechen."""

    def __init__(self) -> None:
        super().__init__()
        self.failures: list[tuple[str, str | None]] = []

    async def _record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        self.failures.append((error, error_kind))


def _fenster(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fenster(CrawlWindow):
        @classmethod
        def from_days(cls, days_back: int, days_ahead: int, today: Any = None) -> CrawlWindow:
            return OKTOBER

    monkeypatch.setattr(runner_module, "CrawlWindow", Fenster)
    monkeypatch.setattr(settings, "text_extraction_enabled", False)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", False)
    monkeypatch.setattr(settings, "scraper_tombstone_full_crawls", 1)
    monkeypatch.setattr(settings, "scraper_tombstone_max_missing", 10)


@pytest.fixture
def abruf(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Läufer mit dem Fake-Fetcher; ``abruf["overrides"]`` ersetzt einzelne Antworten, ``abruf["fetcher"]`` danach."""
    zustand: dict[str, Any] = {"overrides": None, "fetcher": None}

    class FakePolite:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> AllrisFetcher:
            zustand["fetcher"] = AllrisFetcher(zustand["overrides"])
            return zustand["fetcher"]

        async def __aexit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr(runner_module, "PoliteFetcher", FakePolite)
    _fenster(monkeypatch)
    return zustand


def _mit_bestand(orchestrator: Orchestrator) -> Orchestrator:
    orchestrator.storage.active[("meeting", BODY_UUID)] = {SITZUNG, SITZUNG_ALT}
    orchestrator.storage.active[("organization", BODY_UUID)] = {GREMIUM_ALT}
    return orchestrator


# ---------------------------------------------------------------------------
# Läufer
# ---------------------------------------------------------------------------


async def test_vollabgleich_durch_den_laeufer(abruf: dict[str, Any]) -> None:
    orchestrator = _mit_bestand(Orchestrator())

    result = await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]

    assert result.success, result.errors
    assert result.bodies_synced == 1
    assert set(orchestrator.storage.bodies) == {BODY_ID}
    stored: dict[str, set[str]] = {}
    for entity_type, external_id, body_id in orchestrator.stored:
        assert body_id == BODY_UUID
        stored.setdefault(entity_type, set()).add(external_id)
    assert set(stored) == {"organization", "person", "membership", "paper", "meeting", "consultation"}
    assert len(stored["meeting"]) == CALENDAR_MEETINGS
    # Fraktionen (fr010), Ausschüsse der Sitzungen (au020) und das Hauptorgan (pa020)
    assert f"{BASE_URL}fr020.asp?FRLFDNR=1120" in stored["organization"]
    assert f"{BASE_URL}au020.asp?AULFDNR=235" in stored["organization"]
    assert f"{BASE_URL}pa020.asp?PALFDNR=211" in stored["organization"]
    assert f"{BASE_URL}kp020.asp?KPLFDNR=9001" in stored["person"]
    assert f"{BASE_URL}vo020.asp?VOLFDNR={NEWEST_PAPER}" in stored["paper"]

    # Löschabgleich nur für Sitzungen: die verschwundene Sitzung ja, das Gremium ohne Sitzung im Fenster nicht
    assert orchestrator.marked == [SITZUNG_ALT]
    state = orchestrator.storage.state
    assert state["last_run"]["complete"] is True, state["last_run"]
    assert state["last_run"]["incomplete"] == {}
    assert state["last_run"]["parse_quota"] == 1.0
    assert set(state["list_snapshots"]) == {"2023-10", "vo040"}
    assert state["list_snapshots"]["vo040"] == str(NEWEST_PAPER)
    assert orchestrator.storage.source_syncs == [{"full": True, "complete": True}]
    assert orchestrator.failures == []
    # Personen nicht einzeln abgerufen, Dokumente (do027) nie
    assert not abruf["fetcher"].pages("kp020") and not abruf["fetcher"].pages("do027")


async def test_inkrementeller_lauf_markiert_nie(abruf: dict[str, Any]) -> None:
    orchestrator = _mit_bestand(Orchestrator())

    await ScraperSyncRunner(orchestrator, Quelle()).run(full=False)  # type: ignore[arg-type]

    assert orchestrator.marked == []
    # Mitglieder nur im Vollabgleich (members_on_full_only)
    assert not abruf["fetcher"].pages("fr020") and not abruf["fetcher"].pages("au020")
    assert orchestrator.storage.source_syncs == [{"full": False, "complete": True}]


async def test_gestoerte_sitzungsseite_ist_nicht_geloescht(abruf: dict[str, Any]) -> None:
    """Der Kalender nennt die Sitzung, ihre Seite antwortet mit 500: Es gibt sie, sie fehlt nicht."""
    abruf["overrides"] = lambda url: (500, b"") if url.endswith("to010.asp?SILFDNR=1002655") else None
    orchestrator = _mit_bestand(Orchestrator())

    await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]

    assert SITZUNG not in orchestrator.marked
    assert SITZUNG_ALT in orchestrator.marked  # Kalender vollständig gelesen
    last_run = orchestrator.storage.state["last_run"]
    assert last_run["complete"] is False
    assert last_run["incomplete"]["paper"] == "Sitzung 1002655 nicht lesbar"
    assert orchestrator.storage.source_syncs == [{"full": True, "complete": False}]
    # Der Monat gilt nicht als gelesen: Der nächste Änderungsabgleich liest ihn wieder
    assert "2023-10" not in orchestrator.storage.state["list_snapshots"]


async def test_gestoerter_tagesordnungspunkt_haelt_die_sitzung_zurueck(abruf: dict[str, Any]) -> None:
    """Ohne to020 fehlten gespeicherte Protokolle: Die Sitzung wird in diesem Lauf nicht geschrieben."""
    abruf["overrides"] = lambda url: (503, b"") if url.endswith("to020.asp?TOLFDNR=1052638") else None
    orchestrator = _mit_bestand(Orchestrator())

    await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]

    meetings = {external_id for entity_type, external_id, _ in orchestrator.stored if entity_type == "meeting"}
    assert SITZUNG not in meetings
    assert len(meetings) == CALENDAR_MEETINGS - 1
    assert SITZUNG not in orchestrator.marked
    assert orchestrator.storage.state["last_run"]["complete"] is False


async def test_kalender_nicht_lesbar_markiert_keine_sitzung(abruf: dict[str, Any]) -> None:
    sperrseite = b"<html><head><title>Just a moment...</title></head><body>Pruefung</body></html>"
    abruf["overrides"] = lambda url: (200, sperrseite) if "/si010_e.asp" in url else None
    orchestrator = _mit_bestand(Orchestrator())

    await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]

    assert orchestrator.marked == []
    last_run = orchestrator.storage.state["last_run"]
    assert last_run["incomplete"]["meeting"] == "Kalender 10/2023 nicht lesbar"
    assert last_run["complete"] is False


async def test_tagesordnungspunkte_im_loeschabgleich(abruf: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein Punkt, den die gelesene Tagesordnung nicht mehr nennt, wird markiert (Bremse je Sitzung)."""
    abgesetzt = f"{BASE_URL}to020.asp?TOLFDNR=1"

    async def aktive_punkte(self: FakeStorage, meeting_external_ids: list[str]) -> dict[str, set[str]]:
        assert SITZUNG in meeting_external_ids
        punkte = {f"{BASE_URL}to020.asp?TOLFDNR={n}" for n in (1052631, 1052632, 1052638)}
        return {SITZUNG: punkte | {abgesetzt}}

    monkeypatch.setattr(FakeStorage, "get_active_agenda_item_ids", aktive_punkte)
    orchestrator = _mit_bestand(Orchestrator())

    await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]

    assert abgesetzt in orchestrator.marked


async def test_zweiter_lauf_liest_unveraenderte_listen_nicht(abruf: dict[str, Any]) -> None:
    """Listen-Diffing: Kalendermonat unverändert, keine neuere Drucksache: keine Detailseiten."""
    orchestrator = Orchestrator()
    await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]
    state = orchestrator.storage.state

    await ScraperSyncRunner(Orchestrator(), Quelle(scraper_state=state)).run(full=False)  # type: ignore[arg-type]

    fetcher = abruf["fetcher"]
    assert fetcher.pages("si010_e") and fetcher.pages("vo040")
    assert not fetcher.pages("to010") and not fetcher.pages("to020") and not fetcher.pages("vo020")


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def _adapter(fetcher: AllrisFetcher | None = None, **config: Any) -> Allris3Adapter:
    scraper = ScraperConfig.from_sync_config({"scraper": {"base_url": BASE_URL, "body_name": "Musterstadt", **config}})
    return Allris3Adapter(scraper, fetcher or AllrisFetcher())  # type: ignore[arg-type]


async def test_reihenfolge_und_processor() -> None:
    """Jedes Objekt kommt nach denen, auf die es verweist, und passiert den unveränderten OParlProcessor."""
    adapter = _adapter()
    processor = OParlProcessor()
    body = processor.process_body(adapter.build_body(), BODY_ID)
    assert body.name == "Musterstadt"

    seen: dict[str, set[str]] = {}
    agenda_items: set[str] = set()
    async for entity_type, page in adapter.iter_entities(OKTOBER, full=True):
        for entity in page:
            assert entity["id"].startswith("https://ris.example.org/bi/"), entity["id"]
            assert entity["mandari:contentHash"].startswith("sha256:")
            assert processor.process(entity, BODY_ID) is not None, (entity_type, entity["id"])
            if entity_type == "membership":
                assert entity["person"] in seen["person"]
                assert entity["organization"] in seen["organization"]
            if entity_type == "meeting":
                assert set(entity.get("organization", [])) <= seen["organization"]
                agenda_items.update(item["id"] for item in entity.get("agendaItem", []))
            if entity_type == "consultation":
                assert entity["meeting"] in seen["meeting"]
                assert entity["paper"] in seen["paper"]
                assert entity["agendaItem"] in agenda_items
            seen.setdefault(entity_type, set()).add(entity["id"])

    meeting = next(iter(seen["meeting"]))
    assert meeting.startswith(f"{BASE_URL}to010.asp?SILFDNR=")
    assert adapter.stats.parse_quota == 1.0
    assert adapter.stats.complete


async def test_sitzung_mit_tagesordnung_und_protokoll() -> None:
    adapter = _adapter()
    meetings: dict[str, dict[str, Any]] = {}
    papers: dict[str, dict[str, Any]] = {}
    async for entity_type, page in adapter.iter_entities(OKTOBER, full=False):
        target = meetings if entity_type == "meeting" else papers if entity_type == "paper" else None
        if target is not None:
            target.update({entity["id"]: entity for entity in page})

    meeting = meetings[SITZUNG]
    assert meeting["name"] == "Sitzung des Mobilitätsausschusses"
    assert meeting["start"] == "2025-11-03T18:00:00+01:00"
    assert meeting["end"] == "2025-11-03T19:45:00+01:00"
    assert meeting["organization"] == [f"{BASE_URL}au020.asp?AULFDNR=235"]
    assert meeting["location"]["room"] == "Sitzungsraum des Technischen Rathauses (EG)"
    assert meeting["location"]["streetAddress"].startswith("Jessenstraße 1, 22767")
    items = {item["number"]: item for item in meeting["agendaItem"]}
    assert items["2.1"]["id"] == f"{BASE_URL}to020.asp?TOLFDNR=1052638"
    assert items["2.1"]["result"] == "zur Kenntnis genommen"
    assert items["2.1"]["consultation"] == f"{BASE_URL}vo020.asp?VOLFDNR=1017291#consultation/1052638"
    # Protokoll und Beschluss aus to020 (vergangene Sitzung)
    assert items["2.1"]["mandari:minutes"].startswith("Vertagt aus der Sitzung vom 17.02.2025.")
    assert items["2.1"]["resolutionText"] == "Der Ausschuss nimmt die Drucksache zur Kenntnis."
    assert "result" not in items["1"]  # „(offen)“
    # Sitzung nur aus dem Kalender: ohne Tagesordnung, ohne Abruf von to010
    nur_kalender = meetings[f"{BASE_URL}to010.asp?SILFDNR=1002824"]
    assert nur_kalender["start"] == "2023-10-04T18:00:00+02:00"
    assert "agendaItem" not in nur_kalender

    paper = papers[f"{BASE_URL}vo020.asp?VOLFDNR=1017291"]
    assert paper["reference"] == "22-2362"
    assert paper["paperType"] == "Beschlussempfehlung öffentlich"
    assert paper["mandari:proposal"] == "Der Hauptauschuss wird um Zustimmung gebeten."
    assert paper["mandari:explanation"].startswith("Der Mobilitätsausschuss empfiehlt")
    assert "auxiliaryFile" not in paper and "mainFile" not in paper


async def test_kein_protokoll_fuer_kuenftige_sitzungen() -> None:
    fetcher = AllrisFetcher()
    adapter = Allris3Adapter(ScraperConfig(base_url=BASE_URL), fetcher, today=date(2020, 1, 1))  # type: ignore[arg-type]
    async for _ in adapter.iter_entities(OKTOBER, full=False):
        pass
    assert fetcher.pages("to010")
    assert not fetcher.pages("to020")


async def test_rueckwaerts_zaehlen_bis_zur_untergrenze() -> None:
    fetcher = AllrisFetcher()
    adapter = _adapter(fetcher, oldest_paper_id=NEWEST_PAPER - 11)
    papers: set[str] = set()
    async for entity_type, page in adapter.iter_entities(OKTOBER, full=True):
        if entity_type == "paper":
            papers.update(entity["id"] for entity in page)

    requested = {int(_query(url)["VOLFDNR"]) for url in fetcher.pages("vo020")}
    assert set(range(NEWEST_PAPER - 11, NEWEST_PAPER + 1)) <= requested
    # Unter der Untergrenze nur Drucksachen der Tagesordnungen und der Liste
    assert all(n >= NEWEST_PAPER - 11 or n in fetcher.instance_of_paper for n in requested)
    # Lücken (nicht öffentlich) zählen wie ein toter Verweis, nicht als Störung
    assert adapter.stats.complete
    assert f"{BASE_URL}vo020.asp?VOLFDNR={NEWEST_PAPER - 1}" not in papers
    assert adapter.list_snapshots["vo040"] == str(NEWEST_PAPER)


async def test_ohne_untergrenze_kein_rueckwaertszaehlen() -> None:
    fetcher = AllrisFetcher()
    adapter = _adapter(fetcher)
    async for _ in adapter.iter_entities(OKTOBER, full=True):
        pass
    requested = {int(_query(url)["VOLFDNR"]) for url in fetcher.pages("vo020")}
    assert requested <= set(fetcher.instance_of_paper)


async def test_detailseiten_budget() -> None:
    fetcher = AllrisFetcher()
    adapter = _adapter(fetcher, max_detail_pages=4, oldest_paper_id=1)
    async for _ in adapter.iter_entities(OKTOBER, full=True):
        pass
    assert adapter.stats.detail_pages_attempted <= 4 + 1
    assert adapter.stats.incomplete.get("meeting") == "Detailseiten-Budget erreicht"
    assert "paper" in adapter.stats.incomplete
    # Die neueste Kennung rückt nur vor, wenn alle Drucksachen gelesen sind
    assert "vo040" not in adapter.list_snapshots


def test_ungueltige_untergrenze_wird_ignoriert() -> None:
    assert _adapter(oldest_paper_id="abc").oldest_paper_id is None
    assert _adapter(oldest_paper_id=0).oldest_paper_id is None
    assert _adapter(oldest_paper_id="1000").oldest_paper_id == 1000


def test_auswahl_des_adapters() -> None:
    adapter = get_adapter("scraper:allris3", ScraperConfig(base_url=BASE_URL), fetcher=None)
    assert adapter.vendor == "allris3"
    assert adapter.tombstone_entity_types == ("meeting",)  # type: ignore[attr-defined]


def test_sync_source_nimmt_den_scraper() -> None:
    """``sync --source`` und der Daemon geben eine ALLRIS-3-Quelle an den Scraper, nicht an den OParl-Client."""
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = FakeStorage()
    orchestrator.processor = OParlProcessor()
    quelle = Quelle()
    runner = orchestrator._scraper_runner(quelle)
    assert isinstance(runner, ScraperSyncRunner)
    assert runner.source_type == "scraper:allris3"


# ---------------------------------------------------------------------------
# Echter PoliteFetcher: robots.txt und Anmeldeseite
# ---------------------------------------------------------------------------


def _echter_abruf(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    class Abruf(PoliteFetcher):
        async def __aenter__(self) -> Abruf:
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            return self

    async def ohne_pause(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runner_module, "PoliteFetcher", Abruf)
    monkeypatch.setattr(politeness, "asyncio", SimpleNamespace(sleep=ohne_pause, Lock=asyncio.Lock))
    _fenster(monkeypatch)


def _seiten(anfragen: list[str], robots: str) -> Callable[[httpx.Request], httpx.Response]:
    fetcher = AllrisFetcher()

    def handler(request: httpx.Request) -> httpx.Response:
        anfragen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        path = fetcher.route(str(request.url))
        if path is None:
            return httpx.Response(404)
        return httpx.Response(200, content=(FIXTURES / path).read_bytes(), headers={"Content-Type": "text/html"})

    return handler


async def test_robots_sperre_kein_abruf(monkeypatch: pytest.MonkeyPatch, echte_robots: None) -> None:
    """Sperrt die robots.txt die Basis-URL, geht außer der robots.txt keine Anfrage hinaus."""
    anfragen: list[str] = []
    _echter_abruf(monkeypatch, _seiten(anfragen, "User-agent: *\nDisallow: /bi/\n"))
    orchestrator = _mit_bestand(Orchestrator())

    result = await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]

    assert anfragen == ["/robots.txt"]
    assert result.success is False
    assert result.error_kind == ERROR_KIND_ROBOTS_BLOCKED
    assert orchestrator.storage.state["robots_disallowed"] is True
    assert orchestrator.stored == [] and orchestrator.marked == []
    assert orchestrator.failures and orchestrator.failures[0][1] == ERROR_KIND_ROBOTS_BLOCKED


async def test_robots_sperre_fuer_unterseiten(monkeypatch: pytest.MonkeyPatch, echte_robots: None) -> None:
    """Sperrt die robots.txt nur Drucksachen, fragt der Lauf keine an, bricht ab und markiert nichts."""
    anfragen: list[str] = []
    _echter_abruf(monkeypatch, _seiten(anfragen, "User-agent: mandari-ingestor\nDisallow: /bi/vo020.asp\n"))
    orchestrator = _mit_bestand(Orchestrator())

    result = await ScraperSyncRunner(orchestrator, Quelle()).run(full=True)  # type: ignore[arg-type]

    assert "/bi/si010_e.asp" in anfragen
    assert "/bi/vo020.asp" not in anfragen
    assert result.error_kind == ERROR_KIND_ROBOTS_BLOCKED
    assert orchestrator.marked == []
    assert orchestrator.storage.source_syncs == []


async def test_weiterleitung_zur_anmeldung_ist_nicht_oeffentlich() -> None:
    """vo020 leitet auf noauth.asp um: nicht öffentlich, wie ein toter Verweis, keine Störung."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.path.endswith("/vo020.asp"):
            return httpx.Response(302, headers={"Location": "noauth.asp?x=1"})
        return httpx.Response(200, content=b"<html><body>Bitte melden Sie sich an.</body></html>")

    fetcher = PoliteFetcher(rate_limit_seconds=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        adapter = Allris3Adapter(ScraperConfig(base_url=BASE_URL), fetcher)
        paper = await adapter._build_paper(4711, BODY_ID)
    finally:
        await fetcher._client.aclose()

    assert paper is None
    assert fetcher.last_url == "https://ris.example.org/bi/noauth.asp?x=1"
    assert fetcher.last_content == b"<html><body>Bitte melden Sie sich an.</body></html>"
    assert adapter.last_not_public
    assert adapter.stats.gone_pages == adapter.stats.failed_pages == 1
    assert adapter.stats.parse_failures == 0
    assert adapter.stats.complete


async def test_gesperrte_anmeldeseite_ist_nicht_oeffentlich(echte_robots: None) -> None:
    """Sperrt die robots.txt die Anmeldeseite, folgt der Abruf der Weiterleitung nicht; die Drucksache ist nicht
    öffentlich, der Lauf geht weiter (keine Sperre der Quelle)."""
    anfragen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        anfragen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /bi/noauth.asp\n")
        return httpx.Response(302, headers={"Location": "noauth.asp"})

    fetcher = PoliteFetcher(rate_limit_seconds=0)
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        adapter = Allris3Adapter(ScraperConfig(base_url=BASE_URL), fetcher)
        paper = await adapter._build_paper(4711, BODY_ID)
    finally:
        await fetcher._client.aclose()

    assert paper is None
    assert anfragen == ["/robots.txt", "/bi/vo020.asp"]
    assert adapter.last_not_public
    assert adapter.stats.gone_pages == adapter.stats.failed_pages == 1
    assert adapter.stats.complete

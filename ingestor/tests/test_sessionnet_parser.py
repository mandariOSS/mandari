"""
Golden-File-Regressionstests für den SessionNet-Adapter.

HTML-Fixtures stammen von zwei realen, öffentlichen Instanzen
(abgerufen 2026-07-20, siehe fixtures/scrapers/sessionnet/README.md):
- luedenscheid: buergerinfo.luedenscheid.de (klassische *.asp-Variante)
- eschweiler:   rat.eschweiler.de/bi (*.php-Variante, gleiches Markup)

Bricht ein Parser-Refactor Felder, schlagen diese Tests fehl (CI-Gate).
Zusätzlich: End-to-End-Test des Adapters gegen einen Fixture-Fetcher —
die synthetischen OParl-Dicts müssen den unveränderten OParlProcessor
passieren.
"""

import dataclasses
import json
from datetime import date
from pathlib import Path

import pytest

from src.scrapers.base import CrawlWindow, ScraperConfig
from src.scrapers.sessionnet import (
    SessionNetAdapter,
    parse_calendar,
    parse_meeting_agenda,
    parse_meeting_info,
    parse_members,
    parse_organizations,
    parse_paper,
)
from src.sync.processor import OParlProcessor

FIXTURES = Path(__file__).parent / "fixtures" / "scrapers" / "sessionnet"
INSTANCES = ["luedenscheid", "eschweiler"]
VARIANTS = {"luedenscheid": "asp", "eschweiler": "php"}
BASE_URLS = {
    "luedenscheid": "https://buergerinfo.luedenscheid.de/",
    "eschweiler": "https://rat.eschweiler.de/bi/",
}


def read_fixture(instance: str, name: str) -> str:
    return (FIXTURES / instance / name).read_text(encoding="utf-8", errors="replace")


def read_expected(instance: str, name: str):
    path = FIXTURES / instance / "expected" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("instance", INSTANCES)
class TestGoldenFiles:
    """Parser-Ausgabe muss exakt den eingefrorenen Erwartungen entsprechen."""

    def test_calendar(self, instance):
        parsed = parse_calendar(read_fixture(instance, "si0040.html"))
        stubs = [dataclasses.asdict(s) for s in parsed]
        assert stubs == read_expected(instance, "calendar")
        assert len(stubs) >= 4  # Fixture-Monat hat Sitzungen

    def test_meeting_info(self, instance):
        info = parse_meeting_info(read_fixture(instance, "si0050.html"))
        assert info == read_expected(instance, "meeting_info")
        # Pflichtfelder der Sitzungs-Info
        assert info["committee"]
        assert info["date"]

    def test_agenda(self, instance):
        agenda = parse_meeting_agenda(read_fixture(instance, "si0057.html"))
        agenda_json = {
            "title": agenda.title,
            "rows": [dataclasses.asdict(r) for r in agenda.rows],
        }
        assert agenda_json == read_expected(instance, "agenda")
        assert len(agenda.rows) >= 10
        # Mindestens ein TOP verweist auf eine Vorlage
        assert any(r.paper_kvonr for r in agenda.rows)

    def test_paper(self, instance):
        paper = parse_paper(read_fixture(instance, "vo0050.html"))
        assert paper == read_expected(instance, "paper")
        assert paper["name"]
        assert paper["reference"]
        assert paper["files"]

    def test_organizations(self, instance):
        orgs = parse_organizations(read_fixture(instance, "gr0040.html"))
        assert orgs == read_expected(instance, "organizations")
        assert len(orgs) >= 20

    def test_members(self, instance):
        members = parse_members(read_fixture(instance, "kp0040.html"))
        assert members == read_expected(instance, "members")
        assert any(m["kpenr"] for m in members)
        # Beratende Mitglieder haben kein Stimmrecht
        for m in members:
            section = (m["section"] or "").lower()
            if "beratend" in section or "ohne stimmrecht" in section:
                assert m["voting_right"] is False


class FixtureFetcher:
    """PoliteFetcher-Ersatz, der Fixture-HTML statt Netzwerk liefert."""

    source_name = "fixture"

    def __init__(self, instance: str) -> None:
        self.instance = instance
        self.requests: list[str] = []

    async def fetch_text(self, url: str) -> str | None:
        self.requests.append(url)
        for page in ("si0040", "si0050", "si0057", "vo0050", "gr0040", "kp0040"):
            suffix = page + "." + VARIANTS[self.instance]
            if f"/{page}." in url or url.split("?")[0].endswith(suffix):
                return read_fixture(self.instance, f"{page}.html")
        return None

    async def is_allowed(self, url: str) -> bool:
        return True


@pytest.mark.parametrize("instance", INSTANCES)
async def test_adapter_end_to_end_processor(instance):
    """
    Adapter-Crawl über Fixtures: alle erzeugten OParl-Dicts müssen den
    unveränderten OParlProcessor passieren (Kern-Pipeline-Kompatibilität).
    """
    config = ScraperConfig(
        base_url=BASE_URLS[instance],
        body_name=instance.capitalize(),
        variant=VARIANTS[instance],
        max_detail_pages=200,
    )
    fetcher = FixtureFetcher(instance)
    adapter = SessionNetAdapter(config, fetcher)  # type: ignore[arg-type]
    window = CrawlWindow(start=date(2026, 7, 1), end=date(2026, 7, 1))

    collected: dict[str, list[dict]] = {}
    async for entity_type, page in adapter.iter_entities(window, full=True):
        collected.setdefault(entity_type, []).extend(page)

    assert set(collected) >= {"organization", "person", "membership", "paper", "meeting"}
    assert collected.get("consultation"), "TOPs mit Vorlagen müssen Consultations erzeugen"

    processor = OParlProcessor()
    body_id = adapter.urls.body_id()

    # Body selbst
    body = processor.process_body(adapter.build_body(), body_id)
    assert body.name

    for entity_type, entities in collected.items():
        for entity in entities:
            assert entity["id"].startswith("https://"), entity
            assert "mandari:contentHash" in entity, (entity_type, entity.get("id"))
            processed = processor.process(entity, body_id)
            assert processed is not None, (entity_type, entity.get("id"))

    # Sitzungen: Start-Zeitpunkt mit Zeitzonen-Offset, TOPs vorhanden
    meeting = collected["meeting"][0]
    assert meeting.get("start"), "Sitzung ohne Start-Zeitpunkt"
    assert "+" in meeting["start"], "Start muss Zeitzonen-Offset tragen"
    assert meeting.get("agendaItem"), "Sitzung ohne Tagesordnung"

    # Parse-Quote des Fixture-Laufs muss hoch sein
    assert adapter.stats.parse_quota >= 0.9
    assert adapter.stats.detail_pages_parsed > 0

    # external_ids sind kanonisch (nur ID-Parameter, https, sortiert)
    paper_ids = [p["id"] for p in collected["paper"]]
    for pid in paper_ids:
        assert "__kvonr=" in pid
        assert "#" not in pid


async def test_adapter_skips_unchanged_month():
    """Listen-Diffing: unveränderter Kalendermonat -> keine Detail-Fetches."""
    instance = "luedenscheid"
    config = ScraperConfig(base_url=BASE_URLS[instance], variant=VARIANTS[instance])
    window = CrawlWindow(start=date(2026, 7, 1), end=date(2026, 7, 1))

    # Erster Lauf: Snapshots einsammeln
    adapter1 = SessionNetAdapter(config, FixtureFetcher(instance))  # type: ignore[arg-type]
    async for _ in adapter1.iter_entities(window, full=True):
        pass
    assert adapter1.list_snapshots

    # Zweiter Lauf (inkrementell) mit bekannten Snapshots: Monat wird
    # übersprungen -> keine si0057-Detailseiten
    fetcher2 = FixtureFetcher(instance)
    adapter2 = SessionNetAdapter(config, fetcher2)  # type: ignore[arg-type]
    adapter2.previous_snapshots = dict(adapter1.list_snapshots)
    async for _ in adapter2.iter_entities(window, full=False):
        pass
    assert not any("si0057" in url for url in fetcher2.requests)

"""
SessionNet-Instanzen mit mehreren Körperschaften (Mandantenparameter __cpanr)
und Wahlperioden (__cwpnr).

Fixtures: pseudonymisierte Seiten einer Samtgemeinde-Instanz mit drei
Mitgliedskörperschaften und einer kommunalen Gesellschaft
(fixtures/scrapers/sessionnet/samtgemeinde/, siehe README). Kein Netzwerk.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid5

import pytest

from src.scrapers import runner as runner_module
from src.scrapers.base import BODIES_AUTO, BodySpec, CrawlWindow, ScraperConfig
from src.scrapers.runner import ScraperSyncRunner
from src.scrapers.sessionnet import (
    SessionNetAdapter,
    parse_calendar_mandanten,
    parse_legislative_terms,
    parse_mandanten,
)
from src.sync.processor import OParlProcessor

FIXTURES = Path(__file__).parent / "fixtures" / "scrapers" / "sessionnet" / "samtgemeinde"
BASE_URL = "https://ratsinfo.musterheide.example/bi/"
SEPTEMBER = CrawlWindow(start=date(2026, 9, 1), end=date(2026, 9, 30))


def body_url(cpanr: int) -> str:
    return f"{BASE_URL}?__cpanr={cpanr}"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def expected(name: str) -> Any:
    return json.loads((FIXTURES / "expected" / f"{name}.json").read_text(encoding="utf-8"))


class SamtgemeindeFetcher:
    """PoliteFetcher-Ersatz: liefert die Fixture zur URL (Seite + Kennungsparameter)."""

    source_name = "fixture"

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.requests: list[str] = []
        self.overrides = overrides or {}

    async def fetch_text(self, url: str) -> str | None:
        self.requests.append(url)
        parsed = urlparse(url)
        page = parsed.path.rsplit("/", 1)[-1].split(".")[0]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if page == "gr0040":
            name = f"gr0040_cpanr{query['__cpanr']}.html" if "__cpanr" in query else "gr0040.html"
        elif page == "si0040":
            if "__cpanr" not in query:
                return None
            name = f"si0040_cpanr{query['__cpanr']}_{int(query['__cjahr']):04d}-{int(query['__cmonat']):02d}.html"
        elif page == "kp0040":
            name = f"kp0040_{query['__kgrnr']}.html"
        elif page in ("si0050", "si0057"):
            name = f"{page}_{query['__ksinr']}.html"
        elif page == "vo0050":
            name = f"vo0050_{query['__kvonr']}.html"
        else:
            return None
        name = self.overrides.get(name, name)
        path = FIXTURES / name
        return path.read_text(encoding="utf-8") if path.exists() else None

    async def is_allowed(self, url: str) -> bool:
        return True


def make_adapter(bodies: Any, fetcher: SamtgemeindeFetcher | None = None) -> SessionNetAdapter:
    config = ScraperConfig(
        base_url=BASE_URL,
        body_name="Samtgemeinde Musterheide",
        variant="asp",
        bodies=bodies,
    )
    return SessionNetAdapter(config, fetcher or SamtgemeindeFetcher())  # type: ignore[arg-type]


async def crawl(adapter: SessionNetAdapter) -> dict[str, dict[str, dict[str, Any]]]:
    """Body-Kennung -> entity_type -> external_id -> Dict."""
    collected: dict[str, dict[str, dict[str, Any]]] = {}
    async for body_id, entity_type, page in adapter.iter_body_entities(SEPTEMBER, full=True):
        bucket = collected.setdefault(body_id, {}).setdefault(entity_type, {})
        for item in page:
            assert item["id"] not in bucket, f"doppelte Kennung {item['id']}"
            bucket[item["id"]] = item
    return collected


# ---------------------------------------------------------------------------
# Parser (Golden Files)
# ---------------------------------------------------------------------------


class TestParser:
    def test_mandantenauswahl(self):
        assert parse_mandanten(read("gr0040.html")) == expected("mandanten")

    def test_mandantenauswahl_eines_anderen_mandanten_nennt_den_vorausgewaehlten(self):
        entries = parse_mandanten(read("gr0040_cpanr2.html"))
        assert entries[0] == {"cpanr": None, "name": "Gemeinde Musterdorf", "selected": True}
        assert {"cpanr": 1, "name": "Samtgemeinde Musterheide", "selected": False} in entries

    def test_alle_mandanten_ist_keine_koerperschaft(self):
        html = (
            '<ul><li class="nav-item dropdown"><a aria-label="Mandant auswählen" class="nav-link dropdown-toggle" '
            'href="#">Alle Mandanten <span class="caret"></span></a><div class="dropdown-menu">'
            '<a href="gr0040.php?__cpanr=0" class="dropdown-item smcfiltermenumandant">Alle Mandanten</a>'
            '<a href="gr0040.php?__cpanr=1" class="dropdown-item smcfiltermenumandant">Samtgemeinde A</a>'
            '<a href="gr0040.php?__cpanr=2" class="dropdown-item smcfiltermenumandant">Gemeinde B</a>'
            "</div></li></ul>"
        )
        assert parse_mandanten(html) == [
            {"cpanr": 1, "name": "Samtgemeinde A", "selected": False},
            {"cpanr": 2, "name": "Gemeinde B", "selected": False},
        ]

    def test_instanz_ohne_mandantenauswahl(self):
        lued = Path(__file__).parent / "fixtures" / "scrapers" / "sessionnet" / "luedenscheid" / "gr0040.html"
        assert parse_mandanten(lued.read_text(encoding="utf-8", errors="replace")) == []

    def test_wahlperioden(self):
        terms = parse_legislative_terms(read("gr0040_cpanr1.html"))
        assert terms == expected("legislative_terms")
        current = next(t for t in terms if t["selected"])
        assert current["start_date"] == "2021-11-01"
        assert current["end_date"] == "2026-10-31"

    def test_mandant_je_kalendereintrag(self):
        assert parse_calendar_mandanten(read("si0040_cpanr2_2026-09.html")) == {
            2001: "Gemeinde Musterdorf",
            2101: "Gemeinde Musterdorf",
        }


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


class TestKonfiguration:
    def test_ohne_bodies_bleibt_eine_koerperschaft(self):
        config = ScraperConfig.from_sync_config({"scraper": {"base_url": BASE_URL}})
        assert config.bodies is None

    def test_auto(self):
        config = ScraperConfig.from_sync_config({"scraper": {"base_url": BASE_URL, "bodies": "auto"}})
        assert config.bodies == BODIES_AUTO
        assert "bodies" not in config.extra

    def test_liste(self):
        config = ScraperConfig.from_sync_config(
            {"scraper": {"base_url": BASE_URL, "bodies": [{"cpanr": 2, "name": " Gemeinde X "}, {"cpanr": "3"}]}}
        )
        assert config.bodies == [BodySpec(cpanr=2, name="Gemeinde X"), BodySpec(cpanr=3)]

    @pytest.mark.parametrize(
        "bodies",
        [
            [],
            "alle",
            [{"name": "ohne Nummer"}],
            [{"cpanr": 0}],
            [{"cpanr": "x"}],
            [{"cpanr": 2}, {"cpanr": 2}],
            ["2"],
        ],
    )
    def test_ungueltig(self, bodies):
        with pytest.raises(ValueError):
            ScraperConfig.from_sync_config({"scraper": {"base_url": BASE_URL, "bodies": bodies}})


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


async def test_feste_liste_ordnet_alles_der_richtigen_koerperschaft_zu():
    fetcher = SamtgemeindeFetcher()
    adapter = make_adapter(
        [BodySpec(cpanr=1), BodySpec(cpanr=2), BodySpec(cpanr=3, short_name="Beispielfeld")], fetcher
    )

    bodies = await adapter.resolve_bodies()
    assert [b["id"] for b in bodies] == [body_url(1), body_url(2), body_url(3)]
    assert [b["name"] for b in bodies] == [
        "Samtgemeinde Musterheide",
        "Gemeinde Musterdorf",
        "Gemeinde Beispielfeld",
    ]
    assert bodies[2]["shortName"] == "Beispielfeld"
    # Wahlperioden je Körperschaft (Kennung mit Mandant)
    terms = bodies[1]["legislativeTerm"]
    assert [t["id"] for t in terms] == [
        "https://ratsinfo.musterheide.example/bi/gr0040.asp?__cpanr=2&__cwpnr=2",
        "https://ratsinfo.musterheide.example/bi/gr0040.asp?__cpanr=2&__cwpnr=1",
    ]
    assert terms[1]["startDate"] == "2021-11-01"
    assert terms[1]["endDate"] == "2026-10-31"
    assert all(t["body"] == body_url(2) for t in terms)

    collected = await crawl(adapter)
    assert set(collected) == {body_url(1), body_url(2), body_url(3)}

    def ids(body: int, entity_type: str) -> set[str]:
        return set(collected[body_url(body)].get(entity_type, {}))

    org = "https://ratsinfo.musterheide.example/bi/kp0040.asp?__kgrnr={}".format
    meeting = "https://ratsinfo.musterheide.example/bi/si0057.asp?__ksinr={}".format
    paper = "https://ratsinfo.musterheide.example/bi/vo0050.asp?__kvonr={}".format

    # Gremien, Sitzungen und Vorlagen je Körperschaft
    assert ids(1, "organization") == {org(1), org(12)}
    assert ids(2, "organization") == {org(2), org(21)}
    assert ids(3, "organization") == {org(3), org(31)}
    assert ids(1, "meeting") == {meeting(1001), meeting(1002), meeting(1003)}
    assert ids(2, "meeting") == {meeting(2001), meeting(2101)}
    assert ids(3, "meeting") == {meeting(3101)}
    assert ids(1, "paper") == {paper(5001), paper(5002)}
    assert ids(2, "paper") == {paper(6001)}
    assert ids(3, "paper") == {paper(7001)}
    for body in (1, 2, 3):
        for entity_type in ("organization", "meeting", "paper", "person"):
            assert all(item["body"] == body_url(body) for item in collected[body_url(body)][entity_type].values())

    # Gleichnamige Gremien zweier Gemeinden werden nicht vermischt
    va_musterdorf = collected[body_url(2)]["meeting"][meeting(2101)]
    va_beispielfeld = collected[body_url(3)]["meeting"][meeting(3101)]
    assert va_musterdorf["organization"] == [org(21)]
    assert va_beispielfeld["organization"] == [org(31)]
    # Gremium ohne öffentliche Mitgliederliste: Sitzung ohne Gremienverweis
    assert collected[body_url(1)]["meeting"][meeting(1003)]["organization"] == []

    # Personen je Körperschaft: Wer in zwei Räten sitzt, steht in beiden Bodies
    person_1 = "https://ratsinfo.musterheide.example/bi/pe0051.asp?__cpanr=1&__kpenr=102"
    person_2 = "https://ratsinfo.musterheide.example/bi/pe0051.asp?__cpanr=2&__kpenr=102"
    assert person_1 in ids(1, "person")
    assert person_2 in ids(2, "person")
    memberships_2 = collected[body_url(2)]["membership"].values()
    assert {(m["person"], m["organization"]) for m in memberships_2 if m["person"] == person_2} == {(person_2, org(2))}
    assert f"{body_url(1)}#person/lena-ohnelink" in ids(1, "person")

    # Ort je Körperschaft
    loc = collected[body_url(2)]["meeting"][meeting(2001)]["location"]["id"]
    assert loc.startswith(f"{body_url(2)}#location/")

    # Vorlage, die zweimal beraten wird, nur einmal geladen
    assert sum("vo0050.asp?__kvonr=5001" in url for url in fetcher.requests) == 1
    # Listen tragen den Mandantenparameter, Detailseiten nicht; Mandant 4 nicht angefragt
    for url in fetcher.requests:
        if "/gr0040." in url or "/si0040." in url:
            assert "__cpanr=" in url, url
        else:
            assert "__cpanr=" not in url, url
        assert "__cpanr=4" not in url
    # Gremienliste je Mandant nur einmal (resolve_bodies und Crawl teilen sie)
    assert sum("/gr0040." in url for url in fetcher.requests) == 3

    # Kalender-Snapshots je Mandant getrennt
    assert set(adapter.list_snapshots) == {"p1:2026-09", "p2:2026-09", "p3:2026-09"}

    # Alles passiert den unveränderten Processor
    processor = OParlProcessor()
    for body in bodies:
        assert len(processor.process_body(body, body["id"]).nested_entities) == 2
    for body_id, by_type in collected.items():
        for items in by_type.values():
            for item in items.values():
                assert processor.process(item, body_id) is not None


async def test_auto_ermittelt_alle_mandanten():
    fetcher = SamtgemeindeFetcher()
    adapter = make_adapter(BODIES_AUTO, fetcher)
    bodies = await adapter.resolve_bodies()
    assert [b["id"] for b in bodies] == [body_url(n) for n in (1, 2, 3, 4)]
    assert bodies[0]["name"] == "Samtgemeinde Musterheide"
    assert bodies[3]["name"] == "Musterheide Touristik GmbH"
    # Standardseite + Seite eines anderen Mandanten (nennt die Nummer des
    # vorausgewählten) + übrige Mandanten; keine Seite doppelt
    gremien = [url for url in fetcher.requests if "/gr0040." in url]
    assert gremien[0] == f"{BASE_URL}gr0040.asp"
    assert len(gremien) == len(set(gremien)) == 5

    collected = await crawl(adapter)
    assert set(collected[body_url(4)]["organization"]) == {
        "https://ratsinfo.musterheide.example/bi/kp0040.asp?__kgrnr=4"
    }
    assert "meeting" not in collected[body_url(4)]


async def test_kalender_eines_anderen_mandanten_wird_nicht_zugeordnet():
    """Beachtet die Instanz __cpanr nicht, landen fremde Sitzungen nicht im falschen Body."""
    fetcher = SamtgemeindeFetcher(overrides={"si0040_cpanr2_2026-09.html": "si0040_cpanr1_2026-09.html"})
    adapter = make_adapter([BodySpec(cpanr=2)], fetcher)
    collected = await crawl(adapter)
    assert "meeting" not in collected.get(body_url(2), {})
    assert not any("si0057" in url for url in fetcher.requests)


async def test_nichtoeffentliche_tops_mit_eigener_zaehlung_ueberschreiben_nichts():
    adapter = make_adapter([BodySpec(cpanr=1)])
    collected = await crawl(adapter)
    meeting = collected[body_url(1)]["meeting"]["https://ratsinfo.musterheide.example/bi/si0057.asp?__ksinr=1001"]
    items = meeting["agendaItem"]
    assert [i["number"] for i in items] == ["1", "2", "1"]
    assert len({i["id"] for i in items}) == 3
    assert items[2]["id"].endswith("#agendaitem/N1")
    assert items[2]["public"] is False


async def test_ohne_bodies_bleiben_kennungen_unveraendert():
    """Bestehende Quellen: ein Body je Basis-URL, Kennungen ohne __cpanr."""
    lued = Path(__file__).parent / "fixtures" / "scrapers" / "sessionnet" / "luedenscheid"

    class LuedFetcher:
        source_name = "fixture"

        async def fetch_text(self, url: str) -> str | None:
            for page in ("si0040", "si0050", "si0057", "vo0050", "gr0040", "kp0040"):
                if f"/{page}." in url:
                    return (lued / f"{page}.html").read_text(encoding="utf-8", errors="replace")
            return None

    config = ScraperConfig(base_url="https://buergerinfo.luedenscheid.de/", body_name="Lüdenscheid", variant="asp")
    adapter = SessionNetAdapter(config, LuedFetcher())  # type: ignore[arg-type]
    bodies = await adapter.resolve_bodies()
    assert len(bodies) == 1
    body = bodies[0]
    assert body["id"] == "https://buergerinfo.luedenscheid.de/"
    assert body["website"] == "https://buergerinfo.luedenscheid.de/"
    assert body["shortName"] == "Lüdenscheid"
    assert body["legislativeTerm"][0]["id"] == "https://buergerinfo.luedenscheid.de/gr0040.asp?__cwpnr=8"
    assert adapter.build_body() == body

    window = CrawlWindow(start=date(2026, 7, 1), end=date(2026, 7, 1))
    seen_bodies = set()
    async for body_id, entity_type, page in adapter.iter_body_entities(window, full=True):
        seen_bodies.add(body_id)
        for item in page:
            assert "__cpanr" not in item["id"], (entity_type, item["id"])
    assert seen_bodies == {"https://buergerinfo.luedenscheid.de/"}
    assert set(adapter.list_snapshots) == {"2026-07"}


# ---------------------------------------------------------------------------
# Runner: je Körperschaft ein Body in der Datenbank
# ---------------------------------------------------------------------------


class FakeStorage:
    def __init__(self) -> None:
        self.bodies: dict[str, UUID] = {}
        self.body_terms: dict[str, list[str]] = {}
        self.synced_bodies: list[UUID] = []
        self.state: dict[str, Any] = {}
        self.active: dict[tuple[str, UUID], set[str]] = {}

    async def upsert_body(self, processed, source_id) -> UUID:
        body_id = uuid5(UUID(int=0), processed.external_id)
        self.bodies[processed.external_id] = body_id
        self.body_terms[processed.external_id] = [t.external_id for t in processed.nested_entities]
        return body_id

    async def get_entity_content_hashes(self, entity_type, external_ids) -> dict[str, str]:
        return {}

    async def update_scraper_state(self, url, state) -> None:
        self.state = state

    async def update_body_sync_time(self, body_id) -> None:
        self.synced_bodies.append(body_id)

    async def update_source_sync_time(self, source_id, full_sync=False) -> None:
        return None

    async def get_active_meeting_ids_in_window(self, body_id, start, end) -> set[str]:
        return set(self.active.get(("meeting", body_id), set()))

    async def get_active_external_ids_for_body(self, entity_type, body_id) -> set[str]:
        return set(self.active.get((entity_type, body_id), set()))


class FakeOrchestrator:
    def __init__(self) -> None:
        self.storage = FakeStorage()
        self.processor = OParlProcessor()
        self.stored: list[tuple[str, str, UUID]] = []
        self.marked: list[str] = []
        self.indexed: list[tuple[UUID, int]] = []

    async def _store_entity(self, processed, body_id, entity_type, source_name) -> bool:
        self.stored.append((entity_type, processed.external_id, body_id))
        return True

    async def _record_source_failure(self, *args, **kwargs) -> None:
        raise AssertionError(f"unerwarteter Quellenfehler: {args}")

    async def _mark_deleted(self, entity, entity_type, es_deletions) -> bool:
        self.marked.append(entity["id"])
        es_deletions.setdefault(entity_type, []).append(entity["id"])
        return True

    async def _index_body_elasticsearch(self, body_id, stats, es_deletions, full) -> None:
        self.indexed.append((body_id, sum(len(v) for v in es_deletions.values())))


class FakeSource:
    id = UUID(int=1)
    url = BASE_URL
    name = "Samtgemeinde (Fixture)"
    user_agent = None

    def __init__(self, scraper_state: dict[str, Any] | None = None) -> None:
        self.sync_config = {
            "source_type": "scraper:sessionnet",
            "scraper": {
                "base_url": BASE_URL,
                "variant": "asp",
                "bodies": [{"cpanr": 1}, {"cpanr": 2}, {"cpanr": 3}],
                "full_window_days": [-30, 0],
            },
            "scraper_state": scraper_state or {},
        }


@pytest.fixture
def fixture_fetcher(monkeypatch):
    fetcher = SamtgemeindeFetcher()

    class FakePolite:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return fetcher

        async def __aexit__(self, *args) -> None:
            return None

    class FixedWindow(CrawlWindow):
        @classmethod
        def from_days(cls, days_back, days_ahead, today=None):
            return SEPTEMBER

    monkeypatch.setattr(runner_module, "PoliteFetcher", FakePolite)
    monkeypatch.setattr(runner_module, "CrawlWindow", FixedWindow)
    monkeypatch.setattr(runner_module.settings, "text_extraction_enabled", False)
    monkeypatch.setattr(runner_module.settings, "elasticsearch_indexing_enabled", True)
    monkeypatch.setattr(runner_module.settings, "scraper_tombstone_full_crawls", 1)
    return fetcher


async def test_runner_legt_je_koerperschaft_einen_body_an(fixture_fetcher):
    orchestrator = FakeOrchestrator()
    storage = orchestrator.storage
    stale_org = "https://ratsinfo.musterheide.example/bi/kp0040.asp?__kgrnr=99"
    storage.active[("organization", uuid5(UUID(int=0), body_url(3)))] = {stale_org}
    # Gremium von Mandant 1 ist aktiv und wird gesehen: kein Tombstone, obwohl
    # es nicht zu den Kandidaten von Mandant 3 gehört
    storage.active[("organization", uuid5(UUID(int=0), body_url(1)))] = {
        "https://ratsinfo.musterheide.example/bi/kp0040.asp?__kgrnr=1"
    }

    runner = ScraperSyncRunner(orchestrator, FakeSource())  # type: ignore[arg-type]
    result = await runner.run(full=True)

    assert result.success, result.errors
    assert result.bodies_synced == 3
    assert set(storage.bodies) == {body_url(1), body_url(2), body_url(3)}
    assert len(storage.body_terms[body_url(2)]) == 2

    body_of = {external_id: body_id for _, external_id, body_id in orchestrator.stored}
    uuid = storage.bodies
    assert body_of["https://ratsinfo.musterheide.example/bi/kp0040.asp?__kgrnr=31"] == uuid[body_url(3)]
    assert body_of["https://ratsinfo.musterheide.example/bi/si0057.asp?__ksinr=2101"] == uuid[body_url(2)]
    assert body_of["https://ratsinfo.musterheide.example/bi/vo0050.asp?__kvonr=5001"] == uuid[body_url(1)]

    # Verschwundenes Gremium nach (hier) einem Full-Crawl tombstoned, sonst nichts
    assert orchestrator.marked == [stale_org]
    # Indexierung und Sync-Zeit je Body; Index-Löschungen nur einmal
    assert [body_id for body_id, _ in orchestrator.indexed] == [uuid[body_url(n)] for n in (1, 2, 3)]
    assert [deleted for _, deleted in orchestrator.indexed] == [1, 0, 0]
    assert storage.synced_bodies == [uuid[body_url(n)] for n in (1, 2, 3)]
    assert set(storage.state["list_snapshots"]) == {"p1:2026-09", "p2:2026-09", "p3:2026-09"}

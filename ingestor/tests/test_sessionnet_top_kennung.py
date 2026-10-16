"""
Kennungen der Tagesordnungspunkte beim Abruf aus SessionNet-Sitzungsseiten (Issue #863).

Ein Punkt mit Vorlage heißt nach ihr (``__kvonr``), nicht nach seiner Nummer: Fügt die Verwaltung davor einen Punkt
ein, behält er seine Kennung. Geprüft mit zwei Abrufständen derselben Sitzungsseite: dem eingefrorenen Golden File
und demselben Stand mit einem neuen ersten Punkt und verschobenen Nummern. Dazu die Umstellung des Bestands (Zeilen
mit der bisherigen Kennung werden umgeschrieben, ``id`` bleibt), der Löschabgleich der Tagesordnung und der Rückweg.
Kein Netzwerk, keine Datenbank (gegen PostgreSQL: ``test_sessionnet_top_kennung_db.py``).
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mandari_oparl.ids import canonical_id
from rich.console import Console
from typer.testing import CliRunner

from src import main as cli
from src.scrapers import runner as runner_module
from src.scrapers.base import CrawlWindow, ScraperConfig
from src.scrapers.runner import ScraperSyncRunner
from src.scrapers.sessionnet import (
    AgendaItemKey,
    AgendaRow,
    MeetingStub,
    SessionNetAdapter,
    agenda_item_keys,
    is_legacy_agenda_item_id,
    legacy_agenda_item_keys,
    parse_meeting_agenda,
    plan_agenda_item_renames,
    plan_agenda_item_revert,
)
from tests.test_sessionnet_mehrere_koerperschaften import (
    BASE_URL,
    SEPTEMBER,
    FakeOrchestrator,
    FakeSource,
    FakeStorage,
    SamtgemeindeFetcher,
)

FIXTURES = Path(__file__).parent / "fixtures" / "scrapers" / "sessionnet"
#: Instanz -> (Basis-URL, Variante, Sitzung der Fixture si0057.html)
INSTANZEN = {
    "luedenscheid": ("https://buergerinfo.luedenscheid.de/", "asp", 2379),
    "eschweiler": ("https://rat.eschweiler.de/bi/", "php", 4849),
}

_ERSTE_ZEILE = '<tr class="smc-t-r-l"><td class="tofnum">'
_OEFFENTLICHE_NUMMER = re.compile(r'(<span class="badge">&Ouml; )(\d+)')
_NEUER_PUNKT = (
    '<tr class="smc-t-r-l"><td class="tofnum"><span class="badge">&Ouml; 1</span></td><td class="tobetr">'
    '<div class="margin-bottom smc-card-header-title-simple">Einwohnerfragestunde</div></td>'
    '<td class="toxx">&nbsp;</td><td class="smc-t-cl991 sidocs"></td></tr>\n'
)
#: Samtgemeinde, Sitzung 1001
SITZUNG_1001 = f"{BASE_URL}si0057.asp?__ksinr=1001"
_OHNE_VORLAGE_1001 = 'Beschlussfähigkeit</div></td><td class="toxx">&nbsp;</td>'
_MIT_VORLAGE_1001 = (
    'Beschlussfähigkeit</div></td><td data-label="Vorlage" class="smc-t-cl991 tovo"> '
    '<a href="vo0050.asp?__kvonr=5002" class="smce-a-u smc-link-procedure smc_datatype_vo">SG/BV/042/2026</a></td>'
)


def lesen(instanz: str, name: str) -> str:
    return (FIXTURES / instanz / name).read_text(encoding="utf-8", errors="replace")


def mit_neuem_ersten_punkt(html: str) -> str:
    """Zweiter Abrufstand: ein neuer Punkt „Ö 1“ vor allen anderen, deren Nummern rücken um eins auf."""
    umnummeriert = _OEFFENTLICHE_NUMMER.sub(lambda m: f"{m.group(1)}{int(m.group(2)) + 1}", html)
    anfang = umnummeriert.index(_ERSTE_ZEILE)
    return umnummeriert[:anfang] + _NEUER_PUNKT + umnummeriert[anfang:]


def vorlage_folgt(html: str) -> str:
    """Sitzung 1001 der Samtgemeinde: „Ö 1“ bekommt nachträglich eine Vorlage."""
    assert _OHNE_VORLAGE_1001 in html
    return html.replace(_OHNE_VORLAGE_1001, _MIT_VORLAGE_1001)


def ohne_tabelle(html: str) -> str:
    """Seite im SessionNet-Layout, aber ohne Tabelle der Tagesordnung (nicht lesbar)."""
    return html.replace("smctablesitzung", "smctablehinweis").replace("si0057_contenttable", "hinweis")


class Seiten:
    """Abruf-Ersatz: Tagesordnung nach Vorgabe, Informationen aus der Fixture."""

    source_name = "fixture"

    def __init__(self, instanz: str, tagesordnung: str) -> None:
        self.instanz = instanz
        self.tagesordnung = tagesordnung

    async def fetch_text(self, url: str) -> str | None:
        if "/si0057." in url:
            return self.tagesordnung
        if "/si0050." in url:
            return lesen(self.instanz, "si0050.html")
        return None


async def sitzung(instanz: str, tagesordnung: str) -> tuple[dict[str, Any], list[AgendaItemKey]]:
    """Sitzung, wie der Adapter sie aus der Tagesordnung baut, mit den Kennungen ihrer Punkte."""
    basis, variante, ksinr = INSTANZEN[instanz]
    config = ScraperConfig(base_url=basis, body_name=instanz, variant=variante)
    adapter = SessionNetAdapter(config, Seiten(instanz, tagesordnung))  # type: ignore[arg-type]
    meeting = await adapter._build_meeting(MeetingStub(ksinr=ksinr, name="Sitzung"), adapter.urls.body_id(), {})
    assert meeting is not None
    return meeting, adapter._agenda_keys[meeting["id"]]


def bestand(meeting: dict[str, Any], keys: list[AgendaItemKey], *, bisherig: bool) -> list[dict[str, Any]]:
    """Zeilen im Bestand nach einem Abruf dieser Sitzung, mit der bisherigen oder der neuen Kennung."""
    return [
        {
            "external_id": key.legacy_id if bisherig else key.item_id,
            "number": item["number"],
            "name": item["name"],
            "public": item["public"],
            "order": item["order"],
            "consultation": item.get("consultation"),
        }
        for item, key in zip(meeting["agendaItem"], keys, strict=True)
    ]


def row(number: str | None, kvonr: int | None = None, public: bool = True, title: str = "TOP") -> AgendaRow:
    return AgendaRow(number=number, title=title, public=public, paper_kvonr=kvonr)


# ---------------------------------------------------------------------------
# Zwei Abrufstände derselben Sitzungsseite (Golden Files)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instanz", list(INSTANZEN))
async def test_einfuegung_vor_einem_punkt_aendert_seine_kennung_nicht(instanz: str) -> None:
    html = lesen(instanz, "si0057.html")
    vorher, keys_vorher = await sitzung(instanz, html)
    nachher, keys_nachher = await sitzung(instanz, mit_neuem_ersten_punkt(html))

    # Bis Issue #863 (Kennung = Nummer) trug danach jede Kennung einen anderen Punkt
    bisher_vorher = {key.consultation: key.legacy_id for key in keys_vorher if key.consultation}
    bisher_nachher = {key.consultation: key.legacy_id for key in keys_nachher if key.consultation}
    assert all(bisher_nachher[c] != kennung for c, kennung in bisher_vorher.items())

    def mit_vorlage(meeting: dict[str, Any]) -> dict[str, tuple[str, str]]:
        return {i["consultation"]: (i["id"], i["number"]) for i in meeting["agendaItem"] if "consultation" in i}

    a, b = mit_vorlage(vorher), mit_vorlage(nachher)
    assert len(a) >= 10
    assert a.keys() == b.keys()
    for consultation, (kennung, nummer) in a.items():
        # Dieselbe Vorlage unter neuer Nummer: dieselbe Kennung
        assert b[consultation][1] != nummer
        assert b[consultation][0] == kennung
        assert "#agendaitem/kvonr-" in kennung

    # Der neue Punkt bekommt eine eigene Kennung, nichts ist doppelt
    ids_nachher = [i["id"] for i in nachher["agendaItem"]]
    assert len(ids_nachher) == len(set(ids_nachher)) == len(vorher["agendaItem"]) + 1
    assert nachher["agendaItem"][0]["name"] == "Einwohnerfragestunde"
    # Beratungen verweisen auf die Kennung ihres Punkts
    for consultation, (kennung, _) in b.items():
        assert consultation.endswith(f"#consultation/{INSTANZEN[instanz][2]}")
        assert kennung in ids_nachher


@pytest.mark.parametrize("instanz", list(INSTANZEN))
async def test_punkte_ohne_vorlage_heissen_nach_ihrer_nummer(instanz: str) -> None:
    """Ohne Vorlage gibt die Seite keine stabile Kennung her: Die Kennung folgt der Nummer (dokumentiert)."""
    for html in (lesen(instanz, "si0057.html"), mit_neuem_ersten_punkt(lesen(instanz, "si0057.html"))):
        meeting, _ = await sitzung(instanz, html)
        ohne = [i for i in meeting["agendaItem"] if "consultation" not in i]
        assert ohne
        for item in ohne:
            assert item["id"] == f"{meeting['id']}#agendaitem/nr-{item['number']}"


def test_kennungsteile() -> None:
    rows = [
        row("1"),
        row("2", kvonr=12770),
        row(None),
        row("3", kvonr=12770),
        row("1", public=False),
        row("2", kvonr=12770, public=False),
        row("1", public=False),
    ]
    assert agenda_item_keys(rows) == [
        "nr-1",
        "kvonr-12770",
        "pos-3",
        "kvonr-12770-2",
        "nr-N1",
        "kvonr-12770-3",
        "nr-N1-2",
    ]


def test_bisherige_kennungsteile_wie_vor_863() -> None:
    """Die Form von vor Issue #863: Nummer, Wiederholung im nichtöffentlichen Teil mit N, sonst mit Position."""
    rows = [row("1"), row("1"), row("1", public=False), row("1", public=False), row(None), row("2", kvonr=5)]
    assert legacy_agenda_item_keys(rows) == ["1", "1-2", "N1", "1-4", "5", "2"]
    samtgemeinde = parse_meeting_agenda((FIXTURES / "samtgemeinde" / "si0057_1001.html").read_text(encoding="utf-8"))
    assert legacy_agenda_item_keys(samtgemeinde.rows) == ["1", "2", "N1"]


def test_bisherige_form_erkennen() -> None:
    sitzung_ = "https://rat.eschweiler.de/bi/si0057.php?__ksinr=4849"
    for teil in ("3.2", "N1", "3-7", "12"):
        assert is_legacy_agenda_item_id(f"{sitzung_}#agendaitem/{teil}")
        assert not SessionNetAdapter.is_removable_agenda_item_id(f"{sitzung_}#agendaitem/{teil}")
    for teil in ("kvonr-12770", "nr-3.2", "nr-N1", "pos-4"):
        assert not is_legacy_agenda_item_id(f"{sitzung_}#agendaitem/{teil}")
        assert SessionNetAdapter.is_removable_agenda_item_id(f"{sitzung_}#agendaitem/{teil}")
    assert not is_legacy_agenda_item_id(sitzung_)
    assert not is_legacy_agenda_item_id(f"{sitzung_}#agendaitem/")


def test_tagesordnung_ohne_tabelle_ist_nicht_gelesen() -> None:
    html = (FIXTURES / "samtgemeinde" / "si0057_1001.html").read_text(encoding="utf-8")
    assert parse_meeting_agenda(html).table_found
    agenda = parse_meeting_agenda(ohne_tabelle(html))
    assert not agenda.table_found
    assert agenda.rows == []


# ---------------------------------------------------------------------------
# Umstellung des Bestands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instanz", list(INSTANZEN))
async def test_umstellung_ordnet_jede_zeile_ihrem_punkt_zu(instanz: str) -> None:
    """Gleicher Stand wie beim letzten Abruf: Jede Zeile bekommt die neue Kennung ihres Punkts."""
    meeting, keys = await sitzung(instanz, lesen(instanz, "si0057.html"))
    renames = plan_agenda_item_renames(keys, bestand(meeting, keys, bisherig=True))
    assert renames == {key.legacy_id: key.item_id for key in keys}


@pytest.mark.parametrize("instanz", list(INSTANZEN))
async def test_umstellung_nach_umnummerierung(instanz: str) -> None:
    """
    Seit dem letzten Abruf umnummeriert: Punkte mit Vorlage finden ihre Zeile über die Beratung, Punkte ohne Vorlage
    über den eindeutigen Namen. Keine Zeile behält die bisherige Form, kein Ziel ist doppelt, kein Punkt doppelt.
    """
    html = lesen(instanz, "si0057.html")
    vorher, keys_vorher = await sitzung(instanz, html)
    nachher, keys_nachher = await sitzung(instanz, mit_neuem_ersten_punkt(html))
    stored = bestand(vorher, keys_vorher, bisherig=True)
    renames = plan_agenda_item_renames(keys_nachher, stored)

    assert set(renames) == {row["external_id"] for row in stored}
    assert len(set(renames.values())) == len(renames)
    assert not any(is_legacy_agenda_item_id(target) for target in renames.values())
    bisher = {key.consultation: key.legacy_id for key in keys_vorher if key.consultation}
    for key in keys_nachher:
        if key.consultation:
            assert renames[bisher[key.consultation]] == key.item_id
    # Ohne Vorlage: die Zeile mit demselben Namen (in den Golden Files eindeutig), nicht die unter der alten Nummer
    zeile_nach_name = {row["name"]: row["external_id"] for row in stored if not row["consultation"]}
    for key in keys_nachher:
        if not key.consultation and key.name in zeile_nach_name:
            assert renames[zeile_nach_name[key.name]] == key.item_id
    # Jede Zeile kommt bei einem Punkt der Seite an, nur der neue Punkt bekommt eine neue Zeile
    seite = {key.item_id for key in keys_nachher}
    assert set(renames.values()) <= seite
    assert len(seite - set(renames.values())) == 1


@pytest.mark.parametrize("instanz", list(INSTANZEN))
async def test_umgestellter_bestand_bleibt_unberuehrt(instanz: str) -> None:
    meeting, keys = await sitzung(instanz, lesen(instanz, "si0057.html"))
    assert plan_agenda_item_renames(keys, bestand(meeting, keys, bisherig=False)) == {}
    assert plan_agenda_item_renames(keys, []) == {}


def _key(teil: str, bisher: str, *, name: str = "", public: bool = True, vorlage: int | None = None) -> AgendaItemKey:
    sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1"
    return AgendaItemKey(
        item_id=f"{sitzung_}#agendaitem/{teil}",
        legacy_id=f"{sitzung_}#agendaitem/{bisher}",
        name=name,
        public=public,
        consultation=f"{BASE_URL}vo0050.asp?__kvonr={vorlage}#consultation/1" if vorlage else None,
    )


def _zeile(
    teil: str, number: str | None, *, public: bool = True, vorlage: int | None = None, order: int = 1
) -> dict[str, Any]:
    key = _key(teil, teil, public=public, vorlage=vorlage)
    return {
        "external_id": key.item_id,
        "number": number,
        "public": public,
        "order": order,
        "consultation": key.consultation,
    }


def test_ohne_vorlage_nach_nummer_und_teil() -> None:
    sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1#agendaitem/"
    keys = [_key("nr-1", "1"), _key("nr-2", "2"), _key("nr-N1", "N1", public=False)]
    stored = [
        # Unter der Nummer stand ein Punkt mit Vorlage: nicht dieser Punkt, die Zeile heißt nach ihrer Vorlage
        _zeile("1", "1", vorlage=7),
        # Ohne Vorlage, gleich öffentlich: dieselbe Zeile, auch wenn der Name ein anderer ist
        _zeile("2", "2", order=2),
        # Damals öffentlich, jetzt nichtöffentlich: nicht dieser Punkt; nach eigenen Angaben nr-1, das ist vergeben
        _zeile("N1", "1", order=3),
    ]
    assert plan_agenda_item_renames(keys, stored) == {
        f"{sitzung_}1": f"{sitzung_}kvonr-7",
        f"{sitzung_}2": f"{sitzung_}nr-2",
        f"{sitzung_}N1": f"{sitzung_}nr-1-2",
    }


def test_ohne_vorlage_zuerst_ueber_den_namen() -> None:
    sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1#agendaitem/"
    # Umnummeriert: „Mitteilungen“ steht jetzt unter 3, unter 2 steht ein neuer Punkt
    keys = [_key("nr-2", "2", name="Einwohnerfragestunde"), _key("nr-3", "3", name="Mitteilungen")]
    stored = [
        {**_zeile("2", "2"), "name": "Mitteilungen"},
        # „Anfragen“ steht nicht mehr auf der Tagesordnung: Kennung nach eigenen Angaben, nr-3 ist vergeben
        {**_zeile("3", "3", order=2), "name": "Anfragen"},
    ]
    assert plan_agenda_item_renames(keys, stored) == {
        f"{sitzung_}2": f"{sitzung_}nr-3",
        f"{sitzung_}3": f"{sitzung_}nr-3-2",
    }
    # Gleicher Name zweimal auf der Seite: nicht eindeutig, dann gilt die Nummer
    doppelt = [_key("nr-1", "1", name="Mitteilungen"), _key("nr-5", "5", name="Mitteilungen")]
    zeilen = [{**_zeile("5", "5"), "name": "Mitteilungen"}]
    assert plan_agenda_item_renames(doppelt, zeilen) == {f"{sitzung_}5": f"{sitzung_}nr-5"}


def test_dieselbe_vorlage_an_zwei_punkten() -> None:
    sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1#agendaitem/"
    keys = [_key("kvonr-5", "3", vorlage=5), _key("kvonr-5-2", "N1", public=False, vorlage=5)]
    # Gleich viele Zeilen mit dieser Beratung: paarweise in der Reihenfolge
    beide = [_zeile("2", "2", vorlage=5, order=2), _zeile("N1", "1", public=False, vorlage=5, order=9)]
    assert plan_agenda_item_renames(keys, beide) == {f"{sitzung_}2": keys[0].item_id, f"{sitzung_}N1": keys[1].item_id}
    # Eine Zeile für zwei Punkte: nur unter der bisherigen Kennung
    eine = [_zeile("N1", "1", public=False, vorlage=5)]
    assert plan_agenda_item_renames(keys, eine) == {f"{sitzung_}N1": keys[1].item_id}
    # Sonst keinem Punkt zugeordnet, die Zeile heißt nach ihrer Vorlage mit Zählung
    assert plan_agenda_item_renames(keys, [_zeile("7", "7", vorlage=5)]) == {f"{sitzung_}7": f"{sitzung_}kvonr-5-3"}


# ---------------------------------------------------------------------------
# Rückweg
# ---------------------------------------------------------------------------


def test_rueckweg_stellt_genau_die_bisherigen_kennungen_her() -> None:
    sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1#agendaitem/"
    agenda = [
        {"id": f"{sitzung_}nr-1", "number": "1", "public": True},
        {"id": f"{sitzung_}kvonr-5", "number": "2", "public": True},
        {"id": f"{sitzung_}nr-3", "number": "3", "public": True},
        {"id": f"{sitzung_}nr-N1", "number": "1", "public": False},
    ]
    stored = [
        # umgeschrieben: id = uuid5 der bisherigen Kennung
        {"id": canonical_id(f"{sitzung_}1"), "external_id": f"{sitzung_}nr-1", "previous_id": f"{sitzung_}1"},
        # umgeschrieben, inzwischen unter anderer Nummer: trotzdem genau die bisherige Kennung
        {"id": canonical_id(f"{sitzung_}3"), "external_id": f"{sitzung_}kvonr-5", "previous_id": f"{sitzung_}3"},
        # seit der Umstellung angelegt: die Kennung, die der bisherige Abruf vergäbe – „3“ ist vergeben
        {"id": canonical_id(f"{sitzung_}nr-3"), "external_id": f"{sitzung_}nr-3"},
        {"id": canonical_id(f"{sitzung_}nr-N1"), "external_id": f"{sitzung_}nr-N1"},
        # umgeschrieben, aber die bisherige Kennung ist verloren: kein Rückweg
        {"id": canonical_id(f"{sitzung_}9"), "external_id": f"{sitzung_}nr-9"},
    ]
    plan = plan_agenda_item_revert(agenda, stored, canonical_id)
    assert plan.restore == {f"{sitzung_}nr-1": f"{sitzung_}1", f"{sitzung_}kvonr-5": f"{sitzung_}3"}
    assert plan.translate == {f"{sitzung_}nr-N1": f"{sitzung_}N1"}
    assert plan.unresolved == [f"{sitzung_}nr-9"]


# ---------------------------------------------------------------------------
# Runner: Umstellung vor dem Speichern, Löschabgleich der Tagesordnung
# ---------------------------------------------------------------------------


class Speicher(FakeStorage):
    """Bestand mit den Punkten von Sitzungen; hält Umstellungen fest."""

    def __init__(self, protokoll: list[tuple[str, str]], punkte: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__()
        self.protokoll = protokoll
        self.punkte = punkte
        self.umgestellt: dict[str, dict[str, str]] = {}
        self.agenda_items: set[str] = set()

    async def get_agenda_items_of_meeting(self, meeting_external_id: str) -> list[dict[str, Any]]:
        return list(self.punkte.get(meeting_external_id, []))

    async def rename_agenda_items(
        self, meeting_external_id: str, renames: dict[str, str], keep_previous: bool = True
    ) -> int:
        self.protokoll.append(("umgestellt", meeting_external_id))
        self.umgestellt[meeting_external_id] = dict(renames)
        return len(renames)


class Orchestrator(FakeOrchestrator):
    def __init__(self, protokoll: list[tuple[str, str]], punkte: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__()
        self.storage = Speicher(protokoll, punkte)
        self.protokoll = protokoll
        self.gespeichert: dict[str, Any] = {}

    async def _store_entity(self, processed: Any, body_id: Any, entity_type: str, source_name: Any) -> bool:
        self.protokoll.append((entity_type, processed.external_id))
        self.gespeichert[processed.external_id] = processed
        return await super()._store_entity(processed, body_id, entity_type, source_name)


class Tagesordnung(SamtgemeindeFetcher):
    """Samtgemeinde-Seiten; die Tagesordnung von Sitzung 1001 nach Vorgabe."""

    seite_1001: str | None = None

    async def fetch_text(self, url: str) -> str | None:
        if "si0057" in url and "__ksinr=1001" in url and self.seite_1001 is not None:
            self.requests.append(url)
            return self.seite_1001
        return await super().fetch_text(url)


@pytest.fixture
def samtgemeinde(monkeypatch: pytest.MonkeyPatch) -> Tagesordnung:
    fetcher = Tagesordnung()

    class Abruf:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> SamtgemeindeFetcher:
            return fetcher

        async def __aexit__(self, *args: Any) -> None:
            return None

    class September(CrawlWindow):
        @classmethod
        def from_days(cls, days_back: int, days_ahead: int, today: date | None = None) -> CrawlWindow:
            return SEPTEMBER

    monkeypatch.setattr(runner_module, "PoliteFetcher", Abruf)
    monkeypatch.setattr(runner_module, "CrawlWindow", September)
    monkeypatch.setattr(runner_module.settings, "text_extraction_enabled", False)
    monkeypatch.setattr(runner_module.settings, "elasticsearch_indexing_enabled", False)
    monkeypatch.setattr(runner_module.settings, "scraper_tombstone_full_crawls", 2)
    return fetcher


def _quelle(scraper_state: dict[str, Any] | None = None) -> FakeSource:
    quelle = FakeSource(scraper_state)
    quelle.sync_config["scraper"]["bodies"] = [{"cpanr": 1}]
    return quelle


async def test_runner_stellt_vor_dem_verarbeiten_um(samtgemeinde: Tagesordnung) -> None:
    beratung = f"{BASE_URL}vo0050.asp?__kvonr=5001#consultation/1001"
    punkte = {
        SITZUNG_1001: [
            {"external_id": f"{SITZUNG_1001}#agendaitem/1", "number": "1", "public": True, "order": 1},
            {"external_id": f"{SITZUNG_1001}#agendaitem/2", "number": "2", "public": True, "consultation": beratung},
            # Bisher öffentlich, jetzt im nichtöffentlichen Teil: nicht derselbe Punkt
            {"external_id": f"{SITZUNG_1001}#agendaitem/N1", "number": "1", "public": True, "order": 3},
        ]
    }
    protokoll: list[tuple[str, str]] = []
    orchestrator = Orchestrator(protokoll, punkte)

    result = await ScraperSyncRunner(orchestrator, _quelle()).run(full=True)  # type: ignore[arg-type]

    assert result.success, result.errors
    assert orchestrator.storage.umgestellt == {
        SITZUNG_1001: {
            f"{SITZUNG_1001}#agendaitem/1": f"{SITZUNG_1001}#agendaitem/nr-1",
            f"{SITZUNG_1001}#agendaitem/2": f"{SITZUNG_1001}#agendaitem/kvonr-5001",
            f"{SITZUNG_1001}#agendaitem/N1": f"{SITZUNG_1001}#agendaitem/nr-1-2",
        }
    }
    # Vor dem Speichern der Sitzung, danach erst die Sitzung selbst
    assert protokoll.index(("umgestellt", SITZUNG_1001)) < protokoll.index(("meeting", SITZUNG_1001))
    # Die abgelöste Kennung steht an den gelieferten Punkten (für den Rückweg)
    gespeichert = orchestrator.gespeichert[SITZUNG_1001].raw_json["agendaItem"]
    assert [item.get("mandari:previousId") for item in gespeichert] == [
        f"{SITZUNG_1001}#agendaitem/1",
        f"{SITZUNG_1001}#agendaitem/2",
        None,
    ]


async def test_loeschabgleich_der_tagesordnung(samtgemeinde: Tagesordnung) -> None:
    """
    „Vorlage folgt“: TOP 1 bekommt eine Vorlage und damit eine neue Kennung. Die Zeile unter ``nr-1`` liefert die
    Seite nicht mehr; nach zwei vollständigen Vollabgleichen in Folge (hier die Grenze) ist sie markiert.
    """
    alt = f"{SITZUNG_1001}#agendaitem/nr-1"
    bisherig = f"{SITZUNG_1001}#agendaitem/7"
    samtgemeinde.seite_1001 = vorlage_folgt((FIXTURES / "samtgemeinde" / "si0057_1001.html").read_text("utf-8"))
    state: dict[str, Any] = {}

    async def lauf(full: bool = True) -> FakeOrchestrator:
        orchestrator = Orchestrator([], {})
        orchestrator.storage.agenda_items = {alt, bisherig, f"{SITZUNG_1001}#agendaitem/kvonr-5001"}
        await ScraperSyncRunner(orchestrator, _quelle(state)).run(full=full)  # type: ignore[arg-type]
        state.update(orchestrator.storage.state)
        return orchestrator

    assert (await lauf()).marked == []
    assert state["missing"]["agendaitem"] == {alt: 1}
    # Ein inkrementeller Lauf zählt nicht
    assert (await lauf(full=False)).marked == []
    # Seite ohne Tabelle der Tagesordnung: nicht gelesen, die Zählung beginnt von vorn
    seite = samtgemeinde.seite_1001
    samtgemeinde.seite_1001 = ohne_tabelle(seite)
    assert (await lauf()).marked == []
    assert state["missing"]["agendaitem"] == {}
    samtgemeinde.seite_1001 = seite
    assert (await lauf()).marked == []
    # Zweiter vollständiger Vollabgleich in Folge: markiert; die bisherige Form bleibt der Umstellung überlassen
    assert (await lauf()).marked == [alt]


# ---------------------------------------------------------------------------
# Befehl top-kennungen-zurueck
# ---------------------------------------------------------------------------


class RueckwegSpeicher:
    """Bestand einer SessionNet-Quelle mit einer umgestellten Sitzung (Attrappe für den Befehl)."""

    def __init__(self, source_type: str = "scraper:sessionnet", *, verloren: bool = False) -> None:
        sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1#agendaitem/"
        self.source = SimpleNamespace(
            id=uuid.uuid4(), url=BASE_URL, sync_config={"source_type": source_type}, name="Samtgemeinde"
        )
        self.agenda = [{"id": f"{sitzung_}kvonr-5", "number": "2", "public": True}]
        self.rows = [
            {"id": canonical_id(f"{sitzung_}2"), "external_id": f"{sitzung_}kvonr-5", "previous_id": f"{sitzung_}2"}
        ]
        if verloren:
            self.rows.append({"id": canonical_id(f"{sitzung_}9"), "external_id": f"{sitzung_}nr-9"})
        self.geschrieben: list[tuple[dict[str, str], bool]] = []

    async def get_source_by_url(self, url: str) -> SimpleNamespace | None:
        return self.source if url == BASE_URL else None

    async def get_meeting_agendas_of_source(self, source_id: uuid.UUID) -> list[tuple[str, list[Any]]]:
        return [(f"{BASE_URL}si0057.asp?__ksinr=1", self.agenda)]

    async def get_agenda_items_of_meeting(self, meeting_external_id: str) -> list[dict[str, Any]]:
        return self.rows

    async def rename_agenda_items(self, meeting: str, renames: dict[str, str], *, keep_previous: bool = True) -> int:
        self.geschrieben.append((dict(renames), keep_previous))
        return len(renames)


def _befehl(monkeypatch: pytest.MonkeyPatch, speicher: RueckwegSpeicher, *argumente: str) -> tuple[int, str]:
    class BefehlOrchestrator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.storage = speicher

        async def __aenter__(self) -> BefehlOrchestrator:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    ausgabe = io.StringIO()
    monkeypatch.setattr(cli, "SyncOrchestrator", BefehlOrchestrator)
    monkeypatch.setattr(cli, "console", Console(file=ausgabe, width=250))
    result = CliRunner().invoke(cli.app, ["top-kennungen-zurueck", "--source", BASE_URL, *argumente])
    return result.exit_code, ausgabe.getvalue()


def test_befehl_rueckweg(monkeypatch: pytest.MonkeyPatch) -> None:
    speicher = RueckwegSpeicher()
    code, text = _befehl(monkeypatch, speicher)
    assert code == 0, text
    assert "Punkte mit abgelöster Kennung: 1" in text
    assert speicher.geschrieben == []

    code, text = _befehl(monkeypatch, speicher, "--ausfuehren")
    assert code == 0, text
    sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1#agendaitem/"
    assert speicher.geschrieben == [({f"{sitzung_}kvonr-5": f"{sitzung_}2"}, False)]


def test_befehl_rueckweg_bricht_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    # Umgeschriebene Zeile ohne abgelöste Kennung: nicht zurückfallen
    code, text = _befehl(monkeypatch, RueckwegSpeicher(verloren=True))
    assert code == 1
    assert "ohne Rückweg" in text
    # Keine SessionNet-Quelle
    code, text = _befehl(monkeypatch, RueckwegSpeicher("oparl"))
    assert code == 1
    assert "Keine SessionNet-Quelle" in text

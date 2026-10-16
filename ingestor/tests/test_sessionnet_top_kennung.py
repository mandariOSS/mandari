"""
Kennungen der Tagesordnungspunkte beim Abruf aus SessionNet-Sitzungsseiten (Issue #863).

Ein Punkt mit Vorlage heißt nach ihr (``__kvonr``), nicht nach seiner Nummer: Fügt die Verwaltung davor einen Punkt
ein, behält er seine Kennung. Geprüft mit zwei Abrufständen derselben Sitzungsseite: dem eingefrorenen Golden File
und demselben Stand mit einem neuen ersten Punkt und verschobenen Nummern. Dazu die Umstellung des Bestands: Zeilen
mit der bisherigen Kennung werden umgeschrieben (``id`` bleibt), nur wenn eindeutig ist, welcher Punkt es ist.
Kein Netzwerk, keine Datenbank (die Umstellung gegen PostgreSQL prüft ``test_sessionnet_top_kennung_db.py``).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

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


def lesen(instanz: str, name: str) -> str:
    return (FIXTURES / instanz / name).read_text(encoding="utf-8", errors="replace")


def mit_neuem_ersten_punkt(html: str) -> str:
    """Zweiter Abrufstand: ein neuer Punkt „Ö 1“ vor allen anderen, deren Nummern rücken um eins auf."""
    umnummeriert = _OEFFENTLICHE_NUMMER.sub(lambda m: f"{m.group(1)}{int(m.group(2)) + 1}", html)
    anfang = umnummeriert.index(_ERSTE_ZEILE)
    return umnummeriert[:anfang] + _NEUER_PUNKT + umnummeriert[anfang:]


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


def bestand(keys: list[AgendaItemKey], *, bisherig: bool) -> list[dict[str, Any]]:
    """Zeilen im Bestand nach einem Abruf mit diesen Punkten, mit der bisherigen oder der neuen Kennung."""
    return [
        {
            "external_id": key.legacy_id if bisherig else key.item_id,
            "name": key.name,
            "public": key.public,
            "order": position,
            "consultation": key.consultation,
        }
        for position, key in enumerate(keys, start=1)
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
    for teil in ("kvonr-12770", "nr-3.2", "nr-N1", "pos-4"):
        assert not is_legacy_agenda_item_id(f"{sitzung_}#agendaitem/{teil}")
    assert not is_legacy_agenda_item_id(sitzung_)
    assert not is_legacy_agenda_item_id(f"{sitzung_}#agendaitem/")


# ---------------------------------------------------------------------------
# Umstellung des Bestands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("instanz", list(INSTANZEN))
async def test_umstellung_ordnet_jede_zeile_ihrem_punkt_zu(instanz: str) -> None:
    """Gleicher Stand wie beim letzten Abruf: Jede Zeile bekommt die neue Kennung ihres Punkts."""
    _, keys = await sitzung(instanz, lesen(instanz, "si0057.html"))
    renames = plan_agenda_item_renames(keys, bestand(keys, bisherig=True))
    assert renames == {key.legacy_id: key.item_id for key in keys}


@pytest.mark.parametrize("instanz", list(INSTANZEN))
async def test_umstellung_nach_umnummerierung_nur_ueber_die_vorlage(instanz: str) -> None:
    """
    Seit dem letzten Abruf umnummeriert: Punkte mit Vorlage finden ihre Zeile über die Beratung. Unter den
    bisherigen Nummern der Punkte ohne Vorlage stehen jetzt andere Punkte; diese Zeilen bleiben, wie sie sind.
    """
    html = lesen(instanz, "si0057.html")
    _, vorher = await sitzung(instanz, html)
    _, nachher = await sitzung(instanz, mit_neuem_ersten_punkt(html))
    renames = plan_agenda_item_renames(nachher, bestand(vorher, bisherig=True))

    bisher = {key.consultation: key.legacy_id for key in vorher if key.consultation}
    assert renames == {bisher[key.consultation]: key.item_id for key in nachher if key.consultation}


@pytest.mark.parametrize("instanz", list(INSTANZEN))
async def test_umgestellter_bestand_bleibt_unberuehrt(instanz: str) -> None:
    _, keys = await sitzung(instanz, lesen(instanz, "si0057.html"))
    assert plan_agenda_item_renames(keys, bestand(keys, bisherig=False)) == {}
    assert plan_agenda_item_renames(keys, []) == {}


def _key(
    teil: str, bisher: str, *, name: str = "TOP", public: bool = True, vorlage: int | None = None
) -> AgendaItemKey:
    sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1"
    return AgendaItemKey(
        item_id=f"{sitzung_}#agendaitem/{teil}",
        legacy_id=f"{sitzung_}#agendaitem/{bisher}",
        name=name,
        public=public,
        consultation=f"{BASE_URL}vo0050.asp?__kvonr={vorlage}#consultation/1" if vorlage else None,
    )


def _zeile(teil: str, *, name: str = "TOP", public: bool = True, vorlage: int | None = None, order: int = 1) -> dict:
    key = _key(teil, teil, name=name, public=public, vorlage=vorlage)
    return {
        "external_id": key.item_id,
        "name": name,
        "public": public,
        "order": order,
        "consultation": key.consultation,
    }


def test_im_zweifel_keine_umstellung() -> None:
    keys = [
        _key("nr-1", "1", name="Eröffnung"),
        _key("nr-2", "2", name="Mitteilungen"),
        _key("nr-N1", "N1", name="Grundstücke", public=False),
    ]
    stored = [
        # Unter der Nummer stand ein Punkt mit Vorlage: nicht derselbe Punkt
        _zeile("1", name="Eröffnung", vorlage=7),
        # Anderer Name: nach einer Umnummerierung ein anderer Punkt
        _zeile("2", name="Anfragen", order=2),
        # Damals öffentlich, jetzt nichtöffentlich
        _zeile("N1", name="Grundstücke", order=3),
    ]
    assert plan_agenda_item_renames(keys, stored) == {}


def test_dieselbe_vorlage_an_zwei_punkten() -> None:
    keys = [_key("kvonr-5", "3", vorlage=5), _key("kvonr-5-2", "N1", public=False, vorlage=5)]
    # Gleich viele Zeilen mit dieser Beratung: paarweise in der Reihenfolge
    beide = [_zeile("2", vorlage=5, order=2), _zeile("N1", public=False, vorlage=5, order=9)]
    sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1#agendaitem/"
    assert plan_agenda_item_renames(keys, beide) == {f"{sitzung_}2": keys[0].item_id, f"{sitzung_}N1": keys[1].item_id}
    # Eine Zeile für zwei Punkte: nur unter der bisherigen Kennung
    assert plan_agenda_item_renames(keys, [_zeile("N1", public=False, vorlage=5)]) == {f"{sitzung_}N1": keys[1].item_id}
    assert plan_agenda_item_renames(keys, [_zeile("7", vorlage=5)]) == {}


# ---------------------------------------------------------------------------
# Runner: Umstellung vor dem Speichern der Sitzung
# ---------------------------------------------------------------------------


class Speicher(FakeStorage):
    """Bestand mit den Punkten einer Sitzung unter der bisherigen Kennung; hält Umstellungen fest."""

    def __init__(self, protokoll: list[tuple[str, str]], punkte: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__()
        self.protokoll = protokoll
        self.punkte = punkte
        self.umgestellt: dict[str, dict[str, str]] = {}

    async def get_agenda_items_of_meeting(self, meeting_external_id: str) -> list[dict[str, Any]]:
        return list(self.punkte.get(meeting_external_id, []))

    async def rename_agenda_items(self, meeting_external_id: str, renames: dict[str, str]) -> int:
        self.protokoll.append(("umgestellt", meeting_external_id))
        self.umgestellt[meeting_external_id] = dict(renames)
        return len(renames)


class Orchestrator(FakeOrchestrator):
    def __init__(self, protokoll: list[tuple[str, str]], punkte: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__()
        self.storage = Speicher(protokoll, punkte)
        self.protokoll = protokoll

    async def _store_entity(self, processed: Any, body_id: Any, entity_type: str, source_name: Any) -> bool:
        self.protokoll.append((entity_type, processed.external_id))
        return await super()._store_entity(processed, body_id, entity_type, source_name)


@pytest.fixture
def samtgemeinde(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = SamtgemeindeFetcher()

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


async def test_runner_stellt_vor_dem_speichern_um(samtgemeinde: None) -> None:
    sitzung_ = f"{BASE_URL}si0057.asp?__ksinr=1001"
    beratung = f"{BASE_URL}vo0050.asp?__kvonr=5001#consultation/1001"
    eroeffnung = "Eröffnung und Feststellung der Beschlussfähigkeit"
    punkte = {
        sitzung_: [
            {"external_id": f"{sitzung_}#agendaitem/1", "name": eroeffnung, "public": True, "order": 1},
            {"external_id": f"{sitzung_}#agendaitem/2", "name": "Haushalt", "public": True, "consultation": beratung},
            # Bisher öffentlich, jetzt im nichtöffentlichen Teil: nicht derselbe Punkt
            {"external_id": f"{sitzung_}#agendaitem/N1", "name": "Grundstücksangelegenheiten", "public": True},
        ]
    }
    protokoll: list[tuple[str, str]] = []
    orchestrator = Orchestrator(protokoll, punkte)
    quelle = FakeSource()
    quelle.sync_config["scraper"]["bodies"] = [{"cpanr": 1}]

    result = await ScraperSyncRunner(orchestrator, quelle).run(full=True)  # type: ignore[arg-type]

    assert result.success, result.errors
    assert orchestrator.storage.umgestellt == {
        sitzung_: {
            f"{sitzung_}#agendaitem/1": f"{sitzung_}#agendaitem/nr-1",
            f"{sitzung_}#agendaitem/2": f"{sitzung_}#agendaitem/kvonr-5001",
        }
    }
    # Vor dem Speichern der Sitzung, danach erst die Sitzung selbst
    assert protokoll.index(("umgestellt", sitzung_)) < protokoll.index(("meeting", sitzung_))

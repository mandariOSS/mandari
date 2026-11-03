# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abruffenster je Quelle für alle Abgleiche (``sync_config["sync_window"]``, Issue #1186).

Die Stadt Köln erlaubt Abrufe ihrer OParl-Schnittstelle nur zwischen 23:00 und 01:00 Uhr. Außerhalb des Fensters
startet der Daemon die Quelle nicht (auch nicht als ersten Lauf, nach einer Schonung, für den Vollabgleich oder auf
einen Auslöser hin), ``sync`` auf der Kommandozeile ruft nichts ab, und ein laufender Abgleich holt nach dem Ende des
Fensters keine Seite mehr, ob OParl oder Scraper; die Texterkennung lädt keine Datei der Quelle. Ohne Netz und ohne
Datenbank.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from mandari_oparl import IdBases
from typer.testing import CliRunner

from src import main as cli
from src.client.oparl_client import MODIFIED_SINCE_INCONCLUSIVE, FetchResult, FetchWindowClosedError, OParlClient
from src.client.source_options import FullSyncWindow, SourceFetchOptions
from src.config import settings
from src.extraction.extractor import TextExtractor
from src.metrics import metrics
from src.scheduler.takt import REASON_WINDOW, SyncSchedule, SyncState, default_full_sync_window, full_sync_due
from src.scrapers import runner as runner_module
from src.scrapers.politeness import CrawlWindowClosedError, PoliteFetcher
from src.scrapers.runner import ScraperSyncRunner
from src.sync import orchestrator as orchestrator_module
from src.sync.orchestrator import SyncOrchestrator, SyncResult
from src.sync.processor import OParlProcessor
from tests.test_scheduler_takt import Uhr, daemon, durchlauf, naechster, quelle, zustand
from tests.test_sessionnet_mehrere_koerperschaften import SEPTEMBER, FakeOrchestrator, FakeSource, SamtgemeindeFetcher

BERLIN = ZoneInfo("Europe/Berlin")
DIENSTAG = date(2026, 10, 13)
MITTWOCH = date(2026, 10, 14)
KOELN = {"sync_window": {"start": "23:00", "end": "01:00"}}


def berlin(tag: date, stunde: int, minute: int = 0) -> datetime:
    return datetime.combine(tag, time(stunde, minute), tzinfo=BERLIN).astimezone(UTC)


def koeln_fenster() -> FullSyncWindow:
    fenster = FullSyncWindow.parse(KOELN["sync_window"])
    assert fenster is not None
    return fenster


# ---------------------------------------------------------------------------
# Fenster: Einstellung, Ränder, Mitternacht, Zeitzone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wert",
    [
        None,
        "23:00-01:00",
        {},
        {"start": "23:00"},
        {"start": "1:00", "end": "1:00"},
        {"start": "24:00", "end": "01:00"},
        {"start": "23:00", "end": "01:00", "tz": "Mars/Olympus"},
    ],
)
def test_ungueltiges_abruffenster_gilt_als_nicht_gesetzt(wert: Any) -> None:
    options = SourceFetchOptions.from_sync_config({"sync_window": wert})
    assert options.sync_window is None
    assert options.outside_sync_window(berlin(DIENSTAG, 14)) is False
    assert options.fetch_deadline(berlin(DIENSTAG, 14)) is None


def test_abruffenster_wird_gelesen() -> None:
    fenster = SourceFetchOptions.from_sync_config(KOELN).sync_window
    assert fenster is not None
    assert (fenster.start, fenster.end, str(fenster.tz)) == (time(23), time(1), "Europe/Berlin")
    assert fenster.label() == "23:00–01:00 Uhr (Europe/Berlin)"


@pytest.mark.parametrize(
    ("jetzt", "ende", "naechstes"),
    [
        # vor dem Fenster
        (berlin(DIENSTAG, 14), None, berlin(DIENSTAG, 23)),
        (berlin(DIENSTAG, 22, 59), None, berlin(DIENSTAG, 23)),
        # der Beginn gehört dazu
        (berlin(DIENSTAG, 23), berlin(MITTWOCH, 1), berlin(DIENSTAG, 23)),
        (berlin(DIENSTAG, 23, 30), berlin(MITTWOCH, 1), berlin(DIENSTAG, 23, 30)),
        # nach Mitternacht noch im Fenster des Vortags
        (berlin(MITTWOCH, 0), berlin(MITTWOCH, 1), berlin(MITTWOCH, 0)),
        (berlin(MITTWOCH, 0, 59), berlin(MITTWOCH, 1), berlin(MITTWOCH, 0, 59)),
        # das Ende gehört nicht dazu: nächstes Fenster am selben Abend
        (berlin(MITTWOCH, 1), None, berlin(MITTWOCH, 23)),
        (berlin(MITTWOCH, 1, 1), None, berlin(MITTWOCH, 23)),
    ],
)
def test_fenster_ueber_mitternacht(jetzt: datetime, ende: datetime | None, naechstes: datetime) -> None:
    fenster = koeln_fenster()
    assert fenster.closes_at(jetzt) == ende
    assert fenster.next_open(jetzt) == naechstes
    assert SourceFetchOptions.from_sync_config(KOELN).outside_sync_window(jetzt) is (ende is None)


def test_fenster_in_eigener_zeitzone() -> None:
    # 22:00 bis 23:00 Uhr UTC ist in Berlin (Sommerzeit) 00:00 bis 01:00 Uhr
    fenster = FullSyncWindow.parse({"start": "22:00", "end": "23:00", "tz": "UTC"})
    assert fenster is not None
    assert fenster.closes_at(berlin(MITTWOCH, 0, 30)) == berlin(MITTWOCH, 1)
    assert fenster.closes_at(berlin(DIENSTAG, 23, 30)) is None
    assert fenster.next_open(berlin(DIENSTAG, 12)) == berlin(MITTWOCH, 0)


def test_fenster_bei_der_zeitumstellung() -> None:
    """Umstellung auf Winterzeit am 25.10.2026 um 03:00 Uhr: Das Fenster gilt in Ortszeit, vorher wie nachher."""
    fenster = koeln_fenster()
    samstag, sonntag = date(2026, 10, 24), date(2026, 10, 25)
    assert fenster.next_open(berlin(samstag, 12)) == datetime(2026, 10, 24, 21, tzinfo=UTC)  # 23:00 Sommerzeit
    assert fenster.closes_at(berlin(samstag, 23, 30)) == datetime(2026, 10, 24, 23, tzinfo=UTC)  # 01:00 Sommerzeit
    assert fenster.next_open(berlin(sonntag, 12)) == datetime(2026, 10, 25, 22, tzinfo=UTC)  # 23:00 Winterzeit


# ---------------------------------------------------------------------------
# Vollabgleich im Abruffenster
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("voll", "erwartet"),
    [
        # Köln: Vollabgleich nur nach Mitternacht
        ({"start": "00:00", "end": "06:00"}, (time(0), time(1))),
        # umfasst das Abruffenster: die Stücke vor und nach Mitternacht ergeben wieder ein Fenster
        ({"start": "22:00", "end": "02:00"}, (time(23), time(1))),
        # liegt darin
        ({"start": "23:30", "end": "00:30"}, (time(23, 30), time(0, 30))),
        # liegt außerhalb oder in einer anderen Zeitzone: gilt als nicht gesetzt, es gilt das Abruffenster
        ({"start": "02:00", "end": "05:00"}, (time(23), time(1))),
        ({"start": "00:00", "end": "06:00", "tz": "UTC"}, (time(23), time(1))),
        # ohne eigenes Fenster: das Abruffenster, nicht das Standardfenster des Daemons
        (None, (time(23), time(1))),
    ],
)
def test_fenster_des_vollabgleichs_liegt_im_abruffenster(voll: Any, erwartet: tuple[time, time]) -> None:
    options = SourceFetchOptions.from_sync_config({**KOELN, "full_sync_window": voll})
    assert options.full_sync_window is not None
    assert (options.full_sync_window.start, options.full_sync_window.end) == erwartet
    assert options.full_window(default_full_sync_window(1)) == options.full_sync_window


def test_schnittmenge_in_zwei_stuecken() -> None:
    tags = FullSyncWindow.parse({"start": "05:00", "end": "23:00"})
    assert tags is not None
    lang = FullSyncWindow.parse({"start": "21:00", "end": "06:00"})
    gleich = FullSyncWindow.parse({"start": "22:00", "end": "06:00"})
    draussen = FullSyncWindow.parse({"start": "23:00", "end": "05:00"})
    assert lang is not None and gleich is not None and draussen is not None
    # Das längere Stück gilt
    assert lang.within(tags) == FullSyncWindow(start=time(21), end=time(23), tz=BERLIN)
    # Gleich lang: das, das nach dem Beginn des Abruffensters zuerst beginnt
    assert gleich.within(tags) == FullSyncWindow(start=time(5), end=time(6), tz=BERLIN)
    assert draussen.within(tags) is None


def test_vollabgleich_nur_im_teil_des_abruffensters() -> None:
    options = SourceFetchOptions.from_sync_config({**KOELN, "full_sync_window": {"start": "00:00", "end": "06:00"}})
    standard = default_full_sync_window(1)
    # 23:30 Uhr: im Abruffenster, vor dem Fenster des Vollabgleichs: Änderungsabgleich bis 01:00 Uhr
    assert options.full_sync_plan(True, now=berlin(DIENSTAG, 23, 30), default_window=standard) == (False, None)
    assert options.fetch_deadline(berlin(DIENSTAG, 23, 30)) == berlin(MITTWOCH, 1)
    # 00:30 Uhr: Vollabgleich bis 01:00 Uhr
    plan = options.full_sync_plan(True, now=berlin(MITTWOCH, 0, 30), default_window=standard)
    assert plan == (True, berlin(MITTWOCH, 1))
    # Das Standardfenster des Daemons (01:00 bis 06:00 Uhr) gilt nicht
    assert options.full_sync_plan(True, now=berlin(MITTWOCH, 2), default_window=standard) == (False, None)
    assert full_sync_due(options, standard, berlin(MITTWOCH, 2), None, None) is False
    assert full_sync_due(options, standard, berlin(DIENSTAG, 23, 30), None, None) is False
    assert full_sync_due(options, standard, berlin(MITTWOCH, 0), None, None) is True


def test_frist_ist_das_fruehere_ende() -> None:
    options = SourceFetchOptions.from_sync_config(KOELN)
    jetzt = berlin(DIENSTAG, 23, 30)
    assert options.fetch_deadline(jetzt) == berlin(MITTWOCH, 1)
    assert options.fetch_deadline(jetzt, berlin(MITTWOCH, 0, 30)) == berlin(MITTWOCH, 0, 30)
    assert options.fetch_deadline(jetzt, berlin(MITTWOCH, 2)) == berlin(MITTWOCH, 1)
    assert SourceFetchOptions().fetch_deadline(jetzt, berlin(MITTWOCH, 2)) == berlin(MITTWOCH, 2)


def test_vollabgleich_in_zeitscheiben_endet_mit_dem_abruffenster() -> None:
    options = SourceFetchOptions.from_sync_config({**KOELN, "full_sync_slices": {"days": 30, "since": "2010-01-01"}})
    # Auch ohne Standardfenster (Kommandozeile): das Abruffenster begrenzt ihn
    assert options.full_sync_plan(True, now=berlin(DIENSTAG, 23, 30)) == (True, berlin(MITTWOCH, 1))
    assert options.full_sync_plan(True, now=berlin(DIENSTAG, 14)) == (False, None)


# ---------------------------------------------------------------------------
# Takt: nächster Lauf
# ---------------------------------------------------------------------------


def test_naechster_lauf_ausserhalb_beginnt_mit_dem_fenster() -> None:
    fenster = koeln_fenster()
    takt = SyncSchedule()
    # 23:00 Uhr werktags: nachts alle 60 Minuten, 00:00 Uhr liegt im Fenster
    zustand_ = SyncState().after_run(takt, berlin(DIENSTAG, 23), success=True, changes=0, full=False, window=fenster)
    assert (zustand_.next_due, zustand_.reason) == (berlin(MITTWOCH, 0), None)
    # 00:30 Uhr: nach dem Takt 01:30 Uhr, außerhalb: Beginn des nächsten Fensters
    zustand_ = zustand_.after_run(takt, berlin(MITTWOCH, 0, 30), success=True, changes=0, full=False, window=fenster)
    assert (zustand_.next_due, zustand_.reason) == (berlin(MITTWOCH, 23), REASON_WINDOW)
    assert zustand_.interval_minutes == 60, "der Abstand bleibt der des Takts"
    # Auch nach einem gescheiterten Lauf und einem Vollabgleich
    gescheitert = SyncState().after_run(
        takt, berlin(MITTWOCH, 0, 45), success=False, changes=0, full=True, window=fenster
    )
    assert gescheitert.next_due == berlin(MITTWOCH, 23)
    # Der Grund übersteht das Speichern
    assert SyncState.from_sync_config({"sync_state": zustand_.to_dict()}) == zustand_


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


def _laeufe(orchestrator: Any, name: str) -> list[bool]:
    return [full for quelle_, full in orchestrator.laeufe if quelle_ == name]


async def test_daemon_startet_die_quelle_nur_im_fenster(monkeypatch: pytest.MonkeyPatch) -> None:
    fenster_metrik: list[tuple[str, bool | None]] = []
    monkeypatch.setattr(metrics, "record_source_window", lambda name, offen: fenster_metrik.append((name, offen)))
    uhr = Uhr(berlin(DIENSTAG, 10))
    koeln, andere = quelle("Koeln", sync_config=dict(KOELN)), quelle("Andere")
    scheduler, orchestrator, speicher = daemon(uhr, [koeln, andere])

    await durchlauf(scheduler)
    assert _laeufe(orchestrator, "Andere") == [False]
    assert _laeufe(orchestrator, "Koeln") == [], "auch der erste Lauf wartet auf das Fenster"
    faellig = dict(scheduler.get_status()["next_due"])
    assert faellig["Koeln"] == berlin(DIENSTAG, 23).isoformat(timespec="seconds")
    assert ("Koeln", False) in fenster_metrik and ("Andere", None) in fenster_metrik

    uhr.jetzt = berlin(DIENSTAG, 22, 59)
    await durchlauf(scheduler)
    assert _laeufe(orchestrator, "Koeln") == []

    # 23:00 Uhr: Das Fenster öffnet, der Vollabgleich beginnt in ihm (nicht erst im Standardfenster ab 01:00 Uhr)
    uhr.jetzt = berlin(DIENSTAG, 23)
    await durchlauf(scheduler)
    assert _laeufe(orchestrator, "Koeln") == [True]
    assert naechster(koeln) == berlin(MITTWOCH, 0), "nachts alle 60 Minuten, noch im Fenster"

    uhr.jetzt = berlin(MITTWOCH, 0)
    await durchlauf(scheduler)
    assert _laeufe(orchestrator, "Koeln") == [True, False]
    # 01:00 Uhr liegt nicht mehr im Fenster: nächster Lauf zu Beginn des nächsten Fensters, mit Grund
    assert naechster(koeln) == berlin(MITTWOCH, 23)
    assert SyncState.from_sync_config(koeln.sync_config).reason == REASON_WINDOW
    eintrag = [e for e in speicher.protokoll if e["source_id"] == koeln.id][-1]
    assert eintrag["details"]["takt"]["reason"] == REASON_WINDOW
    assert eintrag["details"]["takt"]["window"] == "23:00–01:00 Uhr (Europe/Berlin)"
    assert "reason" not in [e for e in speicher.protokoll if e["source_id"] == andere.id][-1]["details"]["takt"]

    # Bis zum nächsten Fenster kein Lauf, auch nicht im Standardfenster des Vollabgleichs ab 01:00 Uhr
    while uhr.jetzt < berlin(MITTWOCH, 22, 30):
        uhr.weiter(minutes=30)
        await durchlauf(scheduler)
    assert _laeufe(orchestrator, "Koeln") == [True, False]
    uhr.jetzt = berlin(MITTWOCH, 23)
    await durchlauf(scheduler)
    assert _laeufe(orchestrator, "Koeln") == [True, False, True], "nächstes Fenster, mit dem Vollabgleich der Nacht"


async def test_ausloeser_ausserhalb_des_fensters_verfaellt() -> None:
    uhr = Uhr(berlin(DIENSTAG, 10))
    koeln = quelle(
        "Koeln", sync_config={**KOELN, **zustand(berlin(MITTWOCH, 0, 30), full_attempted=berlin(DIENSTAG, 0))}
    )
    scheduler, orchestrator, _ = daemon(uhr, [koeln])

    scheduler.request_sync(source_id=str(koeln.id), full=True)
    await durchlauf(scheduler)
    scheduler.request_sync()
    await durchlauf(scheduler)
    assert orchestrator.laeufe == []
    assert scheduler.get_status()["pending_triggers"] == [], "Auslöser verfällt, wartet nicht auf das Fenster"

    # Im Fenster wirkt ein Auslöser wie sonst
    uhr.jetzt = berlin(DIENSTAG, 23, 30)
    koeln.sync_config = {**KOELN, **zustand(berlin(MITTWOCH, 0, 30), full_attempted=berlin(DIENSTAG, 23))}
    scheduler.request_sync(source_id=str(koeln.id))
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("Koeln", False)]


async def test_nach_der_schonung_wartet_die_quelle_auf_das_fenster() -> None:
    uhr = Uhr(berlin(DIENSTAG, 10))
    koeln = quelle("Koeln", sync_config=dict(KOELN), consecutive_failures=3, last_error_at=berlin(DIENSTAG, 9, 55))
    scheduler, orchestrator, _ = daemon(uhr, [koeln])

    await durchlauf(scheduler)
    uhr.jetzt = berlin(DIENSTAG, 10, 10)  # Schonung (10 Minuten) vorbei
    await durchlauf(scheduler)
    assert orchestrator.laeufe == []
    assert dict(scheduler.get_status()["next_due"])["Koeln"] == berlin(DIENSTAG, 23).isoformat(timespec="seconds")


async def test_daemon_mit_fenster_in_eigener_zeitzone() -> None:
    uhr = Uhr(datetime(2026, 10, 13, 21, 30, tzinfo=UTC))  # 23:30 Uhr in Berlin
    utc = quelle("Utc", sync_config={"sync_window": {"start": "22:00", "end": "23:00", "tz": "UTC"}})
    scheduler, orchestrator, _ = daemon(uhr, [utc])

    await durchlauf(scheduler)
    assert orchestrator.laeufe == []
    uhr.jetzt = datetime(2026, 10, 13, 22, tzinfo=UTC)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("Utc", True)]


async def test_daemon_vollabgleich_nur_im_teil_des_abruffensters() -> None:
    uhr = Uhr(berlin(DIENSTAG, 23))
    koeln = quelle("Koeln", sync_config={**KOELN, "full_sync_window": {"start": "00:00", "end": "06:00"}})
    scheduler, orchestrator, _ = daemon(uhr, [koeln])

    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("Koeln", False)], "23:00 Uhr: Änderungsabgleich"
    uhr.jetzt = berlin(MITTWOCH, 0)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("Koeln", False), ("Koeln", True)]


async def test_fenster_schliesst_zwischen_durchlauf_und_lauf() -> None:
    """Meldet der Abgleich „außerhalb“, obwohl der Durchlauf noch im Fenster war: kein Protokoll, nächstes Fenster."""
    uhr = Uhr(berlin(MITTWOCH, 0, 59))
    koeln = quelle(
        "Koeln", sync_config={**KOELN, **zustand(berlin(MITTWOCH, 0, 59), full_attempted=berlin(DIENSTAG, 23))}
    )
    scheduler, orchestrator, speicher = daemon(uhr, [koeln])

    async def schon_zu(url: str, full: bool = False, default_window: Any = None) -> SyncResult:
        orchestrator.laeufe.append(("Koeln", full))
        return SyncResult(source_url=url, source_name="Koeln", success=False, outside_window=True, errors=["zu"])

    orchestrator.sync_body_url = schon_zu  # type: ignore[method-assign]
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("Koeln", False)]
    assert speicher.protokoll == []
    assert naechster(koeln) == berlin(MITTWOCH, 23)
    await durchlauf(scheduler)
    assert len(orchestrator.laeufe) == 1


# ---------------------------------------------------------------------------
# Abgleich: kein Abruf außerhalb, Ende mit dem Fenster
# ---------------------------------------------------------------------------

BASE = "https://ris.example.org/oparl"
LISTE = f"{BASE}/bodies"
SOURCE_ID = UUID(int=1186)


def fenster_um_jetzt(offen: bool) -> dict[str, str]:
    """Abruffenster um die echte Uhrzeit (Ortszeit Berlin): offen eine Stunde davor bis eine danach, sonst später."""
    jetzt = datetime.now(BERLIN)
    start, ende = (
        (jetzt - timedelta(hours=1), jetzt + timedelta(hours=1))
        if offen
        else (
            jetzt + timedelta(hours=1),
            jetzt + timedelta(hours=2),
        )
    )
    return {"start": f"{start:%H:%M}", "end": f"{ende:%H:%M}"}


class _Speicher:
    """Was ``sync_body_url`` vom Speicher braucht; die Kommunen gleicht ``_sync_body`` (Attrappe) ab."""

    def __init__(self, sync_config: dict[str, Any]) -> None:
        self.id_bases = IdBases()
        self.sync_config = sync_config
        self.fehler: list[str] = []
        self.vollstaendig: list[bool] = []

    async def get_source_by_url(self, url: str) -> Any:
        return SimpleNamespace(
            url=url, name="Stadt Köln", sync_config=self.sync_config, user_agent=None, last_successful_full_sync=None
        )

    async def save_filter_notes(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def upsert_source(self, **kwargs: Any) -> UUID:
        return SOURCE_ID

    async def update_source_sync_time(
        self, source_id: UUID, full_sync: bool = False, complete: bool = False, full_complete: bool | None = None
    ) -> None:
        self.vollstaendig.append(complete)

    async def record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        self.fehler.append(error)


def _orchestrator(speicher: _Speicher) -> SyncOrchestrator:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = speicher  # type: ignore[assignment]
    orchestrator.processor = OParlProcessor()
    orchestrator.max_concurrent = 2
    orchestrator._parallel_mode = False
    return orchestrator


class _KeinAbruf:
    """Schon das Anlegen des Clients wäre ein Abruf außerhalb des Fensters."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("außerhalb des Abruffensters abgerufen")


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("art", [{}, {"source_type": "scraper:sessionnet"}])
async def test_ausserhalb_des_fensters_ruft_der_abgleich_nichts_ab(
    monkeypatch: pytest.MonkeyPatch, full: bool, art: dict[str, str]
) -> None:
    monkeypatch.setattr(orchestrator_module, "OParlClient", _KeinAbruf)
    monkeypatch.setattr(runner_module, "ScraperSyncRunner", _KeinAbruf)
    speicher = _Speicher({**art, "sync_window": fenster_um_jetzt(offen=False)})

    result = await _orchestrator(speicher).sync_body_url(LISTE, full=full)

    assert result.outside_window is True
    assert result.success is False and result.full is False
    assert result.source_name == "Stadt Köln"
    assert "außerhalb des Abruffensters" in result.errors[0] and "nächstes Fenster ab" in result.errors[0]
    assert speicher.fehler == [], "kein Fehlversuch, keine Schonung"


@pytest.mark.parametrize("full", [False, True])
async def test_im_fenster_endet_jeder_abruf_mit_dem_fenster(monkeypatch: pytest.MonkeyPatch, full: bool) -> None:
    def ris(request: httpx.Request) -> httpx.Response:
        kommune = {"id": f"{BASE}/body/1", "type": "https://schema.oparl.org/1.1/Body", "name": "Köln"}
        return httpx.Response(200, json={"data": [kommune], "links": {}})

    echter_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.client.oparl_client.httpx.AsyncClient",
        lambda *args, **kwargs: echter_client(transport=httpx.MockTransport(ris), **kwargs),
    )
    fenster = fenster_um_jetzt(offen=True)
    speicher = _Speicher({"sync_window": fenster})
    orchestrator = _orchestrator(speicher)
    fristen: list[tuple[bool, datetime | None]] = []

    async def sync_body(**kwargs: Any) -> dict[str, Any]:
        fristen.append((kwargs["full"], kwargs["client"].fetch_deadline))
        return {"incomplete": []}

    orchestrator._sync_body = sync_body  # type: ignore[method-assign]
    result = await orchestrator.sync_body_url(LISTE, full=full)

    parsed = FullSyncWindow.parse(fenster)
    assert parsed is not None
    # Änderungs- wie Vollabgleich (das Fenster des Vollabgleichs ist das Abruffenster) enden mit dem Fenster
    assert fristen == [(full, parsed.closes_at(datetime.now(UTC)))]
    assert result.success and not result.outside_window and result.full is full


async def test_liste_endet_am_fensterende_ohne_weitere_seite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Änderungsabgleich: Schließt das Fenster während einer Seite, geht die Folgeseite nicht mehr hinaus."""
    client = OParlClient(fetch_deadline=datetime.now(UTC) + timedelta(hours=1))
    angefragt: list[str] = []
    folgeseite = f"{BASE}/papers?page=2"

    async def fetch(url: str, **_kwargs: Any) -> FetchResult:
        angefragt.append(url)
        client.fetch_deadline = datetime.now(UTC) - timedelta(seconds=1)  # das Fenster schließt
        seite = {"data": [{"id": f"{BASE}/paper/1", "type": "https://schema.oparl.org/1.1/Paper"}]}
        return FetchResult(url=url, data={**seite, "links": {"next": folgeseite}}, status_code=200)

    monkeypatch.setattr(client, "fetch", fetch)
    seiten: list[list[dict[str, Any]]] = []
    with pytest.raises(FetchWindowClosedError) as fehler:
        async for seite in client.fetch_list(f"{BASE}/papers", modified_since=berlin(DIENSTAG, 22)):
            seiten.append(seite)
    assert len(angefragt) == 1 and len(seiten) == 1
    assert fehler.value.page_url == folgeseite
    # Die Prüfung des Filters nach dem Vollabgleich fragt ebenfalls nichts mehr an
    assert await client.probe_modified_since(f"{BASE}/papers", berlin(DIENSTAG, 22)) == MODIFIED_SINCE_INCONCLUSIVE
    assert len(angefragt) == 1


def test_sync_source_ausserhalb_des_fensters(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sync --source`` außerhalb des Fensters: Meldung, kein Protokoll, Rückgabewert 1."""
    protokolle: list[Any] = []

    class Quellen:
        async def get_source_by_url(self, url: str) -> SimpleNamespace:
            return SimpleNamespace(url=url)

    class Orchestrator:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.storage = Quellen()

        async def __aenter__(self) -> Orchestrator:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def sync_body_url(self, url: str, **kwargs: Any) -> SyncResult:
            meldung = "Stadt Köln: außerhalb des Abruffensters 23:00–01:00 Uhr (Europe/Berlin), nichts abgerufen"
            return SyncResult(source_url=url, source_name="", success=False, outside_window=True, errors=[meldung])

        def print_result(self, result: SyncResult) -> None:
            raise AssertionError("kein Lauf, kein Ergebnis")

        async def write_results_to_synclog(self, *args: Any, **kwargs: Any) -> None:
            protokolle.append(args)

    monkeypatch.setattr(cli, "SyncOrchestrator", Orchestrator)
    for argumente in (["--source", LISTE, "--full"], ["--body", LISTE]):
        result = CliRunner().invoke(cli.app, ["sync", *argumente])
        assert result.exit_code == 1, result.output
        assert "außerhalb des Abruffensters" in result.output
    assert protokolle == []


# ---------------------------------------------------------------------------
# Scraper und Texterkennung
# ---------------------------------------------------------------------------


async def test_politefetcher_fragt_nach_dem_fensterende_nichts_an() -> None:
    def keine_anfrage(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nach dem Fensterende angefragt")

    fetcher = PoliteFetcher(rate_limit_seconds=0, fetch_deadline=datetime.now(UTC) - timedelta(seconds=1))
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(keine_anfrage))
    with pytest.raises(CrawlWindowClosedError):
        await fetcher.fetch_text("https://ratsinfo.example/bi/si0040.asp")
    await fetcher._client.aclose()


async def test_scraper_crawl_endet_mit_dem_fenster(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schließt das Fenster nach der ersten Kalenderseite, holt der Crawl keine Seite mehr: kein Fehlversuch der
    Quelle, kein Abgleich, und die Monatsübersicht zählt nicht fürs Listen-Diffing (Detailseiten fehlen)."""
    seiten = SamtgemeindeFetcher()
    anfragen: list[str] = []
    abrufe: list[PoliteFetcher] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        anfragen.append(str(request.url))
        if request.url.path.endswith("/si0040.asp"):
            abrufe[0].fetch_deadline = datetime.now(UTC) - timedelta(seconds=1)
        text = await seiten.fetch_text(str(request.url))
        return httpx.Response(200, text=text) if text is not None else httpx.Response(404)

    class Abruf(PoliteFetcher):
        async def __aenter__(self) -> Abruf:
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
            abrufe.append(self)
            return self

    class Fenster(runner_module.CrawlWindow):
        @classmethod
        def from_days(cls, days_back: int, days_ahead: int, today: Any = None) -> runner_module.CrawlWindow:
            return SEPTEMBER

    monkeypatch.setattr(runner_module, "PoliteFetcher", Abruf)
    monkeypatch.setattr(runner_module, "CrawlWindow", Fenster)
    monkeypatch.setattr(settings, "text_extraction_enabled", False)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", False)
    alt = {"list_snapshots": {"p1:2026-09": "alt"}}
    quelle_ = FakeSource(alt)
    quelle_.sync_config["scraper"]["rate_limit_seconds"] = 0
    orchestrator = FakeOrchestrator()  # ein Fehlversuch der Quelle wäre ein AssertionError

    result = await ScraperSyncRunner(orchestrator, quelle_).run(  # type: ignore[arg-type]
        full=False, fetch_deadline=datetime.now(UTC) + timedelta(hours=1)
    )

    kalender = [url for url in anfragen if "/si0040.asp" in url]
    assert len(kalender) == 1 and anfragen[-1] == kalender[0], "nach dem Fensterende keine Anfrage mehr"
    assert result.success is False
    assert any("Abruffenster" in fehler for fehler in result.errors)
    assert orchestrator.storage.state["list_snapshots"] == alt["list_snapshots"]
    assert orchestrator.storage.source_syncs == []


class _Dateispeicher:
    def __init__(self, sync_config: dict[str, Any]) -> None:
        self.sync_config = sync_config
        self.beansprucht: list[Any] = []
        self.updates: list[dict[str, Any]] = []

    async def get_fetch_options_for_body(self, body_id: Any) -> SourceFetchOptions:
        return SourceFetchOptions.from_sync_config(self.sync_config)

    async def get_download_headers_for_body(self, body_id: Any) -> dict[str, str]:
        return {}

    async def file_downloads_enabled_for_body(self, body_id: Any) -> bool:
        return True

    async def get_pending_files(self, body_id: Any, **_kwargs: Any) -> list[Any]:
        self.beansprucht.append(body_id)
        return []

    async def update_file_text(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


async def test_texterkennung_laedt_nur_im_fenster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "text_extraction_runner", "ingestor")
    speicher = _Dateispeicher(dict(KOELN))
    kommune = uuid4()

    assert await TextExtractor(speicher, wall_clock=lambda: berlin(DIENSTAG, 14)).extract_pending_files(kommune) == 0
    assert speicher.beansprucht == [], "tagsüber nichts beanspruchen"
    await TextExtractor(speicher, wall_clock=lambda: berlin(DIENSTAG, 23, 30)).extract_pending_files(kommune)
    assert speicher.beansprucht and set(speicher.beansprucht) == {kommune}, "im Fenster wie sonst"


async def test_beanspruchte_datei_bleibt_nach_fensterende_wartend(monkeypatch: pytest.MonkeyPatch) -> None:
    speicher = _Dateispeicher(dict(KOELN))
    extractor = TextExtractor(speicher, wall_clock=lambda: berlin(MITTWOCH, 1))

    async def kein_download(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("nach dem Fensterende geladen")

    monkeypatch.setattr(extractor, "_download_to_file", kein_download)
    datei = SimpleNamespace(
        id=uuid4(), body_id=uuid4(), download_url=f"{BASE}/datei.pdf", access_url=None, mime_type="", file_name=""
    )
    assert await extractor._process_file(datei) is False
    assert speicher.updates == [{"file_id": datei.id, "status": "pending", "reset_attempts": False}]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Daemon mit Takt je Quelle (Issue #1026), mit fester Uhr, ohne Datenbank und ohne Netz.

Der Orchestrator ist nachgebildet: Er hält fest, welche Quelle wann wie abgeglichen wurde, und kann einen Lauf
festhalten, bis der Test ihn freigibt. Der Speicher hält ``sync_config`` wie die Datenbank.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from src.client.oparl_client import SyncStats
from src.scheduler import SyncScheduler
from src.scheduler.takt import SyncState
from src.sync.orchestrator import SyncResult

BERLIN = ZoneInfo("Europe/Berlin")
DIENSTAG = date(2026, 10, 13)
MITTWOCH = date(2026, 10, 14)
SAMSTAG = date(2026, 10, 17)


def berlin(tag: date, stunde: int, minute: int = 0) -> datetime:
    return datetime.combine(tag, time(stunde, minute), tzinfo=BERLIN).astimezone(UTC)


class Uhr:
    def __init__(self, jetzt: datetime) -> None:
        self.jetzt = jetzt

    def __call__(self) -> datetime:
        return self.jetzt

    def weiter(self, **dauer: float) -> None:
        self.jetzt += timedelta(**dauer)


def quelle(name: str, **felder: Any) -> SimpleNamespace:
    werte: dict[str, Any] = {
        "id": uuid4(),
        "name": name,
        "url": f"https://{name.lower()}.example.org/oparl/system",
        "sync_config": {},
        "is_active": True,
        "consecutive_failures": 0,
        "last_error_at": None,
        "last_error_kind": None,
        "last_successful_full_sync": None,
    }
    werte.update(felder)
    return SimpleNamespace(**werte)


def zustand(next_due: datetime | None = None, **felder: Any) -> dict[str, Any]:
    return {"sync_state": SyncState(next_due=next_due, **felder).to_dict()}


class Speicher:
    """Nachbildung des DatabaseStorage für den Scheduler."""

    def __init__(self, quellen: list[SimpleNamespace]) -> None:
        self.quellen = quellen
        self.protokoll: list[dict[str, Any]] = []
        self.sitzungen: dict[Any, list[datetime]] = {}
        self.geleert = 0

    async def get_all_sources(self) -> list[SimpleNamespace]:
        return [q for q in self.quellen if q.is_active]

    async def set_source_sync_config_value(self, source_id: Any, key: str, value: Any) -> None:
        for q in self.quellen:
            if q.id == source_id:
                q.sync_config = {**q.sync_config, key: value}

    async def write_sync_log(self, **felder: Any) -> None:
        self.protokoll.append(felder)

    async def get_meeting_starts(self, source_id: Any, start: datetime, end: datetime) -> list[datetime]:
        return [s for s in self.sitzungen.get(source_id, []) if start <= s < end]

    def clear_uuid_caches(self) -> None:
        self.geleert += 1


class Orchestrator:
    """Nachbildung des SyncOrchestrators: protokolliert Läufe, kann sie festhalten."""

    def __init__(self, speicher: Speicher) -> None:
        self.storage = speicher
        self.processor = SimpleNamespace(clear_id_cache=lambda: None)
        self._parallel_mode = False
        self.laeufe: list[tuple[str, bool]] = []
        self.sperren: dict[str, asyncio.Event] = {}
        self.aenderungen: dict[str, int] = {}
        self.fehler: set[str] = set()

    def name_von(self, url: str) -> str:
        return next(q.name for q in self.storage.quellen if q.url == url)

    def festhalten(self, name: str) -> asyncio.Event:
        self.sperren[name] = asyncio.Event()
        return self.sperren[name]

    async def sync_body_url(self, url: str, full: bool = False) -> SyncResult:
        name = self.name_von(url)
        self.laeufe.append((name, full))
        sperre = self.sperren.get(name)
        if sperre is not None:
            await sperre.wait()
        stats = SyncStats(http_requests=12)
        if name in self.fehler:
            return SyncResult(source_url=url, source_name=name, success=False, errors=["HTTP 503"], full=full)
        return SyncResult(
            source_url=url,
            source_name=name,
            success=True,
            papers_synced=self.aenderungen.get(name, 0),
            http_stats=stats,
            full=full,
        )

    def print_result(self, _result: SyncResult) -> None:
        return None


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(berlin(DIENSTAG, 10))


def daemon(
    uhr: Uhr, quellen: list[SimpleNamespace], *, pool: int = 4, full_sync_hour: int | None = 1
) -> tuple[SyncScheduler, Orchestrator, Speicher]:
    speicher = Speicher(quellen)
    orchestrator = Orchestrator(speicher)
    scheduler = SyncScheduler(
        sync_interval_minutes=10,
        full_sync_hour=full_sync_hour,
        pool_size=pool,
        clock=uhr,
        orchestrator=orchestrator,
        heartbeat=False,
    )
    return scheduler, orchestrator, speicher


async def durchlauf(scheduler: SyncScheduler) -> list[asyncio.Task[None]]:
    """Ein Durchlauf und warten, bis alle nicht festgehaltenen Läufe fertig sind."""
    gestartet = await scheduler.tick()
    for _ in range(20):
        await asyncio.sleep(0)
    return gestartet


def naechster(q: SimpleNamespace) -> datetime | None:
    return SyncState.from_sync_config(q.sync_config).next_due


async def test_vollabgleich_einer_quelle_verzoegert_andere_nicht(uhr: Uhr) -> None:
    uhr.jetzt = berlin(DIENSTAG, 1, 0)
    a = quelle("A")
    # B hatte seinen Vollabgleich in dieser Nacht schon; ihr Änderungsabgleich ist fällig
    b = quelle("B", sync_config=zustand(uhr.jetzt, full_attempted=berlin(DIENSTAG, 1, 0)))
    scheduler, orchestrator, speicher = daemon(uhr, [a, b])
    sperre = orchestrator.festhalten("A")

    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", True), ("B", False)]
    assert naechster(b) == berlin(DIENSTAG, 2, 0), "nachts alle 60 Minuten"

    uhr.jetzt = berlin(DIENSTAG, 2, 0)
    await durchlauf(scheduler)
    uhr.jetzt = berlin(DIENSTAG, 3, 0)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", True), ("B", False), ("B", False), ("B", False)]
    assert "A" in scheduler.get_status()["running"], "der Vollabgleich läuft noch"

    sperre.set()
    await durchlauf(scheduler)
    assert [(e["sync_type"], e["triggered_by"]) for e in speicher.protokoll if e["source_id"] == a.id] == [
        ("full", "daemon")
    ]
    assert len([e for e in speicher.protokoll if e["source_id"] == b.id]) == 3, "ein Protokoll je Lauf und Quelle"
    await durchlauf(scheduler)
    assert orchestrator.laeufe.count(("A", True)) == 1, "nur einmal je Fenster"


async def test_vollabgleiche_lassen_einen_platz_frei(uhr: Uhr) -> None:
    uhr.jetzt = berlin(DIENSTAG, 1, 0)
    a, c = quelle("A"), quelle("C")
    scheduler, orchestrator, _ = daemon(uhr, [a, c], pool=2)
    sperre = orchestrator.festhalten("A")

    await durchlauf(scheduler)
    # Ein Platz für Vollabgleiche: C gleicht inzwischen nur Änderungen ab
    assert orchestrator.laeufe == [("A", True), ("C", False)]
    await durchlauf(scheduler)
    assert len(orchestrator.laeufe) == 2, "C wartet auf einen freien Platz für den Vollabgleich"

    sperre.set()
    await durchlauf(scheduler)
    await durchlauf(scheduler)
    assert orchestrator.laeufe[2:] == [("C", True)]


async def test_vollabgleich_faellig_waehrend_aenderungsabgleich_folgt_danach(uhr: Uhr) -> None:
    uhr.jetzt = berlin(DIENSTAG, 0, 59)
    a = quelle("A")
    scheduler, orchestrator, _ = daemon(uhr, [a])
    sperre = orchestrator.festhalten("A")

    await durchlauf(scheduler)
    uhr.jetzt = berlin(DIENSTAG, 1, 0)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False)], "höchstens ein Lauf je Quelle"

    orchestrator.sperren.pop("A")
    sperre.set()
    uhr.jetzt = berlin(DIENSTAG, 1, 5)
    await durchlauf(scheduler)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False), ("A", True)]


async def test_admin_ausloeser_fuer_eine_quelle_und_fuer_alle(uhr: Uhr) -> None:
    spaeter = uhr.jetzt + timedelta(minutes=8)
    a = quelle("A", sync_config=zustand(spaeter))
    b = quelle("B", sync_config=zustand(spaeter))
    scheduler, orchestrator, speicher = daemon(uhr, [a, b])

    await durchlauf(scheduler)
    assert orchestrator.laeufe == [], "noch nicht fällig"

    scheduler._handle_trigger(json.dumps({"full": False, "source_id": str(a.id), "request_id": "req-1"}))
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False)]
    assert speicher.protokoll[-1]["triggered_by"] == "admin"
    assert speicher.protokoll[-1]["source_id"] == a.id

    scheduler.request_sync()
    await durchlauf(scheduler)
    assert sorted(orchestrator.laeufe[1:]) == [("A", False), ("B", False)]

    scheduler._handle_trigger(json.dumps({"full": True, "source_url": b.url + "/"}))
    await durchlauf(scheduler)
    assert orchestrator.laeufe[-1] == ("B", True)

    scheduler.request_sync(source_id=str(uuid4()))
    scheduler._handle_trigger("kein json")
    await durchlauf(scheduler)
    assert len(orchestrator.laeufe) == 4 and scheduler.get_status()["pending_triggers"] == []


async def test_ausloeser_waehrend_eines_laufs_folgt_danach(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, _ = daemon(uhr, [a])
    sperre = orchestrator.festhalten("A")
    await durchlauf(scheduler)

    scheduler.request_sync(source_id=str(a.id))
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False)]

    orchestrator.sperren.pop("A")
    sperre.set()
    await durchlauf(scheduler)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False), ("A", False)]


async def test_schonung_hat_vorrang_vor_takt_und_ausloeser(uhr: Uhr) -> None:
    gesperrt = quelle(
        "Gesperrt",
        consecutive_failures=3,
        last_error_at=uhr.jetzt - timedelta(minutes=1),
        last_error_kind="ua_blocked",
    )
    scheduler, orchestrator, _ = daemon(uhr, [gesperrt])

    scheduler.request_sync(source_id=str(gesperrt.id), full=True)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == []
    assert scheduler.get_status()["pending_triggers"] == [], "Auslöser entfällt in der Schonung"

    uhr.weiter(minutes=61)  # User-Agent-Sperre: mindestens 60 Minuten
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("Gesperrt", False)]


async def test_sitzungstag_kurzer_takt(uhr: Uhr) -> None:
    mit, ohne = quelle("Rat"), quelle("Kreis")
    scheduler, _, speicher = daemon(uhr, [mit, ohne])
    speicher.sitzungen[mit.id] = [berlin(MITTWOCH, 17)]

    await durchlauf(scheduler)
    assert naechster(mit) == uhr.jetzt + timedelta(minutes=5), "Vortag der Sitzung"
    assert naechster(ohne) == uhr.jetzt + timedelta(minutes=10)
    eintrag = next(e for e in speicher.protokoll if e["source_id"] == mit.id)
    assert eintrag["details"]["takt"]["period"] == "sitzungstag"
    assert eintrag["details"]["requests"] == 12


async def test_ruhe_verlaengert_den_abstand_aenderung_setzt_zurueck(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, _ = daemon(uhr, [a])

    abstaende = []
    for _ in range(4):
        await durchlauf(scheduler)
        due = naechster(a)
        assert due is not None
        abstaende.append(int((due - uhr.jetzt).total_seconds() // 60))
        uhr.jetzt = due
    assert abstaende == [10, 10, 20, 20]

    orchestrator.aenderungen["A"] = 3
    await durchlauf(scheduler)
    assert naechster(a) == uhr.jetzt + timedelta(minutes=10)
    assert SyncState.from_sync_config(a.sync_config).idle_runs == 0


async def test_gescheiterter_lauf_protokolliert_und_bleibt_im_takt(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, speicher = daemon(uhr, [a])
    orchestrator.fehler.add("A")

    await durchlauf(scheduler)
    assert speicher.protokoll[0]["status"] == "failed"
    assert speicher.protokoll[0]["errors"] == ["HTTP 503"]
    assert naechster(a) == uhr.jetzt + timedelta(minutes=10)


async def test_zustand_ueberlebt_neustart(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, speicher = daemon(uhr, [a])
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False)]

    neu, neu_orchestrator, _ = daemon(uhr, speicher.quellen)
    uhr.weiter(minutes=3)
    await durchlauf(neu)
    assert neu_orchestrator.laeufe == [], "nicht sofort nach dem Neustart, erst wenn fällig"
    uhr.weiter(minutes=7)
    await durchlauf(neu)
    assert neu_orchestrator.laeufe == [("A", False)]


async def test_pool_begrenzt_gleichzeitige_laeufe(uhr: Uhr) -> None:
    quellen = [quelle(f"Q{i}") for i in range(6)]
    scheduler, orchestrator, _ = daemon(uhr, quellen, pool=4)
    for q in quellen:
        orchestrator.festhalten(q.name)

    gestartet = await durchlauf(scheduler)
    assert len(gestartet) == 4 and len(orchestrator.laeufe) == 4
    for sperre in orchestrator.sperren.values():
        sperre.set()
    await durchlauf(scheduler)
    await durchlauf(scheduler)
    assert sorted(name for name, _ in orchestrator.laeufe) == sorted(q.name for q in quellen)


async def test_geplanter_vollabgleich_abgeschaltet(uhr: Uhr) -> None:
    uhr.jetzt = berlin(DIENSTAG, 1, 0)
    a = quelle("A")
    scheduler, orchestrator, _ = daemon(uhr, [a], full_sync_hour=99)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False)]


async def test_samstag_mit_fester_uhr_zwoelf_laeufe(uhr: Uhr) -> None:
    """Ohne Änderung am Wochenende alle 120 Minuten: 12 Läufe statt 144 im bisherigen 10-Minuten-Takt."""
    uhr.jetzt = berlin(SAMSTAG, 0, 0)
    # Vollabgleich dieser Nacht schon erledigt (Fenster ab 01:00 Uhr)
    a = quelle("A", sync_config=zustand(uhr.jetzt, full_attempted=berlin(SAMSTAG, 1, 30)))
    scheduler, orchestrator, _ = daemon(uhr, [a])
    ende = uhr.jetzt + timedelta(days=1)
    while uhr.jetzt < ende:
        await durchlauf(scheduler)
        uhr.weiter(seconds=30)
    assert len(orchestrator.laeufe) == 12
    assert all(not full for _, full in orchestrator.laeufe)


async def test_sitzungstag_bleibt_ohne_aenderung_im_kurzen_takt(uhr: Uhr) -> None:
    """Vortag einer Sitzung, keine Änderung: bis 22 Uhr höchstens 10 Minuten zwischen zwei Läufen."""
    rat = quelle("Rat")
    scheduler, orchestrator, speicher = daemon(uhr, [rat])
    speicher.sitzungen[rat.id] = [berlin(MITTWOCH, 17)]

    while uhr.jetzt < berlin(DIENSTAG, 22):
        await durchlauf(scheduler)
        due = naechster(rat)
        assert due is not None
        uhr.jetzt = due
    beginne = [e["started_at"] for e in speicher.protokoll]
    abstaende = {b - a for a, b in zip(beginne, beginne[1:], strict=False)}
    assert max(abstaende) == timedelta(minutes=10)
    assert len(orchestrator.laeufe) == 2 + 71, "10:00 und 10:05 Uhr, dann alle 10 Minuten von 10:10 bis 21:50 Uhr"


async def test_vollabgleiche_gerecht_ueber_zwei_naechte(uhr: Uhr) -> None:
    """Belegen lange Vollabgleiche alle Plätze bis zum Fensterende, kommen in der nächsten Nacht die übrigen zuerst."""
    uhr.jetzt = berlin(DIENSTAG, 1, 0)
    quellen = [quelle(name) for name in "ABCDE"]
    scheduler, orchestrator, _ = daemon(uhr, quellen, pool=4)
    lang = [orchestrator.festhalten(name) for name in "ABC"]

    while uhr.jetzt < berlin(DIENSTAG, 6, 30):
        await durchlauf(scheduler)
        uhr.weiter(minutes=5)
    assert [name for name, full in orchestrator.laeufe if full] == ["A", "B", "C"]
    assert {name for name, _ in orchestrator.laeufe} == set("ABCDE"), "D und E gleichen inzwischen Änderungen ab"
    orchestrator.sperren.clear()
    for sperre in lang:
        sperre.set()
    await durchlauf(scheduler)
    assert scheduler.get_status()["running"] == []

    uhr.jetzt = berlin(MITTWOCH, 1, 0)
    orchestrator.laeufe.clear()
    for name in "ABCDE":
        orchestrator.festhalten(name)
    await durchlauf(scheduler)
    # Drei Plätze für Vollabgleiche: D und E (noch keiner) und der früheste fällige der übrigen; B gleicht auf dem
    # letzten Platz Änderungen ab
    assert sorted(orchestrator.laeufe) == [("A", True), ("B", False), ("D", True), ("E", True)]


async def test_angeforderter_vollabgleich_vor_geplanten(uhr: Uhr) -> None:
    uhr.jetzt = berlin(DIENSTAG, 1, 0)
    a, b, c = quelle("A"), quelle("B"), quelle("C")
    scheduler, orchestrator, _ = daemon(uhr, [a, b, c], pool=2)
    for q in (a, b, c):
        orchestrator.festhalten(q.name)
    scheduler.request_sync(full=True, source_id=str(c.id))
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("C", True), ("A", False)]


async def test_takt_bleibt_wenn_der_zustand_nicht_gespeichert_wird(uhr: Uhr) -> None:
    """Scheitert das Speichern, beginnt die Quelle nicht sofort wieder, sondern nach ihrem Abstand."""
    a = quelle("A")
    scheduler, orchestrator, speicher = daemon(uhr, [a])
    original = speicher.set_source_sync_config_value

    async def kaputt(*_args: Any) -> None:
        raise TimeoutError("Zeilensperre")

    speicher.set_source_sync_config_value = kaputt  # type: ignore[method-assign]
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False)]
    assert naechster(a) is None, "nichts gespeichert"
    for _ in range(3):
        await durchlauf(scheduler)
    assert len(orchestrator.laeufe) == 1, "kein Lauf ohne Pause"

    uhr.weiter(minutes=10)
    await durchlauf(scheduler)
    assert len(orchestrator.laeufe) == 2
    assert SyncState.from_sync_config(a.sync_config) == SyncState()

    speicher.set_source_sync_config_value = original  # type: ignore[method-assign]
    uhr.weiter(minutes=10)
    await durchlauf(scheduler)
    assert len(orchestrator.laeufe) == 3
    gespeichert = SyncState.from_sync_config(a.sync_config)
    assert gespeichert.idle_runs == 3, "der Zähler lief im Speicher weiter"
    assert gespeichert.next_due == uhr.jetzt + timedelta(minutes=20)
    assert scheduler._unsaved == {}


class _PubSub:
    def __init__(self, protokoll: list[str]) -> None:
        self.protokoll = protokoll

    async def subscribe(self, _kanal: str) -> None:
        raise ConnectionError("Redis nicht erreichbar")

    async def aclose(self) -> None:
        self.protokoll.append("pubsub zu")


class _Client:
    def __init__(self, protokoll: list[str]) -> None:
        self.protokoll = protokoll
        protokoll.append("client auf")

    def pubsub(self) -> _PubSub:
        return _PubSub(self.protokoll)

    async def aclose(self) -> None:
        self.protokoll.append("client zu")


async def test_ausloeser_empfang_schliesst_verbindungen_vor_neuem_versuch(
    uhr: Uhr, monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis.asyncio as aioredis

    import src.scheduler as scheduler_modul

    protokoll: list[str] = []
    monkeypatch.setattr(aioredis, "from_url", lambda *_args, **_kw: _Client(protokoll))
    monkeypatch.setattr(scheduler_modul, "TRIGGER_RETRY_SECONDS", 0)
    scheduler, _, _ = daemon(uhr, [])

    empfang = asyncio.create_task(scheduler._listen_for_triggers())
    for _ in range(30):
        await asyncio.sleep(0)
    empfang.cancel()
    with pytest.raises(asyncio.CancelledError):
        await empfang

    versuche = protokoll.count("client auf")
    assert versuche >= 3
    assert protokoll.count("client zu") == versuche
    assert protokoll.count("pubsub zu") == versuche

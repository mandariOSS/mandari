# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kein Lauf hält seinen Platz auf Dauer, und kein Lauf bleibt ohne Folge.

- Gesamtfrist je Lauf: Danach endet er, zählt als Fehlversuch der Quelle und gibt seinen Platz zurück.
- Quellen eines Servers belegen höchstens den halben Pool; der Lauf einer abgeschalteten Quelle endet.
- Der Beginn jedes Laufs steht im Zustand. Endete der Daemon mitten im Lauf, laufen die Beteiligten danach zuletzt
  und einzeln; endet ein Einzellauf wieder mit dem Daemon, zählt das als Fehlversuch dieser Quelle.
- Namen fremder Quellen werden nie als Markup ausgewertet, und keine Ausgabe beendet einen Lauf oder den Daemon.

Mit fester Uhr, ohne Datenbank und ohne Netz; die Nachbildungen stammen aus ``test_scheduler_takt``.
"""

from __future__ import annotations

import asyncio
import io
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from src import scheduler as takt_modul
from src.redaction import MaskingConsole
from src.scheduler import ABORTED_RUN_ERROR, DEADLINE_ERROR, SUSPECT_MAX_WAIT, SyncScheduler, print_overview
from src.scheduler.takt import SyncState
from src.sync.orchestrator import BACKOFF_AFTER_FAILURES, end_with_current_task
from tests.test_scheduler_takt import (
    DIENSTAG,
    Orchestrator,
    Speicher,
    Uhr,
    berlin,
    durchlauf,
    naechster,
    quelle,
    zustand,
)


@pytest.fixture
def uhr() -> Uhr:
    return Uhr(berlin(DIENSTAG, 10))


class SpeicherMitFehlversuchen(Speicher):
    """Hält zusätzlich Fehlversuche fest, wie ``DatabaseStorage.record_source_failure``."""

    def __init__(self, quellen: list[SimpleNamespace], uhr: Uhr) -> None:
        super().__init__(quellen)
        self.uhr = uhr
        self.fehlversuche: list[tuple[str, str]] = []

    async def record_source_failure(self, url: str, error: str, error_kind: str | None = None) -> None:
        for q in self.quellen:
            if q.url == url:
                self.fehlversuche.append((q.name, error))
                q.consecutive_failures += 1
                q.last_error_at = self.uhr.jetzt
                q.last_error_kind = error_kind


def daemon(
    uhr: Uhr, quellen: list[SimpleNamespace], *, pool: int = 4, speicher: SpeicherMitFehlversuchen | None = None
) -> tuple[SyncScheduler, Orchestrator, SpeicherMitFehlversuchen]:
    """Ein Daemon wie nach dem Start des Prozesses; mit ``speicher`` über dem Bestand eines früheren."""
    speicher = speicher or SpeicherMitFehlversuchen(quellen, uhr)
    orchestrator = Orchestrator(speicher)
    scheduler = SyncScheduler(
        sync_interval_minutes=10,
        full_sync_hour=None,
        pool_size=pool,
        clock=uhr,
        orchestrator=orchestrator,
        heartbeat=False,
    )
    return scheduler, orchestrator, speicher


def stand(q: SimpleNamespace) -> SyncState:
    return SyncState.from_sync_config(q.sync_config)


async def hart_beenden(scheduler: SyncScheduler, quellen: list[SimpleNamespace]) -> None:
    """
    Der Prozess endet ohne Aufräumen (Speicherlimit): Was in der Datenbank steht, bleibt, wie es beim Ende war.

    Die Läufe der Nachbildung müssen trotzdem enden; ihr geordnetes Ende schriebe den Zustand fort. Deshalb den
    Stand vorher festhalten und danach wiederherstellen.
    """
    vorher = [(q, dict(q.sync_config)) for q in quellen]
    for task in list(scheduler._tasks.values()):
        task.cancel()
    await asyncio.gather(*scheduler._tasks.values(), return_exceptions=True)
    for q, sync_config in vorher:
        q.sync_config = sync_config


# --- Gesamtfrist je Lauf -----------------------------------------------------------------------------------


async def test_lauf_endet_mit_der_gesamtfrist_und_gibt_den_platz_frei(uhr: Uhr) -> None:
    quellen = [quelle(name) for name in "ABCDE"]
    scheduler, orchestrator, speicher = daemon(uhr, quellen)
    scheduler.run_deadline = 0.05
    for name in "ABCD":
        orchestrator.festhalten(name)

    await durchlauf(scheduler)
    assert sorted(scheduler.get_status()["running"]) == list("ABCD"), "der Pool ist voll, E wartet"

    await asyncio.sleep(0.2)
    await durchlauf(scheduler)

    assert ("E", False) in orchestrator.laeufe, "nach der Frist sind die Plätze frei"
    assert sorted(speicher.fehlversuche) == [(name, f"{DEADLINE_ERROR} (0 min)") for name in "ABCD"]
    gescheitert = [e for e in speicher.protokoll if e["status"] == "failed"]
    assert len(gescheitert) == 4 and all(DEADLINE_ERROR in e["errors"][0] for e in gescheitert)
    for q in quellen[:4]:
        assert stand(q).run_started is None, "der Lauf ist beendet, nicht abgebrochen"
        assert naechster(q) == uhr.jetzt + timedelta(minutes=10)


async def test_nach_der_frist_ist_die_quelle_erst_nach_derselben_dauer_wieder_faellig(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, _ = daemon(uhr, [a])
    scheduler.run_deadline = 3 * 3600.0
    orchestrator.festhalten("A")
    echte_frist = asyncio.timeout
    # Die drei Stunden im Zeitraffer: Die Frist läuft sofort ab, gerechnet wird mit ihrer Dauer
    with pytest.MonkeyPatch.context() as zeitraffer:
        zeitraffer.setattr(takt_modul.asyncio, "timeout", lambda _dauer: echte_frist(0.02))
        await durchlauf(scheduler)
        await asyncio.sleep(0.1)

    assert naechster(a) == uhr.jetzt + timedelta(hours=3), "länger als der Takt von zehn Minuten"
    uhr.weiter(minutes=30)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False)], "kein neuer Lauf im gewöhnlichen Takt"


async def test_ohne_frist_laeuft_der_lauf_weiter(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, speicher = daemon(uhr, [a])
    scheduler.run_deadline = None
    sperre = orchestrator.festhalten("A")

    await durchlauf(scheduler)
    await asyncio.sleep(0.1)
    assert scheduler.get_status()["running"] == ["A"] and speicher.fehlversuche == []
    sperre.set()
    await durchlauf(scheduler)
    assert [e["status"] for e in speicher.protokoll] == ["success"]


async def test_vollabgleich_hat_seine_eigene_frist(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, speicher = daemon(uhr, [a])
    scheduler.run_deadline, scheduler.full_deadline = 0.05, None
    sperre = orchestrator.festhalten("A")
    scheduler.request_sync(full=True, source_id=str(a.id))

    await durchlauf(scheduler)
    await asyncio.sleep(0.2)

    assert orchestrator.laeufe == [("A", True)] and speicher.fehlversuche == []
    sperre.set()
    await durchlauf(scheduler)


async def test_texterkennung_endet_mit_dem_abgebrochenen_abgleich() -> None:
    gestartet = asyncio.Event()
    nebenher: list[asyncio.Task[Any]] = []

    async def texterkennung() -> None:
        await asyncio.Event().wait()

    async def abgleich() -> None:
        nebenher.append(asyncio.create_task(texterkennung()))
        end_with_current_task(nebenher[0])
        gestartet.set()
        await asyncio.Event().wait()

    lauf = asyncio.create_task(abgleich())
    await gestartet.wait()
    lauf.cancel()
    await asyncio.gather(lauf, return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert nebenher[0].cancelled(), "ohne Lauf liefe die Texterkennung sonst weiter"


# --- Pool: Server und abgeschaltete Quellen ----------------------------------------------------------------


async def test_quellen_eines_servers_belegen_nur_den_halben_pool(uhr: Uhr) -> None:
    sammel = [quelle(name, url=f"https://sammel.example.org/{name.lower()}/oparl/system") for name in "ABCD"]
    andere = quelle("E")
    scheduler, orchestrator, _ = daemon(uhr, [*sammel, andere])
    sperren = {name: orchestrator.festhalten(name) for name in "ABCDE"}

    await durchlauf(scheduler)
    laufend = scheduler.get_status()["running"]
    assert len([name for name in laufend if name in "ABCD"]) == 2, "zwei von vier Plätzen für den Sammelserver"
    assert "E" in laufend, "eine Quelle auf einem anderen Server startet daneben"

    erster = next(name for name in laufend if name in "ABCD")
    sperren[erster].set()
    await durchlauf(scheduler)
    await durchlauf(scheduler)
    assert len([name for name in scheduler.get_status()["running"] if name in "ABCD"]) == 2, "die nächste rückt nach"
    for sperre in sperren.values():
        sperre.set()
    await durchlauf(scheduler)


async def test_abgeschaltete_quelle_beendet_ihren_lauf(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, speicher = daemon(uhr, [a])
    orchestrator.festhalten("A")

    await durchlauf(scheduler)
    assert scheduler.get_status()["running"] == ["A"]
    a.is_active = False
    await durchlauf(scheduler)

    assert scheduler.get_status()["running"] == []
    assert stand(a).run_started is None, "geordnet beendet"
    assert speicher.fehlversuche == [] and speicher.protokoll == []


# --- Abgebrochene Läufe ------------------------------------------------------------------------------------


async def test_beginn_steht_vor_dem_lauf_und_entfaellt_mit_seinem_ende(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, _ = daemon(uhr, [a])
    sperre = orchestrator.festhalten("A")

    await durchlauf(scheduler)
    assert stand(a).run_started == uhr.jetzt, "auch beim Änderungsabgleich, noch vor der ersten Anfrage"

    sperre.set()
    await durchlauf(scheduler)
    assert (stand(a).run_started, stand(a).suspect) == (None, False)


async def test_nach_hartem_ende_laufen_die_beteiligten_zuletzt_und_einzeln(uhr: Uhr) -> None:
    a, b, c = quelle("A"), quelle("B"), quelle("C", sync_config=zustand(uhr.jetzt + timedelta(minutes=5)))
    scheduler, orchestrator, speicher = daemon(uhr, [a, b, c])
    orchestrator.festhalten("A")
    orchestrator.festhalten("B")
    await durchlauf(scheduler)
    await hart_beenden(scheduler, [a, b])
    assert stand(a).run_started is not None and stand(b).run_started is not None

    # Neustart: C ist inzwischen fällig und kommt zuerst, A und B gelten als beteiligt, noch ohne Fehlversuch
    uhr.weiter(minutes=5)
    scheduler, orchestrator, speicher = daemon(uhr, [a, b, c], speicher=speicher)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("C", False)]
    assert (stand(a).suspect, stand(b).suspect, stand(a).run_started) == (True, True, None)
    assert speicher.fehlversuche == []

    # Der Pool ist leer und nichts anderes fällig: eine der beiden läuft, allein
    sperre = orchestrator.festhalten("A")
    orchestrator.festhalten("B")
    await durchlauf(scheduler)
    einzeln = scheduler.get_status()["running"]
    assert len(einzeln) == 1 and einzeln[0] in "AB"
    uhr.weiter(minutes=15)
    await durchlauf(scheduler)
    assert scheduler.get_status()["running"] == einzeln, "solange sie läuft, startet nichts daneben, auch C nicht"

    # Ihr Lauf endet gewöhnlich: Der Verdacht entfällt, die anderen sind wieder an der Reihe
    for name in "AB":
        orchestrator.sperren[name].set()
    sperre.set()
    await durchlauf(scheduler)
    beendet = a if einzeln == ["A"] else b
    assert (stand(beendet).suspect, stand(beendet).run_started) == (False, None)
    await durchlauf(scheduler)
    assert ("C", False) in orchestrator.laeufe[1:]
    assert speicher.fehlversuche == []


async def test_einzellauf_der_wieder_mit_dem_daemon_endet_fuehrt_zur_schonung(uhr: Uhr) -> None:
    feind, andere = quelle("Feind"), quelle("Andere")
    speicher: SpeicherMitFehlversuchen | None = None
    laeufe_der_anderen = 0
    # Erster Start: beide laufen, der Prozess endet
    scheduler, orchestrator, speicher = daemon(uhr, [feind, andere])
    orchestrator.festhalten("Feind")
    await durchlauf(scheduler)
    await hart_beenden(scheduler, [feind])
    assert naechster(andere) is not None, "die andere Quelle war fertig, bevor der Prozess endete"

    for neustart in range(1, BACKOFF_AFTER_FAILURES + 2):
        uhr.weiter(minutes=11)
        scheduler, orchestrator, speicher = daemon(uhr, [feind, andere], speicher=speicher)
        orchestrator.festhalten("Feind")
        orchestrator.aenderungen["Andere"] = 1  # mit Änderungen bleibt ihr Takt bei zehn Minuten
        await durchlauf(scheduler)
        laeufe_der_anderen += orchestrator.laeufe.count(("Andere", False))
        assert len(speicher.fehlversuche) == neustart - 1, "gezählt wird erst das Ende eines Einzellaufs"
        await durchlauf(scheduler)
        if neustart <= BACKOFF_AFTER_FAILURES:
            assert scheduler.get_status()["running"] == ["Feind"], "allein, nach der anderen Quelle"
            await hart_beenden(scheduler, [feind])
        else:
            assert scheduler.get_status()["running"] == [], "in Schonung startet die Quelle nicht mehr"

    assert speicher.fehlversuche == [("Feind", ABORTED_RUN_ERROR)] * BACKOFF_AFTER_FAILURES
    assert feind.consecutive_failures == BACKOFF_AFTER_FAILURES
    assert laeufe_der_anderen == BACKOFF_AFTER_FAILURES + 1, "die andere Quelle kam bei jedem Start zum Abschluss"
    assert andere.consecutive_failures == 0


async def test_geordnetes_ende_zaehlt_nicht_als_abbruch(uhr: Uhr) -> None:
    a = quelle("A")
    scheduler, orchestrator, speicher = daemon(uhr, [a])
    orchestrator.festhalten("A")
    await durchlauf(scheduler)
    assert stand(a).run_started is not None

    await scheduler.stop()

    assert stand(a).run_started is None
    scheduler, orchestrator, speicher = daemon(uhr, [a], speicher=speicher)
    await durchlauf(scheduler)
    assert orchestrator.laeufe == [("A", False)] and not stand(a).suspect
    assert speicher.fehlversuche == []


async def test_wartet_eine_beteiligte_quelle_zu_lange_startet_nichts_neues(uhr: Uhr) -> None:
    beteiligt = quelle("Beteiligt", sync_config=zustand(uhr.jetzt, suspect=True))
    lang, kurz = quelle("Lang"), quelle("Kurz")
    scheduler, orchestrator, _ = daemon(uhr, [beteiligt, lang, kurz])
    sperre = orchestrator.festhalten("Lang")

    await durchlauf(scheduler)
    assert orchestrator.laeufe.count(("Kurz", False)) == 1
    assert scheduler.get_status()["running"] == ["Lang"], "der Pool wird nicht leer, die beteiligte Quelle wartet"

    uhr.weiter(minutes=11)
    await durchlauf(scheduler)
    assert orchestrator.laeufe.count(("Kurz", False)) == 2, "innerhalb der Wartezeit laufen die anderen weiter"

    uhr.jetzt += SUSPECT_MAX_WAIT
    await durchlauf(scheduler)
    assert orchestrator.laeufe.count(("Kurz", False)) == 2, "jetzt startet nichts Neues mehr"
    sperre.set()
    await durchlauf(scheduler)
    await durchlauf(scheduler)
    assert ("Beteiligt", False) in orchestrator.laeufe
    assert not stand(beteiligt).suspect


# --- Fremde Namen in der Ausgabe ---------------------------------------------------------------------------

MARKUP_NAME = "x[/green][green]"


def test_konsole_gibt_unzulaessiges_markup_unveraendert_aus() -> None:
    ausgabe = io.StringIO()
    konsole = MaskingConsole(file=ausgabe, width=200)

    # Der Name schließt eine Auszeichnung, die nicht offen ist: Die Auswertung scheitert
    konsole.print(f"[blue]{MARKUP_NAME}: Änderungsabgleich beginnt[/blue]")
    konsole.print("[green]danach wie gewohnt[/green]")

    assert ausgabe.getvalue().splitlines() == [
        f"[blue]{MARKUP_NAME}: Änderungsabgleich beginnt[/blue]",
        "danach wie gewohnt",
    ]


async def test_quellenname_mit_markup_laeuft_normal_und_wartet_auf_den_takt(
    uhr: Uhr, monkeypatch: pytest.MonkeyPatch
) -> None:
    ausgabe = io.StringIO()
    monkeypatch.setattr(takt_modul, "console", MaskingConsole(file=ausgabe, width=300))
    a = quelle(MARKUP_NAME, url="https://markup.example.org/oparl/system")
    scheduler, orchestrator, speicher = daemon(uhr, [a])

    for _ in range(3):
        await durchlauf(scheduler)

    assert orchestrator.laeufe == [(MARKUP_NAME, False)], "ein Lauf, danach erst wieder zur nächsten Fälligkeit"
    assert [e["status"] for e in speicher.protokoll] == ["success"]
    assert naechster(a) == uhr.jetzt + timedelta(minutes=10)
    # Der Name erscheint wörtlich, nicht als Auszeichnung ausgewertet
    assert f"{MARKUP_NAME}: Änderungsabgleich beginnt" in ausgabe.getvalue()
    print_overview(scheduler)
    assert f"nächste fällig {MARKUP_NAME} " in ausgabe.getvalue()


async def test_fehler_neben_dem_abgleich_startet_die_quelle_nicht_sofort_wieder(
    uhr: Uhr, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = quelle("A")
    scheduler, orchestrator, _ = daemon(uhr, [a])
    versuche = 0

    def kaputte_ausgabe(*_args: Any, **_kwargs: Any) -> None:
        nonlocal versuche
        versuche += 1
        raise RuntimeError("Ausgabe gescheitert")

    monkeypatch.setattr(takt_modul.console, "print", kaputte_ausgabe)

    for _ in range(5):
        await durchlauf(scheduler)

    assert versuche == 1, "ein Versuch, kein Dauerlauf ohne Pause"
    assert orchestrator.laeufe == []
    assert naechster(a) == uhr.jetzt + timedelta(minutes=10)
    assert stand(a).run_started is None
    assert scheduler.get_status()["running"] == []


def test_uebersicht_beendet_den_daemon_nie(monkeypatch: pytest.MonkeyPatch) -> None:
    def kaputt() -> dict[str, Any]:
        raise RuntimeError("Status nicht lesbar")

    print_overview(SimpleNamespace(get_status=kaputt))  # type: ignore[arg-type]

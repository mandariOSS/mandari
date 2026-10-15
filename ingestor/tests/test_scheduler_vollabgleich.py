"""
Vollabgleich im Daemon (Issue #1025): Fällt der Start des Vollabgleichs in einen laufenden Änderungsabgleich,
folgt er direkt danach, statt für den Tag zu entfallen.

Der 10-Minuten-Takt hat über die ganze Laufzeit des Daemons dieselbe Phase. Begann ein Zyklus regelmäßig kurz
vor 1 Uhr, übersprang der Scheduler den Vollabgleich deshalb in fast jeder Nacht. Ohne Datenbank und ohne Netz:
Der Orchestrator ist nachgebildet und protokolliert nur, welche Art Abgleich lief.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

import src.scheduler as scheduler_modul
from src.scheduler import SyncScheduler


class _Orchestrator:
    """Ersatz für SyncOrchestrator: hält die Läufe fest und ruft während eines Laufs optional etwas auf."""

    laeufe: list[bool] = []
    waehrend: Callable[[bool], Awaitable[None]] | None = None

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _Orchestrator:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def sync_all(self, full: bool = False) -> list[Any]:
        _Orchestrator.laeufe.append(full)
        aufruf = _Orchestrator.waehrend
        if aufruf is not None:
            _Orchestrator.waehrend = None
            await aufruf(full)
        return []

    def print_result(self, _result: Any) -> None:
        return None

    async def write_results_to_synclog(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> SyncScheduler:
    _Orchestrator.laeufe = []
    _Orchestrator.waehrend = None
    monkeypatch.setattr(scheduler_modul, "SyncOrchestrator", _Orchestrator)
    return SyncScheduler(sync_interval_minutes=10, full_sync_hour=1)


async def test_vollabgleich_waehrend_aenderungsabgleich_folgt_danach(daemon: SyncScheduler) -> None:
    async def cron_feuert(_full: bool) -> None:
        await daemon._run_full_sync()  # 1 Uhr, der Änderungsabgleich läuft noch

    _Orchestrator.waehrend = cron_feuert
    await daemon._run_incremental_sync()

    assert _Orchestrator.laeufe == [False, True]
    assert daemon._full_sync_pending is False and daemon._is_syncing is False
    assert "last_full" in daemon._sync_stats

    await daemon._run_incremental_sync()
    assert _Orchestrator.laeufe == [False, True, False], "nur einmal nachgeholt"


async def test_ohne_ueberschneidung_kein_zusaetzlicher_vollabgleich(daemon: SyncScheduler) -> None:
    await daemon._run_incremental_sync()
    await daemon._run_incremental_sync()

    assert _Orchestrator.laeufe == [False, False]


async def test_vollabgleich_waehrend_vollabgleich_laeuft_nicht_doppelt(daemon: SyncScheduler) -> None:
    async def erneut_angefordert(_full: bool) -> None:
        await daemon._run_full_sync()  # etwa aus dem Admin, während der nächtliche Vollabgleich läuft

    _Orchestrator.waehrend = erneut_angefordert
    await daemon._run_full_sync()
    await daemon._run_incremental_sync()

    assert _Orchestrator.laeufe == [True, False]
    assert daemon._full_sync_pending is False


async def test_aenderungsabgleich_waehrend_vollabgleich_entfaellt_weiter(daemon: SyncScheduler) -> None:
    async def takt_feuert(_full: bool) -> None:
        await daemon._run_incremental_sync()

    _Orchestrator.waehrend = takt_feuert
    await daemon._run_full_sync()

    assert _Orchestrator.laeufe == [True]

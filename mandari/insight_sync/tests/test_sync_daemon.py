# SPDX-License-Identifier: AGPL-3.0-or-later
"""``manage.py sync_daemon``: Zyklen enden erfolgreich, Wartezeit stimmt über Stundengrenzen."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from insight_sync.management.commands.sync_daemon import Command, seconds_until_next_slot
from insight_sync.models import SyncLog


def _ergebnis() -> SimpleNamespace:
    zaehler = {
        f"{name}_synced": 0
        for name in (
            "organizations",
            "persons",
            "memberships",
            "papers",
            "files",
            "locations",
            "agenda_items",
            "consultations",
        )
    }
    return SimpleNamespace(source_name="Beispielquelle", errors=[], meetings_synced=3, **zaehler)


class _Orchestrator:
    """Ersatz für den SyncOrchestrator des Ingestors (kein Netz, keine Datenbank des Ingestors)."""

    aufrufe: list[bool] = []

    def __init__(self, max_concurrent: int = 10) -> None:
        self.max_concurrent = max_concurrent

    async def __aenter__(self) -> _Orchestrator:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def sync_all(self, full: bool = False) -> list[SimpleNamespace]:
        self.aufrufe.append(full)
        return [_ergebnis()]


@pytest.mark.django_db
def test_daemon_zyklus_endet_erfolgreich(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vorher lief die Schleife unter asyncio.run – jeder Zyklus scheiterte und nichts wurde protokolliert."""
    _Orchestrator.aufrufe = []
    monkeypatch.setattr("insight_sync.tasks._get_sync_orchestrator", lambda: _Orchestrator)
    befehl = Command()
    monkeypatch.setattr(befehl, "_run_georef_pass", lambda: None)
    befehl._running = False  # nach dem Startlauf beenden

    befehl._run_daemon(interval=15, full_hour=3, concurrent=2)

    assert _Orchestrator.aufrufe == [False]
    log = SyncLog.objects.get()
    assert log.status == SyncLog.Status.SUCCESS
    assert log.triggered_by == "daemon"
    assert log.entities_synced == 3


@pytest.mark.parametrize(
    ("jetzt", "intervall", "erwartet"),
    [
        # 10:50:30, Raster 15 min → 11:00:00 (vorher: 10:00, also sofort und in Dauerschleife)
        (datetime(2026, 9, 29, 10, 50, 30), 15, 570),
        # genau auf dem Rasterpunkt → der nächste, nicht sofort
        (datetime(2026, 9, 29, 11, 0, 0), 15, 900),
        # über Mitternacht
        (datetime(2026, 9, 29, 23, 55, 0), 10, 300),
        # Intervall teilt die Stunde nicht
        (datetime(2026, 9, 29, 0, 50, 0), 45, 40 * 60),
    ],
)
def test_wartezeit_bis_zum_naechsten_rasterpunkt(jetzt: datetime, intervall: int, erwartet: int) -> None:
    assert seconds_until_next_slot(jetzt, intervall) == erwartet

"""
Sync-Protokoll: Startzeit und Dauer stimmen unabhängig von der Zeitzone des Prozesses (Issue #692).

Der Daemon merkt sich den Start als Ortszeit ohne Zeitzone. Wurde sie als UTC gedeutet, lag ``started_at``
im Container mit ``TZ=Europe/Berlin`` ein bis zwei Stunden in der Zukunft und die Dauer war negativ – der
Betriebsmonitor hielt einen stehenden Daemon entsprechend länger für aktiv.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.scheduler import SyncScheduler
from src.sync.orchestrator import SyncOrchestrator, SyncResult


class _Speicher:
    def __init__(self) -> None:
        self.eintraege: list[dict[str, Any]] = []

    async def write_sync_log(self, **felder: Any) -> None:
        self.eintraege.append(felder)


@pytest.fixture
def berliner_zeit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Zeitzone des Prozesses auf Europe/Berlin stellen (wie im Container); unter Windows gilt die des Rechners."""
    if not hasattr(time, "tzset"):
        if datetime.now().astimezone().utcoffset() == timedelta(0):
            pytest.skip("Zeitzone des Prozesses ist UTC und lässt sich hier nicht umstellen")
        yield
        return
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _orchestrator() -> tuple[SyncOrchestrator, _Speicher]:
    speicher = _Speicher()
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = speicher  # type: ignore[assignment]
    return orchestrator, speicher


def _pruefe(eintrag: dict[str, Any]) -> None:
    jetzt = datetime.now(UTC)
    assert eintrag["started_at"].tzinfo is not None
    assert eintrag["started_at"] <= eintrag["finished_at"] <= jetzt
    assert jetzt - eintrag["started_at"] < timedelta(seconds=30), "Start liegt nicht in der Gegenwart"
    assert 0 <= eintrag["duration_seconds"] < 30


@pytest.mark.usefixtures("berliner_zeit")
async def test_startzeit_ohne_zeitzone_ist_ortszeit() -> None:
    orchestrator, speicher = _orchestrator()

    await orchestrator.write_results_to_synclog(
        [SyncResult(source_url="https://ris.example.org", source_name="Musterstadt", success=True)],
        datetime.now(),
    )

    _pruefe(speicher.eintraege[0])


@pytest.mark.usefixtures("berliner_zeit")
async def test_daemon_schreibt_start_in_der_gegenwart() -> None:
    orchestrator, speicher = _orchestrator()
    scheduler = SyncScheduler.__new__(SyncScheduler)

    await scheduler._write_cycle_log(orchestrator, [], datetime.now(), "incremental")

    eintrag = speicher.eintraege[0]
    _pruefe(eintrag)
    assert eintrag["triggered_by"] == "daemon"


async def test_startzeit_mit_zeitzone_bleibt_unveraendert() -> None:
    orchestrator, speicher = _orchestrator()
    start = datetime.now(UTC) - timedelta(seconds=5)

    await orchestrator.write_results_to_synclog([], start, triggered_by="cli")

    assert speicher.eintraege[0]["started_at"] == start
    assert 5 <= speicher.eintraege[0]["duration_seconds"] < 30

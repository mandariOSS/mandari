# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Scraper-Quellen nicht im Worker der Anwendung abgleichen (Teil von Issue #1027).

Ohne laufenden Ingestor oder an einer abgeschalteten Quelle laufen „Inkrementeller Sync“ und „Vollständiger Sync“ im
Worker (``run_sync_with_logging``). Für eine Scraper-Quelle (SessionNet) rief er bisher die Basis-URL als
OParl-System ab, scheiterte mit „Failed to fetch system“ und zählte das als Fehler der Quelle. Dem Image der Anwendung
fehlen die Abhängigkeiten der Scraper; der Worker lässt solche Quellen deshalb aus und sagt im Sync-Protokoll, wie man
sie abgleicht. OParl-Quellen gleicht er weiter ab.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from insight_core.models import OParlSource
from insight_sync import tasks
from insight_sync.models import SyncLog

pytestmark = pytest.mark.django_db


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
    return SimpleNamespace(source_name="Beispiel-RIS", errors=[], meetings_synced=2, **zaehler)


class _Orchestrator:
    """Ersatz für den SyncOrchestrator des Ingestors (kein Netz, keine Datenbank des Ingestors)."""

    aufrufe: list[tuple[str, bool]] = []

    def __init__(self, max_concurrent: int = 10) -> None:
        self.max_concurrent = max_concurrent

    async def __aenter__(self) -> _Orchestrator:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def sync_source(self, url: str, full: bool = False) -> SimpleNamespace:
        self.aufrufe.append((url, full))
        return _ergebnis()


@pytest.fixture
def orchestrator(monkeypatch: pytest.MonkeyPatch) -> type[_Orchestrator]:
    _Orchestrator.aufrufe = []
    monkeypatch.setattr(tasks, "_get_sync_orchestrator", lambda: _Orchestrator)
    return _Orchestrator


def _quelle(sync_config: dict[str, Any] | None = None) -> OParlSource:
    return OParlSource.objects.create(
        name="Samtgemeinde", url="https://sessionnet.example.de/bi", sync_config=sync_config or {}
    )


@pytest.mark.parametrize("full", [False, True])
def test_scraper_quelle_laeuft_nicht_im_worker(orchestrator: type[_Orchestrator], full: bool) -> None:
    quelle = _quelle({"source_type": "scraper:sessionnet"})

    tasks.run_sync_with_logging(source=quelle, full=full, triggered_by="admin")

    assert orchestrator.aufrufe == [], "kein Abruf der Basis-URL als OParl-System"
    log = SyncLog.objects.get()
    assert log.source_id == quelle.pk
    assert log.status == SyncLog.Status.FAILED
    assert log.errors == [tasks.SCRAPER_NUR_IM_INGESTOR]
    assert log.finished_at is not None
    assert log.sync_type == (SyncLog.SyncType.FULL if full else SyncLog.SyncType.INCREMENTAL)
    quelle.refresh_from_db()
    assert (quelle.consecutive_failures or 0) == 0, "kein Fehler der Quelle"


def test_oparl_quelle_laeuft_weiter_im_worker(orchestrator: type[_Orchestrator]) -> None:
    quelle = _quelle({"source_type": "bridge:allris"})

    tasks.run_sync_with_logging(source=quelle, full=True, triggered_by="admin")

    assert orchestrator.aufrufe == [(quelle.url, True)]
    log = SyncLog.objects.get()
    assert log.status == SyncLog.Status.SUCCESS
    assert log.entities_synced == 2


@pytest.mark.parametrize(
    ("sync_config", "erwartet"),
    [
        ({"source_type": "scraper:sessionnet"}, True),
        ({"source_type": "scraper:"}, True),
        ({"source_type": "oparl"}, False),
        ({"source_type": "bridge:allris"}, False),
        ({"source_type": None}, False),
        ({}, False),
        (None, False),
        ("scraper:sessionnet", False),
    ],
)
def test_scraper_quelle_erkennen(sync_config: Any, erwartet: bool) -> None:
    assert tasks.is_scraper_source(SimpleNamespace(sync_config=sync_config)) is erwartet

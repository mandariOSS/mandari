# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Scraper-Quellen auch beim Abgleich einer einzelnen Quelle über den Scraper (Teil von Issue #1027).

Der Daemon gleicht eine Quelle über ``sync_body_url`` ab, das Scraper-Quellen (``sync_config["source_type"] =
"scraper:<vendor>"``) an den ``ScraperSyncRunner`` gibt. Der Worker der Anwendung (``run_sync_with_logging``, Aktionen
„Inkrementeller Sync“ und „Vollständiger Sync“ ohne laufenden Ingestor oder an einer abgeschalteten Quelle) und
``python -m src.main sync --source`` kommen dagegen über ``sync_source``. Dort fehlte der Zweig: Die Basis-URL eines
SessionNet wurde als OParl-System abgerufen, der Abgleich schlug mit „Failed to fetch system“ fehl und zählte als
Fehler der Quelle. Jetzt nehmen beide Einstiege denselben Weg. Ohne Netz und ohne Datenbank.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from mandari_oparl import IdBases

from src.scrapers import runner as runner_module
from src.sync import orchestrator as orchestrator_module
from src.sync.orchestrator import SyncOrchestrator, SyncResult
from src.sync.processor import OParlProcessor

BASIS = "https://sessionnet.example.de/bi"
SCRAPER = {"source_type": "scraper:sessionnet"}


class _Speicher:
    """Was die Einstiege vor der Weiche brauchen: die Quelle und die Basen der Kennungen."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self.id_bases = IdBases()
        self.angefragt: list[str] = []

    async def get_source_by_url(self, url: str) -> Any:
        self.angefragt.append(url)
        return self.source


class _Runner:
    """Attrappe des ``ScraperSyncRunner``: merkt sich Quelle und Art des Abgleichs."""

    laeufe: list[tuple[Any, bool]] = []

    def __init__(self, orchestrator: SyncOrchestrator, source: Any) -> None:
        self.orchestrator = orchestrator
        self.source = source

    async def run(self, full: bool = False) -> SyncResult:
        _Runner.laeufe.append((self.source, full))
        return SyncResult(source_url=self.source.url, source_name="Scraper", success=True, full=full)


class _KeinOParlClient:
    """Ein Abruf als OParl-System wäre der Fehler: Schon das Anlegen des Clients schlägt fehl."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("Scraper-Quelle über den OParl-Client abgerufen")

    @staticmethod
    def get_modified_since_unsupported() -> set[str]:
        raise AssertionError("Scraper-Quelle über den OParl-Pfad abgeglichen")


def _quelle(sync_config: Any) -> SimpleNamespace:
    return SimpleNamespace(id="quelle-1", url=BASIS, name="Samtgemeinde", sync_config=sync_config)


def _orchestrator(source: Any) -> SyncOrchestrator:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = _Speicher(source)
    orchestrator.processor = OParlProcessor()
    orchestrator.max_concurrent = 2
    orchestrator._parallel_mode = False

    async def _kein_oparl_cache() -> None:
        raise AssertionError("Scraper-Quelle über den OParl-Pfad abgeglichen")

    orchestrator._seed_modified_since_cache = _kein_oparl_cache  # type: ignore[method-assign]
    return orchestrator


@pytest.fixture
def runner_attrappe(monkeypatch: pytest.MonkeyPatch) -> type[_Runner]:
    _Runner.laeufe = []
    monkeypatch.setattr(runner_module, "ScraperSyncRunner", _Runner)
    monkeypatch.setattr(orchestrator_module, "OParlClient", _KeinOParlClient)
    return _Runner


@pytest.mark.parametrize("einstieg", ["sync_source", "sync_body_url"])
@pytest.mark.parametrize("full", [False, True])
async def test_scraper_quelle_laeuft_ueber_den_scraper(
    runner_attrappe: type[_Runner], einstieg: str, full: bool
) -> None:
    quelle = _quelle(dict(SCRAPER))
    orchestrator = _orchestrator(quelle)

    result = await getattr(orchestrator, einstieg)(BASIS, full=full)

    assert result.success is True
    assert result.full is full
    assert runner_attrappe.laeufe == [(quelle, full)]
    assert orchestrator.storage.angefragt == [BASIS]


async def test_filter_nach_kommune_gilt_bei_scraper_quellen_nicht(runner_attrappe: type[_Runner]) -> None:
    quelle = _quelle(dict(SCRAPER))

    result = await _orchestrator(quelle).sync_source(BASIS, body_filter="Musterdorf")

    assert result.success is True
    assert runner_attrappe.laeufe == [(quelle, False)]


@pytest.mark.parametrize(
    "sync_config",
    [None, {}, {"source_type": "oparl"}, {"source_type": "bridge:allris"}, {"source_type": ""}, "kaputt"],
)
def test_ohne_scraper_typ_kein_runner(sync_config: Any) -> None:
    orchestrator = _orchestrator(_quelle(sync_config))

    assert orchestrator._scraper_runner(_quelle(sync_config)) is None


def test_unbekannte_quelle_kein_runner() -> None:
    assert _orchestrator(None)._scraper_runner(None) is None


def test_scraper_typ_ergibt_runner(runner_attrappe: type[_Runner]) -> None:
    quelle = _quelle({"source_type": "scraper:sessionnet"})
    orchestrator = _orchestrator(quelle)

    runner = orchestrator._scraper_runner(quelle)

    assert isinstance(runner, _Runner)
    assert runner.source is quelle
    assert runner.orchestrator is orchestrator

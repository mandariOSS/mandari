# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Scraper-Quellen auch beim Abgleich einer einzelnen Quelle über den Scraper (Teil von Issue #1027).

Der Daemon gleicht eine Quelle über ``sync_body_url`` ab, das Scraper-Quellen (``sync_config["source_type"] =
"scraper:<vendor>"``) an den ``ScraperSyncRunner`` gibt. ``python -m src.main sync --source`` kam bis Issue #1043
über einen eigenen Einstieg (``sync_source``), dem der Zweig fehlte: Die Basis-URL eines SessionNet wurde als
OParl-System abgerufen, der Abgleich schlug mit „Failed to fetch system“ fehl und zählte als Fehler der Quelle. Seit
Issue #554 gibt es nur noch den Einstieg des Daemons; ``sync --source`` nutzt ihn ebenfalls. Ohne Netz und ohne
Datenbank.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from mandari_oparl import IdBases
from typer.testing import CliRunner

from src import main as cli
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


def _quelle(sync_config: Any) -> SimpleNamespace:
    return SimpleNamespace(id="quelle-1", url=BASIS, name="Samtgemeinde", sync_config=sync_config)


def _orchestrator(source: Any) -> SyncOrchestrator:
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = _Speicher(source)
    orchestrator.processor = OParlProcessor()
    orchestrator.max_concurrent = 2
    orchestrator._parallel_mode = False

    def _keine_vermerke(url: str, source_row: Any) -> Any:
        # Erster Schritt des OParl-Pfads: Vermerke „filtert nicht“ der Quelle (Issue #1041)
        raise AssertionError("Scraper-Quelle über den OParl-Pfad abgeglichen")

    orchestrator._filter_notes = _keine_vermerke  # type: ignore[method-assign]
    return orchestrator


@pytest.fixture
def runner_attrappe(monkeypatch: pytest.MonkeyPatch) -> type[_Runner]:
    _Runner.laeufe = []
    monkeypatch.setattr(runner_module, "ScraperSyncRunner", _Runner)
    monkeypatch.setattr(orchestrator_module, "OParlClient", _KeinOParlClient)
    return _Runner


@pytest.mark.parametrize("full", [False, True])
async def test_scraper_quelle_laeuft_ueber_den_scraper(runner_attrappe: type[_Runner], full: bool) -> None:
    quelle = _quelle(dict(SCRAPER))
    orchestrator = _orchestrator(quelle)

    result = await orchestrator.sync_body_url(BASIS, full=full)

    assert result.success is True
    assert result.full is full
    assert runner_attrappe.laeufe == [(quelle, full)]
    assert orchestrator.storage.angefragt == [BASIS]


async def test_filter_nach_kommune_gilt_bei_scraper_quellen_nicht(runner_attrappe: type[_Runner]) -> None:
    quelle = _quelle(dict(SCRAPER))

    result = await _orchestrator(quelle).sync_body_url(BASIS, body_filter="Musterdorf")

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


def test_nur_ein_einstieg_fuer_den_abgleich_einer_quelle() -> None:
    """Issue #554: Den abweichenden Altpfad ``sync_source`` gibt es nicht mehr."""
    assert not hasattr(SyncOrchestrator, "sync_source")


@pytest.mark.parametrize(
    ("argumente", "erwartet"),
    [
        ([], {"full": False, "body_filter": None}),
        (["--full"], {"full": True, "body_filter": None}),
        (["--filter", "Musterdorf"], {"full": False, "body_filter": "Musterdorf"}),
    ],
)
def test_sync_source_nimmt_den_einstieg_des_daemons(
    monkeypatch: pytest.MonkeyPatch, argumente: list[str], erwartet: dict[str, Any]
) -> None:
    """``sync --source`` gleicht über ``sync_body_url`` ab wie der Daemon, mit ``--full`` und ``--filter``."""
    aufrufe: list[dict[str, Any]] = []
    protokolle: list[str] = []

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
            aufrufe.append({"url": url, **kwargs})
            return SyncResult(source_url=url, source_name="Samtgemeinde", success=True)

        def print_result(self, result: SyncResult) -> None:
            return None

        async def write_results_to_synclog(self, results: list[SyncResult], *args: Any, **kwargs: Any) -> None:
            protokolle.append(kwargs["triggered_by"])

    monkeypatch.setattr(cli, "SyncOrchestrator", Orchestrator)
    result = CliRunner().invoke(cli.app, ["sync", "--source", BASIS, *argumente])

    assert result.exit_code == 0, result.output
    assert len(aufrufe) == 1
    assert aufrufe[0]["url"] == BASIS
    assert {schluessel: aufrufe[0][schluessel] for schluessel in erwartet} == erwartet
    assert protokolle == ["cli"], "ein Protokoll für den Aufruf, kein zweites mit triggered_by=daemon"

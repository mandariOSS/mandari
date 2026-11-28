# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Suchindex als Abonnement (Issue #527): Mit ``SEARCH_INDEX_SUBSCRIPTION=aktiv`` schreibt nur noch das Abonnement
``suchindex`` der Anwendung den Index. Der Ingestor schreibt dann nicht mehr, gleich was
``ELASTICSEARCH_INDEXING_ENABLED`` sagt; mit ``aus`` und ``schatten`` schreibt er wie bisher.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from src.config import Settings, elasticsearch_writes_enabled, settings


@pytest.mark.parametrize(
    ("indexing", "modus", "schreibt"),
    [
        (True, "aus", True),
        (True, "schatten", True),
        (True, "aktiv", False),
        (True, " Aktiv ", False),
        (False, "aus", False),
        (False, "aktiv", False),
    ],
)
def test_schalter(indexing: bool, modus: str, schreibt: bool) -> None:
    werte = Settings(elasticsearch_indexing_enabled=indexing, search_index_subscription=modus)
    assert elasticsearch_writes_enabled(werte) is schreibt


def test_schalter_aus_der_umgebung(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_INDEX_SUBSCRIPTION", "aktiv")
    assert Settings().search_index_subscription == "aktiv"
    assert elasticsearch_writes_enabled(Settings()) is False


class _Indexer:
    """Ersatz für den Elasticsearch-Indexer: merkt sich jeden Zugriff."""

    zugriffe: list[str] = []

    async def __aenter__(self) -> _Indexer:
        self.zugriffe.append("verbunden")
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def is_healthy(self) -> bool:
        return True

    async def missing_indices(self) -> set[str]:
        return set()

    async def delete_documents(self, index: str, ids: list[str]) -> None:
        self.zugriffe.append(f"delete {index}")

    async def index_documents(self, index: str, docs: list[dict[str, Any]]) -> None:
        self.zugriffe.append(f"index {index}")


class _Speicher:
    async def iter_for_body(self, *_: Any, **__: Any) -> Any:
        return
        yield

    async def iter_files_with_text(self, *_: Any, **__: Any) -> Any:
        return
        yield


@pytest.mark.parametrize(("modus", "schreibt"), [("aktiv", False), ("schatten", True), ("aus", True)])
async def test_mit_aktiv_schreibt_der_ingestor_nicht(
    monkeypatch: pytest.MonkeyPatch, modus: str, schreibt: bool
) -> None:
    import src.indexing.elasticsearch as es_modul
    from src.sync.orchestrator import SyncOrchestrator

    _Indexer.zugriffe = []
    monkeypatch.setattr(es_modul, "ElasticsearchIndexer", _Indexer)
    monkeypatch.setattr(settings, "elasticsearch_indexing_enabled", True)
    monkeypatch.setattr(settings, "search_index_subscription", modus)
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = _Speicher()
    stats: dict[str, Any] = {"errors": []}

    # Auch Aufrufer, die den Schalter nicht selbst prüfen (OParl-Abgleich und Scraper prüfen ihn), schreiben nichts
    await orchestrator._index_body_elasticsearch(uuid4(), stats, {"papers": [str(uuid4())]}, full=True)

    assert bool(_Indexer.zugriffe) is schreibt
    if schreibt:
        assert "delete papers" in _Indexer.zugriffe
    assert stats["errors"] == []

"""
Elasticsearch-Indexer: Der Ingestor legt keine Indizes an und ändert keine Mappings;
das ist Aufgabe von Django (Issue #215). Fehlende Indizes werden gemeldet und beim
Schreiben übersprungen, damit Elasticsearch sie nicht dynamisch ohne die deutschen
Analyzer auto-anlegt.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

import src.indexing.elasticsearch as es_modul
from src.indexing.elasticsearch import INDEX_NAMES, ElasticsearchIndexer


class FakeElasticsearch:
    def __init__(self, vorhanden: set[str]) -> None:
        self.vorhanden = vorhanden
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method == "HEAD":
            return httpx.Response(200 if request.url.path.lstrip("/") in self.vorhanden else 404)
        if request.url.path == "/_bulk":
            return httpx.Response(200, json={"errors": False, "items": []})
        return httpx.Response(200, json={})


def _indexer(fake: FakeElasticsearch) -> ElasticsearchIndexer:
    indexer = ElasticsearchIndexer(url="http://es.test")
    indexer._client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), base_url="http://es.test")
    return indexer


@pytest.mark.asyncio
async def test_missing_indices_are_reported_not_created() -> None:
    fake = FakeElasticsearch(vorhanden={"papers", "meetings"})
    indexer = _indexer(fake)

    missing = await indexer.missing_indices()

    assert missing == set(INDEX_NAMES) - {"papers", "meetings"}
    assert all(method == "HEAD" for method, _ in fake.requests), "nur HEAD, kein PUT (Index/Mapping)"


@pytest.mark.asyncio
async def test_writes_to_missing_index_are_skipped() -> None:
    fake = FakeElasticsearch(vorhanden={"papers"})
    indexer = _indexer(fake)
    await indexer.missing_indices()
    fake.requests.clear()

    assert await indexer.index_documents("papers", [{"id": "1", "name": "Antrag"}]) is True
    assert await indexer.index_documents("files", [{"id": "2", "name": "Anlage"}]) is False
    assert await indexer.delete_documents("files", ["2"]) is False

    assert fake.requests == [("POST", "/_bulk")], "fehlender Index: kein Request, kein Auto-Create"


def test_indexer_defines_no_mappings() -> None:
    quelle = inspect.getsource(es_modul)
    assert "_mapping" not in quelle
    assert '"analyzer"' not in quelle

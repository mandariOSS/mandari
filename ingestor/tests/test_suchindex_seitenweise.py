# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Suchindex seitenweise statt mit fester Obergrenze.

Früher lud die Indexierung je Art höchstens 10.000 Objekte eines Bodies (``get_all_for_body(limit=10000)``)
und alle Dateien mit Volltext auf einmal. Bei einer großen Quelle fehlten dadurch rund 2.500 Vorlagen in der
Suche. Jetzt läuft jede Art seitenweise (Keyset nach ``id``) vollständig durch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.indexing import document_builders
from src.storage.database import DatabaseStorage
from src.storage.models import OParlFile, OParlPaper

GROSS = 10_005  # mehr als die frühere Obergrenze


class _Indexer:
    """Ersatz für den Elasticsearch-Indexer: sammelt geschriebene Dokumente je Index."""

    docs: dict[str, list[dict[str, Any]]] = {}

    async def __aenter__(self) -> _Indexer:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def is_healthy(self) -> bool:
        return True

    async def missing_indices(self) -> set[str]:
        return set()

    async def delete_documents(self, index: str, ids: list[str]) -> None:
        return None

    async def index_documents(self, index: str, docs: list[dict[str, Any]]) -> None:
        self.docs.setdefault(index, []).extend(docs)


class _Speicher:
    """Großer Body im Speicher; die frühere Abfrage mit Obergrenze ist mit nachgebildet."""

    def __init__(self) -> None:
        self.papers = [SimpleNamespace(id=uuid4()) for _ in range(GROSS)]
        self.files = [SimpleNamespace(id=uuid4(), paper_id=self.papers[-1].id, text_content="Volltext")]
        self.page_sizes: list[int] = []

    def _rows(self, model_class: type) -> list[SimpleNamespace]:
        return self.papers if model_class.__name__ == "OParlPaper" else []

    async def get_all_for_body(self, body_id: UUID, model_class: type, limit: int = 10000) -> list[Any]:
        return self._rows(model_class)[:limit]

    async def get_files_with_text(self, body_id: UUID) -> list[Any]:
        return self.files

    async def iter_for_body(self, body_id: UUID, model_class: type, page_size: int | None = None) -> AsyncIterator:
        rows = self._rows(model_class)
        size = page_size or 500
        for start in range(0, len(rows), size):
            self.page_sizes.append(len(rows[start : start + size]))
            yield rows[start : start + size]

    async def get_files_with_text_for_papers(self, body_id: UUID, paper_ids: list[UUID]) -> list[Any]:
        wanted = set(paper_ids)
        return [f for f in self.files if f.paper_id in wanted]

    async def iter_files_with_text(self, body_id: UUID, page_size: int | None = None) -> AsyncIterator:
        yield self.files


@pytest.fixture
def indexer(monkeypatch: pytest.MonkeyPatch) -> type[_Indexer]:
    import src.indexing.elasticsearch as es_modul

    _Indexer.docs = {}
    monkeypatch.setattr(es_modul, "ElasticsearchIndexer", _Indexer)
    # Dokumente schlank halten: geprüft wird die Vollständigkeit, nicht der Inhalt
    monkeypatch.setattr(
        document_builders,
        "paper_to_doc",
        lambda p, files=None: {"id": str(p.id), "files": len(files or [])},
    )
    monkeypatch.setattr(document_builders, "file_to_doc", lambda f: {"id": str(f.id)})
    return _Indexer


async def test_grosser_body_wird_vollstaendig_indexiert(indexer: type[_Indexer]) -> None:
    from src.sync.orchestrator import SyncOrchestrator

    speicher = _Speicher()
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = speicher
    stats: dict[str, Any] = {}

    await orchestrator._index_body_elasticsearch(uuid4(), stats, {}, full=True)

    assert "errors" not in stats
    papers = indexer.docs["papers"]
    assert len(papers) == GROSS
    assert len({d["id"] for d in papers}) == GROSS
    assert stats["indexed"] == GROSS + len(speicher.files)
    # Dateitexte erreichen den Vorgang auch auf der letzten Seite
    assert papers[-1]["files"] == 1
    assert max(speicher.page_sizes) <= 500


class _Sitzung:
    def __init__(self, seiten: list[list[Any]], statements: list[Any]) -> None:
        self.seiten = seiten
        self.statements = statements

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        rows = self.seiten.pop(0) if self.seiten else []
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _speicher_mit(seiten: list[list[Any]], statements: list[Any]) -> DatabaseStorage:
    storage = DatabaseStorage(database_url="postgresql+asyncpg://nicht/benutzt")

    @asynccontextmanager
    async def sitzung() -> AsyncIterator[_Sitzung]:
        yield _Sitzung(seiten, statements)

    storage.get_session = sitzung  # type: ignore[method-assign,assignment]
    return storage


def _sql(stmt: Any) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": False}))


async def test_keyset_seiten_bis_zur_kurzen_seite() -> None:
    erste = [SimpleNamespace(id=UUID(int=i)) for i in (1, 2)]
    zweite = [SimpleNamespace(id=UUID(int=i)) for i in (3, 4)]
    dritte = [SimpleNamespace(id=UUID(int=5))]
    statements: list[Any] = []
    storage = _speicher_mit([erste, zweite, dritte], statements)

    seiten = [rows async for rows in storage.iter_for_body(uuid4(), OParlPaper, page_size=2)]

    assert seiten == [erste, zweite, dritte]
    assert len(statements) == 3  # die kurze dritte Seite beendet den Lauf ohne weitere Abfrage
    assert "ORDER BY oparl_papers.id" in _sql(statements[0])
    assert "oparl_papers.id >" not in _sql(statements[0])
    assert "oparl_papers.id >" in _sql(statements[1])
    assert statements[1].compile().params["id_1"] == UUID(int=2)
    assert statements[2].compile().params["id_1"] == UUID(int=4)


async def test_dateien_mit_text_seitenweise_und_je_vorgangsseite() -> None:
    statements: list[Any] = []
    storage = _speicher_mit([[SimpleNamespace(id=UUID(int=1))], []], statements)

    seiten = [rows async for rows in storage.iter_files_with_text(uuid4(), page_size=1)]
    assert len(seiten) == 1 and len(statements) == 2
    assert "oparl_files.text_content IS NOT NULL" in _sql(statements[0])
    assert "LIMIT" in _sql(statements[0])

    assert await storage.get_files_with_text_for_papers(uuid4(), []) == []
    paper_id = uuid4()
    await storage.get_files_with_text_for_papers(uuid4(), [paper_id])
    assert "oparl_files.paper_id IN" in _sql(statements[-1])
    assert OParlFile.__tablename__ == "oparl_files"

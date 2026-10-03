# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ein Sync überschreibt die Portal-Felder der Suchdokumente nicht mehr (Issue #429).

Vorher schrieb der Ingestor nach jedem Sync vollständige Dokumente per Bulk-``index``. Felder, die nur Django
setzt – vor allem ``organization_names`` für den Gremienfilter –, fehlten danach bis zur nächsten
Neuindexierung. Jetzt schreibt er per ``update``/``doc_as_upsert`` nur seine eigenen Felder.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from src.indexing import document_builders as builders
from src.indexing.elasticsearch import INDEX_NAMES, ElasticsearchIndexer


class MergingElasticsearch:
    """Elasticsearch-Attrappe mit der Semantik der Bulk-Aktionen ``index`` (ersetzt) und ``update`` (führt zusammen)."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in INDEX_NAMES}
        self.actions: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method in ("GET", "HEAD"):
            return httpx.Response(200, json={})
        assert request.url.path == "/_bulk"
        lines = [json.loads(line) for line in request.content.decode().splitlines() if line]
        items = []
        for action, body in zip(lines[::2], lines[1::2], strict=True):
            self.actions.append(action)
            ((kind, meta),) = action.items()
            index = self.docs[meta["_index"]]
            if kind == "index":
                index[meta["_id"]] = body
            elif kind == "update":
                if meta["_id"] in index:
                    index[meta["_id"]].update(body["doc"])
                elif body.get("doc_as_upsert"):
                    index[meta["_id"]] = dict(body["doc"])
                else:
                    items.append({kind: {"status": 404, "error": {"type": "document_missing_exception"}}})
                    continue
            items.append({kind: {"status": 200}})
        errors = any("error" in outcome for item in items for outcome in item.values())
        return httpx.Response(200, json={"errors": errors, "items": items})


@pytest.fixture
def es(monkeypatch: pytest.MonkeyPatch) -> MergingElasticsearch:
    fake = MergingElasticsearch()
    real_client = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(fake.handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return fake


# ---------------------------------------------------------------------------
# Bulk-Aufruf
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_schreibt_partielle_updates(es: MergingElasticsearch) -> None:
    async with ElasticsearchIndexer(url="http://es.test") as indexer:
        assert await indexer.index_documents("papers", [{"id": "p1", "name": "Antrag"}]) is True

    assert es.actions == [{"update": {"_index": "papers", "_id": "p1", "retry_on_conflict": 3}}]
    assert es.docs["papers"]["p1"] == {"id": "p1", "name": "Antrag"}


def test_ingestor_dokumente_enthalten_keine_portal_felder() -> None:
    now = datetime(2026, 9, 29, tzinfo=UTC)
    rows = _rows(uuid4(), now)
    docs = [
        builders.paper_to_doc(rows["paper"], files=[rows["file"]]),
        builders.meeting_to_doc(rows["meeting"]),
        builders.person_to_doc(rows["person"]),
        builders.organization_to_doc(rows["organization"]),
        builders.file_to_doc(rows["file"]),
    ]
    for doc in docs:
        assert not builders.DJANGO_ONLY_FIELDS & doc.keys(), doc["type"]


def test_leerer_name_ueberschreibt_den_ersatznamen_des_portals_nicht() -> None:
    now = datetime(2026, 9, 29, tzinfo=UTC)
    rows = _rows(uuid4(), now)
    rows["meeting"].name = None
    rows["person"].name = ""

    assert "name" not in builders.meeting_to_doc(rows["meeting"])
    assert "name" not in builders.person_to_doc(rows["person"])


# ---------------------------------------------------------------------------
# Nachweis mit einem Sync-Lauf (Indexierungsphase des Orchestrators)
# ---------------------------------------------------------------------------


def _rows(body_id: UUID, now: datetime) -> dict[str, SimpleNamespace]:
    paper_id, meeting_id = uuid4(), uuid4()
    return {
        "paper": SimpleNamespace(
            id=paper_id,
            body_id=body_id,
            name="Neubau Kita",
            reference="V/0815",
            paper_type="Antrag",
            date=now.date(),
            oparl_created=now,
            oparl_modified=now,
        ),
        "meeting": SimpleNamespace(
            id=meeting_id,
            body_id=body_id,
            name="Sitzung des Bauausschusses",
            location_name="Rathaus, Saal 2",
            start=now,
            end=None,
            cancelled=False,
            oparl_modified=now,
        ),
        "person": SimpleNamespace(
            id=uuid4(),
            body_id=body_id,
            name="Erika Mustermann",
            given_name="Erika",
            family_name="Mustermann",
            title="",
            oparl_modified=now,
        ),
        "organization": SimpleNamespace(
            id=uuid4(),
            body_id=body_id,
            name="Bauausschuss",
            short_name="BauA",
            organization_type="committee",
            classification="Ausschuss",
            oparl_modified=now,
        ),
        "file": SimpleNamespace(
            id=uuid4(),
            body_id=body_id,
            name="Beschlussvorlage",
            file_name="vorlage.pdf",
            mime_type="application/pdf",
            text_content="Der Bauausschuss beschließt den Neubau.",
            paper_id=paper_id,
            meeting_id=None,
            oparl_modified=now,
        ),
    }


class StubStorage:
    def __init__(self, rows: dict[str, SimpleNamespace], new_meeting: SimpleNamespace) -> None:
        self.by_model = {
            "OParlPaper": [rows["paper"]],
            "OParlMeeting": [rows["meeting"], new_meeting],
            "OParlPerson": [rows["person"]],
            "OParlOrganization": [rows["organization"]],
        }
        self.files = [rows["file"]]

    async def iter_for_body(
        self, body_id: UUID, model_class: type, page_size: int | None = None, **_kwargs: Any
    ) -> AsyncIterator[list[SimpleNamespace]]:
        yield self.by_model[model_class.__name__]

    async def get_files_with_text_for_papers(
        self, body_id: UUID, paper_ids: list[UUID], max_chars: int | None = None
    ) -> list[SimpleNamespace]:
        return [f for f in self.files if f.paper_id in paper_ids]

    async def iter_files_with_text(
        self, body_id: UUID, page_size: int | None = None, **_kwargs: Any
    ) -> AsyncIterator[list[SimpleNamespace]]:
        yield self.files


def _portal_docs(rows: dict[str, SimpleNamespace]) -> dict[str, dict[str, Any]]:
    """So legt Djangos Indexierung (search_documents.py) die Dokumente an – mit den Portal-Feldern."""
    paper = {**builders.paper_to_doc(rows["paper"]), "organization_names": ["Bauausschuss", "Rat"]}
    meeting = {**builders.meeting_to_doc(rows["meeting"]), "organization_names": ["Bauausschuss"]}
    file = {
        **builders.file_to_doc(rows["file"]),
        "access_url": "https://ris.example.org/files/1.pdf",
        "paper_name": "Neubau Kita",
        "paper_reference": "V/0815",
        "organization_names": ["Bauausschuss"],
        "meeting_name": "Sitzung des Bauausschusses",
        "meeting_date": "2026-09-29T00:00:00+00:00",
        "agenda_number": "5",
    }
    return {"papers": paper, "meetings": meeting, "files": file}


@pytest.mark.asyncio
async def test_sync_erhaelt_gremien_und_portalfelder(es: MergingElasticsearch) -> None:
    from src.sync.orchestrator import SyncOrchestrator

    body_id = uuid4()
    now = datetime(2026, 9, 29, tzinfo=UTC)
    rows = _rows(body_id, now)
    for index, doc in _portal_docs(rows).items():
        es.docs[index][doc["id"]] = dict(doc)

    # Der Sync bringt Änderungen aus dem RIS und eine neue Sitzung
    rows["meeting"].location_name = "Rathaus, Saal 3"
    rows["paper"].name = "Neubau Kita (geändert)"
    new_meeting = SimpleNamespace(**{**vars(rows["meeting"]), "id": uuid4(), "name": "Neue Sitzung"})

    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = StubStorage(rows, new_meeting)
    stats: dict[str, Any] = {}
    await orchestrator._index_body_elasticsearch(body_id, stats, {}, full=False)

    assert "errors" not in stats
    meeting = es.docs["meetings"][str(rows["meeting"].id)]
    assert meeting["organization_names"] == ["Bauausschuss"]  # Gremienfilter trifft weiter
    assert meeting["location_name"] == "Rathaus, Saal 3"  # Ingestor-Felder sind aktuell
    paper = es.docs["papers"][str(rows["paper"].id)]
    assert paper["organization_names"] == ["Bauausschuss", "Rat"]
    assert paper["name"] == "Neubau Kita (geändert)"
    file = es.docs["files"][str(rows["file"].id)]
    assert {k: file[k] for k in ("access_url", "paper_name", "organization_names", "agenda_number")} == {
        "access_url": "https://ris.example.org/files/1.pdf",
        "paper_name": "Neubau Kita",
        "organization_names": ["Bauausschuss"],
        "agenda_number": "5",
    }
    # Neue Objekte legt der Ingestor mit seinen Feldern an; die Portal-Felder ergänzt Django
    assert es.docs["meetings"][str(new_meeting.id)]["name"] == "Neue Sitzung"
    assert all(next(iter(action)) == "update" for action in es.actions)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schreiben in den Suchindex mit begrenztem Speicher.

Der Speicherbedarf beim Indexieren hängt weder von der Größe einzelner Dateitexte noch von der Zahl der
Dokumente ab:

- Bulk-Anfragen werden nach Bytes und Anzahl geteilt; ein Dokument, das nicht mehr passt, beginnt eine neue
  Anfrage, mehrere große Dokumente liegen also nie zusammen in einer Anfrage.
- Die Anfrage entsteht gleich als UTF-8-Bytes, ohne Zwischenstufen als Zeilen und Text, und httpx behält sie
  nach dem Senden nicht an der Anfrage (sonst lebte sie bis zur zyklischen Speicherbereinigung weiter).
- Der Volltext je Datei ist für den Index gekürzt und ohne Steuerzeichen (wie in Django), und die Dateitexte
  kommen in Seiten nach Bytes und schon gekürzt aus der Datenbank.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import tracemalloc
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.indexing.elasticsearch import ElasticsearchIndexer
from src.storage.database import DatabaseStorage
from src.storage.models import Base, OParlFile

#: Bytegrenze je Bulk-Anfrage (dieselbe wie im Abonnement suchindex in Django)
GRENZE = 8 * 1024 * 1024


class _Elasticsearch(httpx.AsyncBaseTransport):
    """Elasticsearch-Attrappe: liest den Körper Stück für Stück wie die echte Verbindung, ohne ihn abzulegen."""

    def __init__(self, behalten: bool = False) -> None:
        self.behalten = behalten
        self.groessen: list[int] = []
        self.koerper: list[bytes] = []
        self.kopfzeilen: list[httpx.Headers] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_bulk"
        groesse = 0
        stuecke: list[bytes] = []
        async for stueck in request.stream:
            groesse += len(stueck)
            if self.behalten:
                stuecke.append(stueck)
        self.groessen.append(groesse)
        self.kopfzeilen.append(request.headers)
        if self.behalten:
            self.koerper.append(b"".join(stuecke))
        return httpx.Response(200, json={"errors": False, "items": []})


def _indexer(transport: _Elasticsearch) -> ElasticsearchIndexer:
    indexer = ElasticsearchIndexer(url="http://es.test")
    indexer._client = httpx.AsyncClient(transport=transport, base_url="http://es.test")
    return indexer


def _dokumente(koerper: bytes) -> list[dict[str, Any]]:
    """Die Dokumente einer Bulk-Anfrage (jede zweite Zeile ist die Aktion)."""
    zeilen = [json.loads(zeile) for zeile in koerper.decode("utf-8").splitlines() if zeile]
    for aktion, quelle in zip(zeilen[::2], zeilen[1::2], strict=True):
        assert aktion["update"]["_id"] == quelle["doc"]["id"]
    return [quelle["doc"] for quelle in zeilen[1::2]]


# ---------------------------------------------------------------------------
# Bulk-Anfragen nach Bytes und Anzahl
# ---------------------------------------------------------------------------


async def test_grosse_dokumente_gehen_nie_zusammen_hinaus() -> None:
    gross = "ä" * 3_000_000  # 6 MB in UTF-8: zwei davon passen nicht unter die Grenze
    riesig = "x" * (GRENZE + 1024)  # allein schon über der Grenze
    docs = [
        {"id": "klein-0", "name": "Antrag"},
        {"id": "gross-0", "text_content": gross},
        {"id": "klein-1", "name": "Beschluss"},
        {"id": "gross-1", "text_content": gross},
        {"id": "gross-2", "text_content": gross},
        {"id": "riesig", "text_content": riesig},
        {"id": "klein-2", "name": "Anlage"},
    ]
    transport = _Elasticsearch(behalten=True)
    indexer = _indexer(transport)

    assert await indexer.index_documents("files", docs) is True
    await indexer._client.aclose()  # type: ignore[union-attr]

    anfragen = [_dokumente(koerper) for koerper in transport.koerper]
    assert [d["id"] for anfrage in anfragen for d in anfrage] == [d["id"] for d in docs]
    assert len(anfragen) >= 4
    for anfrage, groesse, kopf in zip(anfragen, transport.groessen, transport.kopfzeilen, strict=True):
        grosse = [d["id"] for d in anfrage if d["id"].startswith(("gross", "riesig"))]
        assert len(grosse) <= 1, f"mehrere große Dokumente in einer Anfrage: {grosse}"
        assert groesse <= GRENZE or len(anfrage) == 1, "über der Grenze nur ein einzelnes Dokument"
        assert kopf["content-length"] == str(groesse)
        assert "transfer-encoding" not in kopf
    # Umlaute als UTF-8 (2 Bytes), nicht als \u-Folge (6 Bytes)
    assert "ä".encode() in transport.koerper[0] and b"\\u00e4" not in transport.koerper[0]


async def test_anzahl_je_anfrage_begrenzt() -> None:
    transport = _Elasticsearch(behalten=True)
    indexer = _indexer(transport)

    docs = [{"id": f"p{n}", "name": "Antrag"} for n in range(1201)]
    assert await indexer.index_documents("papers", docs) is True
    await indexer._client.aclose()  # type: ignore[union-attr]

    assert [len(_dokumente(koerper)) for koerper in transport.koerper] == [500, 500, 201]


@pytest.fixture
def ohne_speicherbereinigung() -> Iterator[None]:
    """Speichermessung ohne zyklische Speicherbereinigung: Was nach dem Aufruf noch lebt, bleibt sichtbar."""
    gc.collect()
    gc.disable()
    tracemalloc.start()
    try:
        yield
    finally:
        tracemalloc.stop()
        gc.enable()
        gc.collect()


async def test_speicher_beim_schreiben_haengt_nicht_an_der_zahl_der_dokumente(ohne_speicherbereinigung: None) -> None:
    from src.indexing.document_builders import FILE_TEXT_MAX_CHARS, file_to_doc
    from src.indexing.elasticsearch import _bulk_entry

    # Dokumente in der größten Form, die der Index annimmt: voller Volltext aus Umlauten (je rund 10 MB in UTF-8),
    # alle mit demselben Text, damit nur die Verarbeitung zählt
    text = "ä" * FILE_TEXT_MAX_CHARS
    docs = [{**file_to_doc(_datei(text)), "id": f"gross-{n}"} for n in range(8)]
    einzeln = sum(len(teil) for teil in _bulk_entry("files", docs[0]))
    ladung = einzeln * len(docs)
    transport = _Elasticsearch()
    indexer = _indexer(transport)

    vorher = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    assert await indexer.index_documents("files", docs) is True
    nachher, spitze = tracemalloc.get_traced_memory()
    await indexer._client.aclose()  # type: ignore[union-attr]

    assert len(transport.groessen) == len(docs), "je großes Dokument eine Anfrage"
    assert max(transport.groessen) <= max(GRENZE, einzeln)
    # Begrenzt durch ein Vielfaches der größten Anfrage, nicht durch die ganze Ladung: Eine Rückkehr zu einer
    # Anfrage aus allen Dokumenten hielte die Ladung mindestens einmal im Speicher (vorher dreimal, als Zeilen,
    # Text und Bytes) und überschritte die Grenze deutlich.
    grenze = 5 * max(GRENZE, einzeln)
    assert grenze < ladung
    assert spitze - vorher < grenze, f"Spitze {(spitze - vorher) / 2**20:.0f} MiB, Grenze {grenze / 2**20:.0f} MiB"
    # httpx hält die gesendeten Bytes nicht über einen Zyklus fest, bis die Speicherbereinigung läuft
    assert nachher - vorher < 1024 * 1024, f"nach dem Senden noch {(nachher - vorher) / 2**20:.1f} MiB belegt"


def test_steuerzeichen_vergroessern_das_dokument_nicht() -> None:
    from src.indexing.document_builders import file_to_doc
    from src.indexing.elasticsearch import _bulk_entry

    doc = file_to_doc(_datei("A\x00B\x01C\tD\nE\rF\x1f" + "\x01😀" * 1000))
    assert doc["text_content"] == "ABC\tD\nE\rF" + "😀" * 1000
    # Höchstens vier Bytes je Zeichen des Textes (statt sechs für jedes maskierte Steuerzeichen)
    _, quelle, _ = _bulk_entry("files", doc)
    assert len(quelle) < 4 * 2000 + 500


# ---------------------------------------------------------------------------
# Volltext je Datei gekürzt
# ---------------------------------------------------------------------------


def _datei(text_content: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        body_id=uuid.uuid4(),
        name="Anlage",
        file_name="anlage.txt",
        mime_type="text/plain",
        text_content=text_content,
        paper_id=None,
        meeting_id=None,
        oparl_modified=None,
    )


def test_volltext_je_datei_gekuerzt() -> None:
    from src.indexing.document_builders import FILE_TEXT_MAX_CHARS, file_to_doc

    assert FILE_TEXT_MAX_CHARS == 5_000_000  # wie INDEX_TEXT_MAX_CHARS in Django (search_documents.py)
    doc = file_to_doc(_datei("ä" * (FILE_TEXT_MAX_CHARS + 10)))
    assert len(doc["text_content"]) == FILE_TEXT_MAX_CHARS
    assert doc["text_preview"] == "ä" * 500 + "..."
    kurz = file_to_doc(_datei("Beschluss"))
    assert (kurz["text_content"], kurz["text_preview"]) == ("Beschluss", "Beschluss")


class _Indexer:
    docs: list[dict[str, Any]] = []

    async def __aenter__(self) -> _Indexer:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def is_healthy(self) -> bool:
        return True

    async def missing_indices(self) -> set[str]:
        return set()

    async def index_documents(self, index: str, docs: list[dict[str, Any]]) -> bool:
        if index == "files":
            self.docs.extend(docs)
        return True


class _Speicher:
    def __init__(self, text_content: str) -> None:
        self.aufrufe: list[dict[str, Any]] = []
        self.datei = _datei(text_content)

    async def iter_for_body(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[list[Any]]:
        return
        yield []

    async def iter_files_with_text(self, body_id: uuid.UUID, page_size: int | None = None, **kwargs: Any) -> Any:
        self.aufrufe.append(kwargs)
        yield [self.datei]


async def test_sync_laedt_dateitexte_gekuerzt(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.indexing.elasticsearch as es_modul
    from src.indexing.document_builders import FILE_TEXT_MAX_CHARS
    from src.sync.orchestrator import SyncOrchestrator

    _Indexer.docs = []
    monkeypatch.setattr(es_modul, "ElasticsearchIndexer", _Indexer)
    speicher = _Speicher("x" * (FILE_TEXT_MAX_CHARS + 10))
    orchestrator = SyncOrchestrator.__new__(SyncOrchestrator)
    orchestrator.storage = speicher  # type: ignore[assignment]
    stats: dict[str, Any] = {}

    await orchestrator._index_body_elasticsearch(uuid.uuid4(), stats, {}, full=True)

    assert "errors" not in stats
    assert [aufruf.get("max_chars") for aufruf in speicher.aufrufe] == [FILE_TEXT_MAX_CHARS]
    assert [len(doc["text_content"]) for doc in _Indexer.docs] == [FILE_TEXT_MAX_CHARS]


# ---------------------------------------------------------------------------
# Dateitexte seitenweise nach Bytes aus PostgreSQL
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("INGESTOR_TEST_DATABASE_URL", "")
SCHEMA = f"ingestor_bytegrenze_{uuid.uuid4().hex[:12]}"
BASE = "https://ris.example.org/oparl"


def _engine() -> AsyncEngine:
    url = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, connect_args={"server_settings": {"search_path": SCHEMA}})


async def _schema(anlegen: bool) -> None:
    engine = _engine()
    async with engine.begin() as conn:
        if anlegen:
            await conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
            await conn.run_sync(Base.metadata.create_all)
        else:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    await engine.dispose()


@pytest.fixture(scope="module")
def schema() -> Iterator[None]:
    if not DATABASE_URL:
        if os.environ.get("CI"):
            pytest.fail("INGESTOR_TEST_DATABASE_URL fehlt: In der CI müssen diese Tests laufen.")
        pytest.skip("braucht PostgreSQL (INGESTOR_TEST_DATABASE_URL)")
    asyncio.run(_schema(anlegen=True))
    yield
    asyncio.run(_schema(anlegen=False))


@pytest.fixture
async def speicher(schema: None) -> AsyncIterator[tuple[DatabaseStorage, uuid.UUID]]:
    from src.sync.processor import OParlProcessor

    storage = DatabaseStorage(DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1), events_enabled=False)
    await storage._engine.dispose()
    storage._engine = _engine()
    storage._session_factory = async_sessionmaker(storage._engine, class_=AsyncSession, expire_on_commit=False)
    async with storage._engine.begin() as conn:
        tabellen = ", ".join(f'"{tabelle.name}"' for tabelle in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tabellen} CASCADE"))
    source_id = await storage.upsert_source(f"{BASE}/system", "Musterstadt")
    body = OParlProcessor().process_body(
        {"id": f"{BASE}/body/1", "type": "https://schema.oparl.org/1.1/Body", "name": "Musterstadt"}, f"{BASE}/body/1"
    )
    body_id = await storage.upsert_body(body, source_id)
    yield storage, body_id
    await storage.close()


async def _datei_anlegen(storage: DatabaseStorage, body_id: uuid.UUID, nummer: int, text_content: str) -> uuid.UUID:
    file_id = uuid.UUID(int=nummer + 1)  # Reihenfolge nach Kennung wie beim Lesen
    async with storage.get_session() as session:
        await session.execute(
            pg_insert(OParlFile).values(
                id=file_id,
                external_id=f"{BASE}/file/{nummer}",
                body_id=body_id,
                name=f"Anlage {nummer}",
                file_name=f"anlage-{nummer}.txt",
                mime_type="text/plain",
                raw_json={},
                text_content=text_content,
                text_extraction_status="completed",
            )
        )
        await session.commit()
    return file_id


@pytest.mark.integration
async def test_dateitexte_kommen_nach_bytes_und_gekuerzt(speicher: tuple[DatabaseStorage, uuid.UUID]) -> None:
    from src.indexing.document_builders import file_to_doc

    storage, body_id = speicher
    budget = 4 * 1024 * 1024
    kappe = 1_000_000
    texte = ["kurz"] * 3 + ["x" * 3_000_000] * 3 + ["kurz"] * 2 + ["ä" * 2_500_000]
    ids = [await _datei_anlegen(storage, body_id, nummer, wert) for nummer, wert in enumerate(texte)]

    seiten = [rows async for rows in storage.iter_files_with_text(body_id, 500, max_chars=kappe, max_bytes=budget)]

    assert [row.id for seite in seiten for row in seite] == ids
    assert len(seiten) > 1
    # Kleine Seiten: Keyset über mehrere Seiten, jede Datei genau einmal
    kleine_seiten = storage.iter_files_with_text(body_id, 2, max_chars=kappe, max_bytes=budget)
    assert [row.id async for rows in kleine_seiten for row in rows] == ids
    for seite in seiten:
        assert sum(len(row.text_content.encode()) for row in seite) <= budget or len(seite) == 1
        assert sum(len(row.text_content) >= kappe for row in seite) <= 1, "höchstens ein großer Text je Seite"
    zeilen = {row.id: row for seite in seiten for row in seite}
    assert [len(zeilen[kennung].text_content) for kennung in ids] == [min(len(t), kappe) for t in texte]
    # Die Zeilen tragen alles, was das Suchdokument braucht
    doc = file_to_doc(zeilen[ids[0]])
    assert (doc["name"], doc["file_name"], doc["mime_type"], doc["text_content"]) == (
        "Anlage 0",
        "anlage-0.txt",
        "text/plain",
        "kurz",
    )

    # Ohne Kürzung der volle Text, Seiten weiterhin nach Bytes
    voll = [rows async for rows in storage.iter_files_with_text(body_id, 500, max_bytes=budget)]
    assert [len(row.text_content) for seite in voll for row in seite] == [len(t) for t in texte]
    for seite in voll:
        assert sum(len(row.text_content.encode()) for row in seite) <= budget or len(seite) == 1

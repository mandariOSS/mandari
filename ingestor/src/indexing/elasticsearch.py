"""
Async Elasticsearch Client for the Ingestor.

Uses httpx directly (no elasticsearch-py dependency needed).
Indexes entities after sync + text extraction.

Vollständige Suchdokumente baut allein Django (``insight_core/services/search_documents.py``). Der Ingestor
schreibt per ``update`` mit ``doc_as_upsert`` nur die Felder, die er selbst kennt. Felder, die nur Django setzt
(u. a. ``organization_names`` für den Gremienfilter, ``access_url``, Vorgangs- und Sitzungsangaben an Dateien),
bleiben bei einem Sync erhalten statt überschrieben zu werden (Issue #429).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

#: Höchstens so viele Bytes bzw. Dokumente je Bulk-Anfrage. Die Bytegrenze ist dieselbe wie im Abonnement
#: ``suchindex`` (Django, ``insight_search.abonnement.BULK_BYTES``). Ein Dokument, das nicht mehr passt, beginnt
#: eine neue Anfrage; nur ein einzelnes Dokument kann darüber liegen (wie groß, begrenzt
#: ``document_builders.FILE_TEXT_MAX_CHARS``).
BULK_MAX_BYTES: Final = 8 * 1024 * 1024
BULK_MAX_DOCUMENTS: Final = 500

# Die Indizes samt Mappings und Analyzern legt ausschließlich Django an
# (``manage.py setup_elasticsearch``, insight_search). Der Ingestor kennt nur die
# Namen und schreibt Dokumente hinein. Er darf weder Indizes anlegen noch Mappings
# ändern: Ein hier gepflegtes Zweit-Mapping driftete von Djangos Analyzern
# (german_custom/german_search mit Synonymen) ab, und ein vom Ingestor angelegter
# Index kannte diese Analyzer gar nicht (Issue #215). Ob die Namen zu Django
# passen, prüft scripts/check_schema_contract.py.
INDEX_NAMES: tuple[str, ...] = ("papers", "meetings", "persons", "organizations", "files")


def _bulk_entry(index_name: str, doc: dict[str, Any]) -> tuple[bytes, bytes, bytes]:
    """
    Aktion und Teildokument als NDJSON-Zeilen, gleich als UTF-8-Bytes.

    ``ensure_ascii=False``: Umlaute kosten 2 statt 6 Bytes. Ein einzelnes Surrogat (in Daten aus der Datenbank
    nicht zu erwarten) wird wie bisher als ``\\uXXXX`` geschrieben statt den Aufruf scheitern zu lassen.
    """
    action = {"update": {"_index": index_name, "_id": str(doc["id"]), "retry_on_conflict": 3}}
    source = json.dumps({"doc": doc, "doc_as_upsert": True}, ensure_ascii=False).encode("utf-8", "backslashreplace")
    return json.dumps(action).encode() + b"\n", source, b"\n"


async def _once(body: list[bytes]) -> AsyncIterator[bytes]:
    """Gibt den Körper einmal ab und behält danach keinen Verweis (siehe ``ElasticsearchIndexer._send_bulk``)."""
    while body:
        yield body.pop()


class ElasticsearchIndexer:
    """
    Async Elasticsearch client using httpx.

    Usage as async context manager:
        async with ElasticsearchIndexer() as indexer:
            await indexer.index_documents("papers", docs)
    """

    def __init__(
        self,
        url: str | None = None,
    ) -> None:
        self.url = (url or settings.elasticsearch_url).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._missing: set[str] = set()

    async def __aenter__(self) -> ElasticsearchIndexer:
        self._client = httpx.AsyncClient(
            base_url=self.url,
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def is_healthy(self) -> bool:
        """Check if Elasticsearch is reachable."""
        if not self._client:
            return False
        try:
            response = await self._client.get("/")
            return response.status_code == 200
        except Exception:
            return False

    async def missing_indices(self) -> set[str]:
        """Prüft, welche der bekannten Indizes fehlen, und merkt sie sich.

        Fehlende Indizes werden nicht angelegt; das ist Aufgabe von Django
        (``manage.py setup_elasticsearch``). Schreibzugriffe auf fehlende Indizes
        werden anschließend übersprungen, damit Elasticsearch sie nicht mit einem
        dynamischen Mapping ohne die deutschen Analyzer auto-anlegt.
        """
        if not self._client:
            logger.warning("Elasticsearch client not initialized, skipping index check")
            return set()

        missing: set[str] = set()
        for index_name in INDEX_NAMES:
            try:
                resp = await self._client.head(f"/{index_name}")
            except Exception as e:
                logger.warning("Error checking index '%s': %s", index_name, e)
                missing.add(index_name)
                continue
            if resp.status_code != 200:
                missing.add(index_name)
        if missing:
            logger.warning(
                "Elasticsearch indices missing: %s. Run `manage.py setup_elasticsearch` on the "
                "Django side; the ingestor does not create indices (see issue #215).",
                ", ".join(sorted(missing)),
            )
        self._missing = missing
        return missing

    def _skip_missing(self, index_name: str, action: str) -> bool:
        if index_name in self._missing:
            logger.warning("Skipping %s: index '%s' does not exist (Django creates it)", action, index_name)
            return True
        return False

    async def index_documents(
        self,
        index_name: str,
        documents: list[dict[str, Any]],
    ) -> bool:
        """
        Add or update documents in an Elasticsearch index using bulk API.

        Partielles Update (``update`` mit ``doc_as_upsert``): Vorhandene Dokumente behalten alle Felder, die
        ``documents`` nicht enthält – insbesondere die nur von Django gesetzten. Fehlt ein Dokument, wird es mit
        den übergebenen Feldern angelegt. Unveränderte Dokumente erkennt Elasticsearch als No-op.

        Die Dokumente gehen in Anfragen von höchstens ``BULK_MAX_BYTES`` Bytes bzw. ``BULK_MAX_DOCUMENTS``
        Dokumenten hinaus; ein Dokument, das nicht mehr passt, beginnt die nächste Anfrage. So hängt der
        Speicherbedarf nicht von der Zahl oder Größe der Dokumente ab, und mehrere große Dokumente liegen nie
        zusammen in einer Anfrage.

        Args:
            index_name: The index to write to (e.g. "papers", "files")
            documents: List of documents with "id" field

        Returns:
            True if all requests were accepted, False on error
        """
        if not documents:
            return True

        if not self._client:
            logger.warning("Elasticsearch client not initialized")
            return False
        if self._skip_missing(index_name, "indexing"):
            return False

        accepted = True
        try:
            parts: list[bytes] = []
            size = count = 0
            for doc in documents:
                entry = _bulk_entry(index_name, doc)
                entry_size = sum(len(part) for part in entry)
                if parts and (size + entry_size > BULK_MAX_BYTES or count >= BULK_MAX_DOCUMENTS):
                    accepted = await self._send_bulk(index_name, parts, count) and accepted
                    size = count = 0
                parts.extend(entry)
                size += entry_size
                count += 1
                del entry
            if parts:
                accepted = await self._send_bulk(index_name, parts, count) and accepted
        except Exception as e:
            logger.warning("Elasticsearch indexing error: %s", e)
            return False
        return accepted

    async def _send_bulk(self, index_name: str, parts: list[bytes], count: int) -> bool:
        """
        Sendet eine Bulk-Anfrage aus ``parts`` und leert die Liste.

        Der Körper liegt danach nur noch einmal vor und geht als einmaliger Strom mit fester Länge hinaus:
        Mit ``content=bytes`` behielte die Anfrage die Bytes, und httpx hält Anfrage und Antwort in einem
        Referenzzyklus, sodass jede gesendete Anfrage bis zur zyklischen Speicherbereinigung im Speicher bliebe.
        """
        body = [b"".join(parts)]
        parts.clear()
        length = len(body[0])
        if self._client is None:
            return False
        response = await self._client.post(
            "/_bulk",
            content=_once(body),
            headers={"Content-Type": "application/x-ndjson", "Content-Length": str(length)},
        )
        if response.status_code == 200:
            result = response.json()
            if result.get("errors"):
                error_count = sum(
                    1 for item in result.get("items", []) for outcome in item.values() if "error" in outcome
                )
                logger.warning("Elasticsearch bulk indexing: %d errors out of %d", error_count, count)
            else:
                logger.debug("Indexed %d documents in '%s' (%d bytes)", count, index_name, length)
            return True
        logger.warning(
            "Elasticsearch indexing failed: %d %s",
            response.status_code,
            response.text[:200],
        )
        return False

    async def delete_documents(
        self,
        index_name: str,
        doc_ids: list[str],
    ) -> bool:
        """
        Delete documents from an index by ID (bulk API, missing docs ignored).

        Used for tombstones: entities marked as deleted by the source are
        removed from the search index (the DB row is kept, only flagged).

        Args:
            index_name: The index to delete from (e.g. "papers", "files")
            doc_ids: List of document IDs (entity UUIDs as strings)

        Returns:
            True if the request was accepted, False on error
        """
        if not doc_ids:
            return True

        if not self._client:
            logger.warning("Elasticsearch client not initialized")
            return False
        if self._skip_missing(index_name, "delete"):
            return False

        try:
            lines = [f'{{"delete":{{"_index":"{index_name}","_id":"{doc_id}"}}}}' for doc_id in doc_ids]
            bulk_body = "\n".join(lines) + "\n"

            response = await self._client.post(
                "/_bulk",
                content=bulk_body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            if response.status_code == 200:
                logger.debug("Deleted %d documents from '%s'", len(doc_ids), index_name)
                return True
            logger.warning(
                "Elasticsearch bulk delete failed: %d %s",
                response.status_code,
                response.text[:200],
            )
            return False
        except Exception as e:
            logger.warning("Elasticsearch delete error: %s", e)
            return False

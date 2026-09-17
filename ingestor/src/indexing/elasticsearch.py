"""
Async Elasticsearch Client for the Ingestor.

Uses httpx directly (no elasticsearch-py dependency needed).
Indexes entities after sync + text extraction.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

# Die Indizes samt Mappings und Analyzern legt ausschließlich Django an
# (``manage.py setup_elasticsearch``, insight_search). Der Ingestor kennt nur die
# Namen und schreibt Dokumente hinein. Er darf weder Indizes anlegen noch Mappings
# ändern: Ein hier gepflegtes Zweit-Mapping driftete von Djangos Analyzern
# (german_custom/german_search mit Synonymen) ab, und ein vom Ingestor angelegter
# Index kannte diese Analyzer gar nicht (Issue #215). Ob die Namen zu Django
# passen, prüft scripts/check_schema_contract.py.
INDEX_NAMES: tuple[str, ...] = ("papers", "meetings", "persons", "organizations", "files")


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

        Args:
            index_name: The index to write to (e.g. "papers", "files")
            documents: List of documents with "id" field

        Returns:
            True if the request was accepted, False on error
        """
        if not documents:
            return True

        if not self._client:
            logger.warning("Elasticsearch client not initialized")
            return False
        if self._skip_missing(index_name, "indexing"):
            return False

        try:
            # NDJSON bulk format
            lines = []
            for doc in documents:
                lines.append(f'{{"index":{{"_index":"{index_name}","_id":"{doc["id"]}"}}}}')
                import json

                lines.append(json.dumps(doc))
            bulk_body = "\n".join(lines) + "\n"

            response = await self._client.post(
                "/_bulk",
                content=bulk_body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            if response.status_code == 200:
                result = response.json()
                if result.get("errors"):
                    error_count = sum(1 for item in result.get("items", []) if "error" in item.get("index", {}))
                    logger.warning("Elasticsearch bulk indexing: %d errors out of %d", error_count, len(documents))
                else:
                    logger.debug("Indexed %d documents in '%s'", len(documents), index_name)
                return True
            logger.warning(
                "Elasticsearch indexing failed: %d %s",
                response.status_code,
                response.text[:200],
            )
            return False
        except Exception as e:
            logger.warning("Elasticsearch indexing error: %s", e)
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

    async def delete_index(self, index_name: str) -> bool:
        """Delete all documents in an index."""
        if not self._client:
            return False
        try:
            response = await self._client.post(
                f"/{index_name}/_delete_by_query",
                json={"query": {"match_all": {}}},
            )
            return response.status_code == 200
        except Exception as e:
            logger.warning("Elasticsearch delete error: %s", e)
            return False

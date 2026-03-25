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

# Index mappings mirroring setup_elasticsearch.py (Django command).
# Synonyms are NOT included here — they require Django's insight_search.synonyms.
INDEX_MAPPINGS: dict[str, dict[str, Any]] = {
    "papers": {
        "properties": {
            "id": {"type": "keyword"},
            "type": {"type": "keyword"},
            "body_id": {"type": "keyword"},
            "name": {"type": "text", "analyzer": "german"},
            "reference": {"type": "text", "analyzer": "standard", "fields": {"keyword": {"type": "keyword"}}},
            "paper_type": {"type": "keyword"},
            "date": {"type": "date", "format": "strict_date_optional_time||yyyy-MM-dd", "ignore_malformed": True},
            "oparl_created": {"type": "date", "ignore_malformed": True},
            "oparl_modified": {"type": "date", "ignore_malformed": True},
            "file_contents_preview": {"type": "text", "analyzer": "german"},
            "file_names": {"type": "text"},
        }
    },
    "meetings": {
        "properties": {
            "id": {"type": "keyword"},
            "type": {"type": "keyword"},
            "body_id": {"type": "keyword"},
            "name": {"type": "text", "analyzer": "german"},
            "organization_names": {"type": "text", "analyzer": "german"},
            "location_name": {"type": "text"},
            "start": {"type": "date", "ignore_malformed": True},
            "end": {"type": "date", "ignore_malformed": True},
            "cancelled": {"type": "boolean"},
            "oparl_modified": {"type": "date", "ignore_malformed": True},
        }
    },
    "persons": {
        "properties": {
            "id": {"type": "keyword"},
            "type": {"type": "keyword"},
            "body_id": {"type": "keyword"},
            "name": {"type": "text", "analyzer": "german"},
            "given_name": {"type": "text"},
            "family_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "title": {"type": "text"},
            "oparl_modified": {"type": "date", "ignore_malformed": True},
        }
    },
    "organizations": {
        "properties": {
            "id": {"type": "keyword"},
            "type": {"type": "keyword"},
            "body_id": {"type": "keyword"},
            "name": {"type": "text", "analyzer": "german", "fields": {"keyword": {"type": "keyword"}}},
            "short_name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
            "organization_type": {"type": "keyword"},
            "classification": {"type": "keyword"},
            "oparl_modified": {"type": "date", "ignore_malformed": True},
        }
    },
    "files": {
        "properties": {
            "id": {"type": "keyword"},
            "type": {"type": "keyword"},
            "body_id": {"type": "keyword"},
            "name": {"type": "text", "analyzer": "german"},
            "file_name": {"type": "text"},
            "mime_type": {"type": "keyword"},
            "text_content": {"type": "text", "analyzer": "german"},
            "text_preview": {"type": "text", "index": False},
            "paper_id": {"type": "keyword"},
            "paper_name": {"type": "text", "analyzer": "german"},
            "paper_reference": {"type": "text", "analyzer": "standard"},
            "meeting_id": {"type": "keyword"},
            "organization_names": {"type": "text", "analyzer": "german"},
            "meeting_name": {"type": "text"},
            "meeting_date": {"type": "date", "ignore_malformed": True},
            "agenda_number": {"type": "keyword"},
            "oparl_modified": {"type": "date", "ignore_malformed": True},
        }
    },
}

INDEX_SETTINGS: dict[str, Any] = {
    "number_of_shards": 1,
    "number_of_replicas": 0,
}


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

    async def ensure_index_settings(self) -> None:
        """Configure all index settings (idempotent).

        Creates indices with proper mappings if they don't exist.
        """
        if not self._client:
            logger.warning("Elasticsearch client not initialized, skipping settings")
            return

        for index_name, mappings in INDEX_MAPPINGS.items():
            try:
                # Prüfen ob Index existiert
                resp = await self._client.head(f"/{index_name}")
                if resp.status_code == 200:
                    # Index existiert — Mappings aktualisieren
                    resp = await self._client.put(
                        f"/{index_name}/_mapping",
                        json=mappings,
                    )
                    if resp.status_code == 200:
                        logger.info("Index mappings updated for '%s'", index_name)
                    else:
                        logger.warning(
                            "Failed to update mappings on %s: %d %s",
                            index_name, resp.status_code, resp.text[:200],
                        )
                else:
                    # Neuen Index erstellen
                    resp = await self._client.put(
                        f"/{index_name}",
                        json={
                            "settings": INDEX_SETTINGS,
                            "mappings": mappings,
                        },
                    )
                    if resp.status_code == 200:
                        logger.info("Index '%s' created", index_name)
                    else:
                        logger.warning(
                            "Failed to create index %s: %d %s",
                            index_name, resp.status_code, resp.text[:200],
                        )
            except Exception as e:
                logger.warning("Error configuring index '%s': %s", index_name, e)

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
            else:
                logger.warning(
                    "Elasticsearch indexing failed: %d %s",
                    response.status_code,
                    response.text[:200],
                )
                return False
        except Exception as e:
            logger.warning("Elasticsearch indexing error: %s", e)
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

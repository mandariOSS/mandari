"""
Async Meilisearch Client for the Ingestor.

Uses httpx directly (no meilisearch-python dependency needed).
Indexes entities after sync + text extraction.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


class MeilisearchIndexer:
    """
    Async Meilisearch client using httpx.

    Usage as async context manager:
        async with MeilisearchIndexer() as indexer:
            await indexer.index_documents("papers", docs)
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.url = (url or settings.meilisearch_url).rstrip("/")
        self.api_key = api_key or settings.meilisearch_key
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> MeilisearchIndexer:
        self._client = httpx.AsyncClient(
            base_url=self.url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def is_healthy(self) -> bool:
        """Check if Meilisearch is reachable."""
        if not self._client:
            return False
        try:
            response = await self._client.get("/health")
            return response.status_code == 200
        except Exception:
            return False

    async def index_documents(
        self,
        index_name: str,
        documents: list[dict[str, Any]],
    ) -> bool:
        """
        Add or update documents in a Meilisearch index.

        Args:
            index_name: The index to write to (e.g. "papers", "files")
            documents: List of documents with "id" field

        Returns:
            True if the request was accepted, False on error
        """
        if not documents:
            return True

        if not self._client:
            logger.warning("Meilisearch client not initialized")
            return False

        try:
            response = await self._client.post(
                f"/indexes/{index_name}/documents",
                json=documents,
            )
            if response.status_code in (200, 202):
                logger.debug(
                    "Indexed %d documents in '%s'", len(documents), index_name
                )
                return True
            else:
                logger.warning(
                    "Meilisearch indexing failed: %d %s",
                    response.status_code,
                    response.text[:200],
                )
                return False
        except Exception as e:
            logger.warning("Meilisearch indexing error: %s", e)
            return False

    async def delete_index(self, index_name: str) -> bool:
        """Delete all documents in an index."""
        if not self._client:
            return False
        try:
            response = await self._client.delete(
                f"/indexes/{index_name}/documents"
            )
            return response.status_code in (200, 202)
        except Exception as e:
            logger.warning("Meilisearch delete error: %s", e)
            return False

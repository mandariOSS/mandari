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

# Index settings mirroring setup_meilisearch.py (Django command).
# Synonyms are NOT included here — they require Django's insight_search.synonyms.
INDEX_SETTINGS: dict[str, dict[str, Any]] = {
    "papers": {
        "searchableAttributes": ["name", "reference", "paper_type", "file_contents_preview", "file_names"],
        "filterableAttributes": ["body_id", "paper_type", "date"],
        "sortableAttributes": ["date", "oparl_created", "oparl_modified"],
    },
    "meetings": {
        "searchableAttributes": ["name", "organization_names", "location_name"],
        "filterableAttributes": ["body_id", "cancelled", "start"],
        "sortableAttributes": ["start", "end", "oparl_modified"],
    },
    "persons": {
        "searchableAttributes": ["name", "given_name", "family_name", "title"],
        "filterableAttributes": ["body_id"],
        "sortableAttributes": ["family_name", "given_name", "oparl_modified"],
    },
    "organizations": {
        "searchableAttributes": ["name", "short_name", "organization_type", "classification"],
        "filterableAttributes": ["body_id", "organization_type"],
        "sortableAttributes": ["name", "oparl_modified"],
    },
    "files": {
        "searchableAttributes": ["name", "file_name", "text_content", "paper_name", "paper_reference"],
        "filterableAttributes": ["body_id", "paper_id", "meeting_id", "mime_type"],
        "sortableAttributes": ["oparl_modified"],
    },
}

TYPO_TOLERANCE: dict[str, Any] = {
    "enabled": True,
    "minWordSizeForTypos": {
        "oneTypo": 4,
        "twoTypos": 8,
    },
    "disableOnWords": [],
    "disableOnAttributes": [],
}

RANKING_RULES: list[str] = [
    "words",
    "typo",
    "proximity",
    "attribute",
    "sort",
    "exactness",
]

EMBEDDER_TEMPLATES: dict[str, dict[str, Any]] = {
    "papers": {
        "documentTemplate": "{{ doc.name }} {{ doc.reference }} {{ doc.paper_type }} {{ doc.file_contents_preview }}",
        "documentTemplateMaxBytes": 2048,
    },
    "files": {
        "documentTemplate": "{{ doc.name }} {{ doc.file_name }} {{ doc.text_content }}",
        "documentTemplateMaxBytes": 4096,
    },
    "meetings": {
        "documentTemplate": "{{ doc.name }} {{ doc.organization_names }} {{ doc.location_name }}",
        "documentTemplateMaxBytes": 400,
    },
    "persons": {
        "documentTemplate": "{{ doc.name }} {{ doc.given_name }} {{ doc.family_name }}",
        "documentTemplateMaxBytes": 400,
    },
    "organizations": {
        "documentTemplate": "{{ doc.name }} {{ doc.short_name }} {{ doc.organization_type }} {{ doc.classification }}",
        "documentTemplateMaxBytes": 400,
    },
}


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

    async def ensure_index_settings(self) -> None:
        """Configure all index settings (idempotent).

        Sets searchable/filterable/sortable attributes, typo tolerance,
        ranking rules, and embedders for every index. Safe to call repeatedly —
        Meilisearch accepts duplicate PUT requests without error.
        """
        if not self._client:
            logger.warning("Meilisearch client not initialized, skipping settings")
            return

        # Enable vector store only when semantic search is active
        use_embedders = settings.meilisearch_semantic_ratio > 0
        if use_embedders:
            try:
                resp = await self._client.patch(
                    "/experimental-features",
                    json={"vectorStore": True},
                )
                if resp.status_code == 200:
                    logger.info("Vector store experimental feature enabled")
                else:
                    logger.warning("Failed to enable vector store: %d %s", resp.status_code, resp.text[:200])
            except Exception as e:
                logger.warning("Failed to enable vector store: %s", e)

        for index_name, cfg in INDEX_SETTINGS.items():
            try:
                # Settings endpoints accept PUT and return a task
                for key in ("searchableAttributes", "filterableAttributes", "sortableAttributes"):
                    resp = await self._client.put(
                        f"/indexes/{index_name}/settings/{self._camel_to_kebab(key)}",
                        json=cfg[key],
                    )
                    if resp.status_code not in (200, 202):
                        logger.warning(
                            "Failed to set %s on %s: %d %s",
                            key, index_name, resp.status_code, resp.text[:200],
                        )

                # Typo tolerance
                resp = await self._client.put(
                    f"/indexes/{index_name}/settings/typo-tolerance",
                    json=TYPO_TOLERANCE,
                )
                if resp.status_code not in (200, 202):
                    logger.warning(
                        "Failed to set typo-tolerance on %s: %d",
                        index_name, resp.status_code,
                    )

                # Ranking rules
                resp = await self._client.put(
                    f"/indexes/{index_name}/settings/ranking-rules",
                    json=RANKING_RULES,
                )
                if resp.status_code not in (200, 202):
                    logger.warning(
                        "Failed to set ranking-rules on %s: %d",
                        index_name, resp.status_code,
                    )

                # Embedder for hybrid search (Meilisearch v1.10+) — only when enabled
                if use_embedders and index_name in EMBEDDER_TEMPLATES:
                    template = EMBEDDER_TEMPLATES[index_name]
                    embedder_payload = {
                        "default": {
                            "source": "huggingFace",
                            "model": settings.meilisearch_embedding_model,
                            **template,
                        }
                    }
                    resp = await self._client.put(
                        f"/indexes/{index_name}/settings/embedders",
                        json=embedder_payload,
                    )
                    if resp.status_code not in (200, 202):
                        logger.warning(
                            "Failed to set embedders on %s: %d %s",
                            index_name, resp.status_code, resp.text[:200],
                        )
                    else:
                        logger.info("Embedder 'default' configured for '%s'", index_name)

                logger.info("Index settings configured for '%s'", index_name)
            except Exception as e:
                logger.warning("Error configuring index '%s': %s", index_name, e)

    @staticmethod
    def _camel_to_kebab(name: str) -> str:
        """Convert camelCase to kebab-case for Meilisearch REST endpoints."""
        import re
        return re.sub(r"(?<=[a-z0-9])([A-Z])", r"-\1", name).lower()

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

"""
Meilisearch Service

Provides search functionality using Meilisearch for full-text search
across meetings, papers, persons, and organizations.
"""

import logging
from typing import Any

import meilisearch
from meilisearch.errors import MeilisearchApiError

from src.core.config import settings

logger = logging.getLogger(__name__)

# Index names
INDEX_MEETINGS = "meetings"
INDEX_PAPERS = "papers"
INDEX_PERSONS = "persons"
INDEX_ORGANIZATIONS = "organizations"
INDEX_FILES = "files"

# Index configurations
INDEX_CONFIGS = {
    INDEX_MEETINGS: {
        "primaryKey": "id",
        "searchableAttributes": ["name", "location_name", "location_address"],
        "filterableAttributes": ["body_id", "cancelled", "start"],
        "sortableAttributes": ["start", "name"],
        "displayedAttributes": ["id", "name", "start", "end", "cancelled", "location_name", "body_id", "type"],
    },
    INDEX_PAPERS: {
        "primaryKey": "id",
        "searchableAttributes": ["name", "reference", "paper_type", "summary"],
        "filterableAttributes": ["body_id", "paper_type", "date"],
        "sortableAttributes": ["date", "name", "reference"],
        "displayedAttributes": ["id", "name", "reference", "paper_type", "date", "body_id", "type"],
    },
    INDEX_PERSONS: {
        "primaryKey": "id",
        "searchableAttributes": ["name", "family_name", "given_name", "title", "email"],
        "filterableAttributes": ["body_id", "gender"],
        "sortableAttributes": ["name", "family_name"],
        "displayedAttributes": ["id", "name", "family_name", "given_name", "title", "body_id", "type"],
    },
    INDEX_ORGANIZATIONS: {
        "primaryKey": "id",
        "searchableAttributes": ["name", "short_name", "organization_type", "classification"],
        "filterableAttributes": ["body_id", "organization_type", "start_date", "end_date"],
        "sortableAttributes": ["name", "start_date"],
        "displayedAttributes": ["id", "name", "short_name", "organization_type", "start_date", "end_date", "body_id", "type"],
    },
    INDEX_FILES: {
        "primaryKey": "id",
        "searchableAttributes": ["text_content", "name", "file_name", "paper_name", "paper_reference"],
        "filterableAttributes": ["body_id", "paper_id", "mime_type", "created"],
        "sortableAttributes": ["created", "name"],
        "displayedAttributes": [
            "id", "name", "file_name", "mime_type", "size",
            "paper_id", "paper_name", "paper_reference",
            "body_id", "created", "type", "text_preview"
        ],
    },
}


class SearchService:
    """Service for interacting with Meilisearch."""

    def __init__(self) -> None:
        """Initialize Meilisearch client."""
        self.client = meilisearch.Client(
            settings.meilisearch_url,
            settings.meilisearch_key
        )
        self._initialized = False

    def is_healthy(self) -> bool:
        """Check if Meilisearch is healthy."""
        try:
            health = self.client.health()
            return health.get("status") == "available"
        except Exception as e:
            logger.warning(f"Meilisearch health check failed: {e}")
            return False

    def initialize_indexes(self) -> None:
        """Create and configure all indexes if they don't exist."""
        if self._initialized:
            return

        for index_name, config in INDEX_CONFIGS.items():
            try:
                # Create index if it doesn't exist
                try:
                    self.client.get_index(index_name)
                    logger.info(f"Index '{index_name}' already exists")
                except MeilisearchApiError:
                    logger.info(f"Creating index '{index_name}'")
                    self.client.create_index(index_name, {"primaryKey": config["primaryKey"]})

                # Update index settings
                index = self.client.index(index_name)
                index.update_searchable_attributes(config["searchableAttributes"])
                index.update_filterable_attributes(config["filterableAttributes"])
                index.update_sortable_attributes(config["sortableAttributes"])
                index.update_displayed_attributes(config["displayedAttributes"])

            except Exception as e:
                logger.error(f"Failed to initialize index '{index_name}': {e}")

        self._initialized = True

    def index_document(self, index_name: str, document: dict[str, Any]) -> None:
        """Index a single document."""
        try:
            index = self.client.index(index_name)
            index.add_documents([document])
        except Exception as e:
            logger.error(f"Failed to index document in '{index_name}': {e}")

    def index_documents(self, index_name: str, documents: list[dict[str, Any]]) -> None:
        """Index multiple documents."""
        if not documents:
            return

        try:
            index = self.client.index(index_name)
            # Meilisearch handles batching internally
            index.add_documents(documents)
            logger.info(f"Indexed {len(documents)} documents in '{index_name}'")
        except Exception as e:
            logger.error(f"Failed to index documents in '{index_name}': {e}")

    def delete_document(self, index_name: str, document_id: str) -> None:
        """Delete a document by ID."""
        try:
            index = self.client.index(index_name)
            index.delete_document(document_id)
        except Exception as e:
            logger.error(f"Failed to delete document '{document_id}' from '{index_name}': {e}")

    def search(
        self,
        query: str,
        index_names: list[str] | None = None,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        Search across multiple indexes.

        Args:
            query: Search query string
            index_names: List of indexes to search (default: all)
            body_id: Filter by body/municipality ID
            page: Page number (1-indexed)
            page_size: Number of results per page

        Returns:
            Dict with results, total count, and pagination info
        """
        if index_names is None:
            index_names = [INDEX_MEETINGS, INDEX_PAPERS, INDEX_PERSONS, INDEX_ORGANIZATIONS, INDEX_FILES]

        all_results: list[dict[str, Any]] = []
        total_hits = 0

        # Build filter
        filters = []
        if body_id:
            filters.append(f"body_id = '{body_id}'")
        filter_str = " AND ".join(filters) if filters else None

        # Search each index
        for index_name in index_names:
            try:
                index = self.client.index(index_name)
                search_params: dict[str, Any] = {
                    "limit": page_size * 2,  # Get more to merge
                    "offset": 0,
                }
                if filter_str:
                    search_params["filter"] = filter_str

                result = index.search(query, search_params)

                # Add type to each hit
                for hit in result.get("hits", []):
                    hit["type"] = index_name[:-1]  # Remove 's' (meetings -> meeting)
                    all_results.append(hit)

                total_hits += result.get("estimatedTotalHits", 0)

            except Exception as e:
                logger.error(f"Search failed for index '{index_name}': {e}")

        # Sort by relevance (Meilisearch already sorts by relevance within each index)
        # For multi-index, we'll just interleave results
        all_results.sort(key=lambda x: x.get("_rankingScore", 0), reverse=True)

        # Paginate
        start = (page - 1) * page_size
        end = start + page_size
        paginated = all_results[start:end]

        return {
            "results": paginated,
            "total": total_hits,
            "page": page,
            "page_size": page_size,
            "pages": (total_hits + page_size - 1) // page_size if total_hits > 0 else 0,
        }

    def search_single_index(
        self,
        query: str,
        index_name: str,
        body_id: str | None = None,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Search a single index with advanced options.

        Args:
            query: Search query string
            index_name: Index to search
            body_id: Filter by body/municipality ID
            filters: Additional filters as dict
            page: Page number (1-indexed)
            page_size: Number of results per page
            sort: List of sort expressions (e.g., ["name:asc", "date:desc"])

        Returns:
            Dict with results, total count, and pagination info
        """
        try:
            index = self.client.index(index_name)

            # Build filter
            filter_parts = []
            if body_id:
                filter_parts.append(f"body_id = '{body_id}'")
            if filters:
                for key, value in filters.items():
                    if isinstance(value, list):
                        filter_parts.append(f"{key} IN {value}")
                    else:
                        filter_parts.append(f"{key} = '{value}'")

            search_params: dict[str, Any] = {
                "limit": page_size,
                "offset": (page - 1) * page_size,
            }

            if filter_parts:
                search_params["filter"] = " AND ".join(filter_parts)
            if sort:
                search_params["sort"] = sort

            result = index.search(query, search_params)

            return {
                "results": result.get("hits", []),
                "total": result.get("estimatedTotalHits", 0),
                "page": page,
                "page_size": page_size,
                "pages": (result.get("estimatedTotalHits", 0) + page_size - 1) // page_size,
                "processing_time_ms": result.get("processingTimeMs", 0),
            }

        except Exception as e:
            logger.error(f"Search failed for index '{index_name}': {e}")
            return {
                "results": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "pages": 0,
                "error": str(e),
            }

    def get_stats(self) -> dict[str, Any]:
        """Get statistics for all indexes."""
        stats = {}
        for index_name in INDEX_CONFIGS.keys():
            try:
                index = self.client.index(index_name)
                index_stats = index.get_stats()
                stats[index_name] = {
                    "numberOfDocuments": index_stats.get("numberOfDocuments", 0),
                    "isIndexing": index_stats.get("isIndexing", False),
                }
            except Exception as e:
                stats[index_name] = {"error": str(e)}
        return stats


# Global service instance
_search_service: SearchService | None = None


def get_search_service() -> SearchService:
    """Get or create the search service singleton."""
    global _search_service
    if _search_service is None:
        _search_service = SearchService()
    return _search_service

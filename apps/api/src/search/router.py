"""
Search Router

Admin endpoints for search index management.
"""

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from src.search.indexer import index_all
from src.search.service import get_search_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/search", tags=["search"])


@router.get("/health")
async def search_health() -> dict[str, Any]:
    """Check Meilisearch health status."""
    search_service = get_search_service()

    is_healthy = search_service.is_healthy()
    stats = search_service.get_stats() if is_healthy else {}

    return {
        "status": "healthy" if is_healthy else "unhealthy",
        "meilisearch_available": is_healthy,
        "indexes": stats,
    }


@router.post("/reindex")
async def trigger_reindex(background_tasks: BackgroundTasks) -> dict[str, str]:
    """
    Trigger a full reindex of all data.

    This runs in the background and may take several minutes.
    """
    search_service = get_search_service()

    if not search_service.is_healthy():
        raise HTTPException(
            status_code=503,
            detail="Meilisearch is not available"
        )

    async def do_reindex() -> None:
        try:
            counts = await index_all()
            logger.info(f"Reindexing complete: {counts}")
        except Exception as e:
            logger.error(f"Reindexing failed: {e}")

    background_tasks.add_task(do_reindex)

    return {
        "status": "started",
        "message": "Reindexing started in background. Check /search/health for progress.",
    }


@router.post("/initialize")
async def initialize_indexes() -> dict[str, str]:
    """Initialize search indexes without data."""
    search_service = get_search_service()

    if not search_service.is_healthy():
        raise HTTPException(
            status_code=503,
            detail="Meilisearch is not available"
        )

    search_service.initialize_indexes()

    return {
        "status": "success",
        "message": "Indexes initialized",
    }

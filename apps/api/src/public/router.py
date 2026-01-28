"""
Public API Router

Public endpoints for citizens (Säule 1: Transparenz).
"""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.oparl.models import OParlBody
from src.search.service import get_search_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/public", tags=["public"])


@router.get("/")
async def public_index() -> dict:
    """Public API index - shows available endpoints."""
    return {
        "message": "Willkommen zur Mandari Public API",
        "description": "API für kommunalpolitische Transparenz",
        "endpoints": {
            "bodies": "/api/v1/public/bodies - Liste aller Kommunen",
            "search": "/api/v1/public/search - Volltextsuche",
            "chat": "/api/v1/public/chat - KI-Chatbot (coming soon)",
        },
    }


@router.get("/search")
async def search(
    q: str = Query(..., min_length=2, description="Suchbegriff"),
    body_id: str | None = Query(None, description="Filter nach Kommune"),
    type: str | None = Query(None, description="Filter nach Typ (meeting, paper, person, organization)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict:
    """
    Search across all OParl data.

    Full-text search across meetings, papers, persons, and organizations.
    Uses Meilisearch for fast, typo-tolerant search.
    """
    search_service = get_search_service()

    # Check if Meilisearch is available
    if not search_service.is_healthy():
        logger.warning("Meilisearch is not available, returning empty results")
        return {
            "query": q,
            "body_id": body_id,
            "page": page,
            "page_size": page_size,
            "results": [],
            "total": 0,
            "message": "Suche vorübergehend nicht verfügbar",
        }

    # Determine which indexes to search
    index_names = None
    if type:
        type_to_index = {
            "meeting": "meetings",
            "paper": "papers",
            "person": "persons",
            "organization": "organizations",
        }
        if type in type_to_index:
            index_names = [type_to_index[type]]

    result = search_service.search(
        query=q,
        index_names=index_names,
        body_id=body_id,
        page=page,
        page_size=page_size,
    )

    return {
        "query": q,
        "body_id": body_id,
        "type": type,
        **result,
    }


@router.get("/search/stats")
async def search_stats() -> dict:
    """Get search index statistics."""
    search_service = get_search_service()

    if not search_service.is_healthy():
        return {
            "status": "unavailable",
            "message": "Meilisearch ist nicht erreichbar",
        }

    return {
        "status": "available",
        "indexes": search_service.get_stats(),
    }


@router.get("/bodies")
async def list_public_bodies(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all available bodies/municipalities for public access."""
    result = await db.execute(select(OParlBody).order_by(OParlBody.name))
    bodies = result.scalars().all()

    return {
        "bodies": [
            {
                "id": str(body.id),
                "name": body.name,
                "short_name": body.short_name,
                "website": body.website,
            }
            for body in bodies
        ],
        "total": len(bodies),
    }


@router.post("/chat")
async def chat(
    message: str,
    body_id: str | None = None,
) -> dict:
    """
    AI Chatbot for asking questions about municipal data.

    Coming soon: Integration with Groq/OpenAI for intelligent responses.
    """
    # TODO: Implement AI chatbot
    return {
        "message": message,
        "body_id": body_id,
        "response": "Der KI-Chatbot ist in Entwicklung. Bitte versuchen Sie es später erneut.",
        "status": "coming_soon",
    }


@router.post("/questions")
async def submit_question(
    person_id: str,
    question: str,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict:
    """
    Submit a question to a politician (like AbgeordnetenWatch).

    Coming soon: Integration with Säule 2 for question delivery.
    """
    # TODO: Implement question submission
    return {
        "person_id": person_id,
        "question": question,
        "status": "coming_soon",
        "message": "Die Fragefunktion ist in Entwicklung.",
    }

"""
AI API Router

Endpoints for AI-powered features: summaries, chat, location extraction.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.ai.service import ChatMessage, ai_service
from src.core.database import get_db
from src.oparl.models import OParlAgendaItem, OParlFile, OParlMeeting, OParlPaper

router = APIRouter(prefix="/ai", tags=["ai"])


# --- Request/Response Models ---


class SummarizeRequest(BaseModel):
    """Request to summarize a paper or meeting."""
    paper_id: UUID | None = None
    meeting_id: UUID | None = None
    text: str | None = None


class SummarizeResponse(BaseModel):
    """Summary response."""
    summary: str
    source_type: str
    source_id: str | None = None


class ChatRequest(BaseModel):
    """Chat request."""
    message: str
    body_id: UUID | None = None
    context_paper_id: UUID | None = None
    context_meeting_id: UUID | None = None
    history: list[ChatMessage] = []


class ChatResponse(BaseModel):
    """Chat response."""
    response: str
    context_used: bool = False


class LocationsRequest(BaseModel):
    """Request to extract locations."""
    text: str | None = None
    paper_id: UUID | None = None
    city: str = ""


class LocationResponse(BaseModel):
    """A single location."""
    name: str
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class LocationsResponse(BaseModel):
    """Locations response."""
    locations: list[LocationResponse]
    source_id: str | None = None


class SearchParseRequest(BaseModel):
    """Request to parse a natural language search query."""
    query: str


class SearchParseResponse(BaseModel):
    """Parsed search filters."""
    keywords: list[str]
    paper_type: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    organization: str | None = None
    person: str | None = None


# --- Endpoints ---


@router.post("/summarize", response_model=SummarizeResponse)
async def summarize(
    request: SummarizeRequest,
    db: AsyncSession = Depends(get_db),
) -> SummarizeResponse:
    """
    Generate an AI summary for a paper or meeting.

    Provide either:
    - paper_id: UUID of a paper to summarize
    - meeting_id: UUID of a meeting to summarize
    - text: Raw text to summarize
    """
    if request.paper_id:
        # Summarize a paper
        result = await db.execute(
            select(OParlPaper).where(OParlPaper.id == request.paper_id)
        )
        paper = result.scalar_one_or_none()
        if not paper:
            raise HTTPException(status_code=404, detail="Paper not found")

        # Get paper files for text content
        files_result = await db.execute(
            select(OParlFile).where(OParlFile.paper_id == paper.id)
        )
        files = files_result.scalars().all()

        # Combine text from files
        text_content = paper.name or ""
        for file in files:
            if file.text_content:
                text_content += f"\n\n{file.text_content}"

        summary = await ai_service.summarize_paper(
            paper_name=paper.name or "Unbekannte Vorlage",
            paper_text=text_content if len(text_content) > 50 else None,
            paper_type=paper.paper_type,
        )

        return SummarizeResponse(
            summary=summary,
            source_type="paper",
            source_id=str(paper.id),
        )

    elif request.meeting_id:
        # Summarize a meeting
        result = await db.execute(
            select(OParlMeeting).where(OParlMeeting.id == request.meeting_id)
        )
        meeting = result.scalar_one_or_none()
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")

        # Get agenda items
        agenda_result = await db.execute(
            select(OParlAgendaItem)
            .where(OParlAgendaItem.meeting_id == meeting.id)
            .order_by(OParlAgendaItem.order)
        )
        agenda_items = agenda_result.scalars().all()

        summary = await ai_service.summarize_meeting(
            meeting_name=meeting.name or "Sitzung",
            agenda_items=[
                {"number": item.number, "name": item.name}
                for item in agenda_items
            ],
            date=meeting.start.isoformat() if meeting.start else None,
        )

        return SummarizeResponse(
            summary=summary,
            source_type="meeting",
            source_id=str(meeting.id),
        )

    elif request.text:
        # Summarize raw text
        summary = await ai_service.summarize_paper(
            paper_name="Dokument",
            paper_text=request.text,
        )

        return SummarizeResponse(
            summary=summary,
            source_type="text",
        )

    else:
        raise HTTPException(
            status_code=400,
            detail="Provide paper_id, meeting_id, or text",
        )


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """
    Chat with the AI about municipal data.

    Optionally provide context_paper_id or context_meeting_id
    to give the AI context about a specific document.
    """
    context = None
    context_used = False

    # Build context from referenced documents
    if request.context_paper_id:
        result = await db.execute(
            select(OParlPaper).where(OParlPaper.id == request.context_paper_id)
        )
        paper = result.scalar_one_or_none()
        if paper:
            context = f"Aktuelle Vorlage: {paper.name}\nTyp: {paper.paper_type}\nDatum: {paper.date}"
            context_used = True

    elif request.context_meeting_id:
        result = await db.execute(
            select(OParlMeeting).where(OParlMeeting.id == request.context_meeting_id)
        )
        meeting = result.scalar_one_or_none()
        if meeting:
            context = f"Aktuelle Sitzung: {meeting.name}\nDatum: {meeting.start}\nOrt: {meeting.location_name}"
            context_used = True

    response = await ai_service.chat(
        user_message=request.message,
        context=context,
        history=request.history if request.history else None,
    )

    return ChatResponse(
        response=response,
        context_used=context_used,
    )


@router.post("/locations", response_model=LocationsResponse)
async def extract_locations(
    request: LocationsRequest,
    db: AsyncSession = Depends(get_db),
) -> LocationsResponse:
    """
    Extract and geocode locations from text or a paper.

    Provide either:
    - text: Raw text to extract locations from
    - paper_id: UUID of a paper to extract locations from
    """
    text = request.text
    source_id = None

    if request.paper_id:
        result = await db.execute(
            select(OParlPaper).where(OParlPaper.id == request.paper_id)
        )
        paper = result.scalar_one_or_none()
        if not paper:
            raise HTTPException(status_code=404, detail="Paper not found")

        source_id = str(paper.id)
        text = paper.name or ""

        # Get file contents
        files_result = await db.execute(
            select(OParlFile).where(OParlFile.paper_id == paper.id)
        )
        files = files_result.scalars().all()
        for file in files:
            if file.text_content:
                text += f"\n{file.text_content}"

    if not text:
        raise HTTPException(
            status_code=400,
            detail="Provide text or paper_id",
        )

    locations = await ai_service.extract_and_geocode_locations(
        text=text,
        city=request.city,
    )

    return LocationsResponse(
        locations=[
            LocationResponse(
                name=loc.name,
                address=loc.address,
                latitude=loc.latitude,
                longitude=loc.longitude,
            )
            for loc in locations
        ],
        source_id=source_id,
    )


@router.post("/parse-search", response_model=SearchParseResponse)
async def parse_search(
    request: SearchParseRequest,
) -> SearchParseResponse:
    """
    Parse a natural language search query into structured filters.

    Example: "Anträge zum Thema Radwege aus 2024" ->
    {keywords: ["Radwege"], paper_type: "Antrag", date_from: "2024-01-01"}
    """
    result = await ai_service.parse_search_query(request.query)

    return SearchParseResponse(
        keywords=result.get("keywords", []),
        paper_type=result.get("paper_type"),
        date_from=result.get("date_from"),
        date_to=result.get("date_to"),
        organization=result.get("organization"),
        person=result.get("person"),
    )


@router.get("/papers/{paper_id}/locations", response_model=LocationsResponse)
async def get_paper_locations(
    paper_id: UUID,
    city: str = Query("", description="City name for geocoding context"),
    db: AsyncSession = Depends(get_db),
) -> LocationsResponse:
    """
    Get locations for a paper.

    If the paper already has cached locations, returns those.
    Otherwise extracts and geocodes locations from the paper text.
    """
    result = await db.execute(
        select(OParlPaper).where(OParlPaper.id == paper_id)
    )
    paper = result.scalar_one_or_none()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")

    # Check for cached locations
    if paper.locations and "locations" in paper.locations:
        return LocationsResponse(
            locations=[
                LocationResponse(**loc)
                for loc in paper.locations["locations"]
            ],
            source_id=str(paper.id),
        )

    # Extract and geocode
    text = paper.name or ""
    files_result = await db.execute(
        select(OParlFile).where(OParlFile.paper_id == paper.id)
    )
    files = files_result.scalars().all()
    for file in files:
        if file.text_content:
            text += f"\n{file.text_content}"

    locations = await ai_service.extract_and_geocode_locations(
        text=text,
        city=city,
    )

    # Cache the results
    paper.locations = {
        "locations": [
            {
                "name": loc.name,
                "address": loc.address,
                "latitude": loc.latitude,
                "longitude": loc.longitude,
            }
            for loc in locations
        ]
    }
    await db.commit()

    return LocationsResponse(
        locations=[
            LocationResponse(
                name=loc.name,
                address=loc.address,
                latitude=loc.latitude,
                longitude=loc.longitude,
            )
            for loc in locations
        ],
        source_id=str(paper.id),
    )

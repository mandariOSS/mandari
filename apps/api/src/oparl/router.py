"""
OParl API Router

Endpoints for accessing OParl data.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_db
from src.oparl.models import (
    OParlAgendaItem,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)
from src.oparl.schemas import (
    OParlAgendaItemResponse,
    OParlBodyDetail,
    OParlBodyResponse,
    OParlFileResponse,
    OParlMeetingDetail,
    OParlMeetingResponse,
    OParlOrganizationResponse,
    OParlPaperDetail,
    OParlPaperResponse,
    OParlPersonResponse,
    OParlSourceCreate,
    OParlSourceResponse,
    PaginatedResponse,
)

router = APIRouter(prefix="/oparl", tags=["oparl"])


# --- Sources ---


@router.get("/sources", response_model=list[OParlSourceResponse])
async def list_sources(
    db: AsyncSession = Depends(get_db),
) -> list[OParlSource]:
    """List all registered OParl sources."""
    result = await db.execute(select(OParlSource).order_by(OParlSource.name))
    return list(result.scalars().all())


@router.post("/sources", response_model=OParlSourceResponse)
async def create_source(
    source: OParlSourceCreate,
    db: AsyncSession = Depends(get_db),
) -> OParlSource:
    """Register a new OParl source."""
    db_source = OParlSource(**source.model_dump())
    db.add(db_source)
    await db.flush()
    return db_source


# --- Bodies ---


@router.get("/bodies", response_model=PaginatedResponse)
async def list_bodies(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List all bodies (municipalities)."""
    # Count total
    count_result = await db.execute(select(func.count(OParlBody.id)))
    total = count_result.scalar() or 0

    # Get paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        select(OParlBody).order_by(OParlBody.name).offset(offset).limit(page_size)
    )
    items = list(result.scalars().all())

    return {
        "items": [OParlBodyResponse.model_validate(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/bodies/{body_id}", response_model=OParlBodyDetail)
async def get_body(
    body_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> OParlBody:
    """Get a single body by ID."""
    result = await db.execute(select(OParlBody).where(OParlBody.id == body_id))
    body = result.scalar_one_or_none()
    if not body:
        raise HTTPException(status_code=404, detail="Body not found")
    return body


# --- Meetings ---


@router.get("/bodies/{body_id}/meetings", response_model=PaginatedResponse)
async def list_body_meetings(
    body_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List meetings for a body."""
    # Count total
    count_result = await db.execute(
        select(func.count(OParlMeeting.id)).where(OParlMeeting.body_id == body_id)
    )
    total = count_result.scalar() or 0

    # Get paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        select(OParlMeeting)
        .where(OParlMeeting.body_id == body_id)
        .order_by(OParlMeeting.start.desc())
        .offset(offset)
        .limit(page_size)
    )
    items = list(result.scalars().all())

    return {
        "items": [OParlMeetingResponse.model_validate(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/meetings/{meeting_id}", response_model=OParlMeetingDetail)
async def get_meeting(
    meeting_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> OParlMeeting:
    """Get a single meeting by ID."""
    result = await db.execute(select(OParlMeeting).where(OParlMeeting.id == meeting_id))
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.get("/meetings/{meeting_id}/agenda", response_model=list[OParlAgendaItemResponse])
async def get_meeting_agenda(
    meeting_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[OParlAgendaItem]:
    """Get agenda items for a meeting."""
    result = await db.execute(
        select(OParlAgendaItem)
        .where(OParlAgendaItem.meeting_id == meeting_id)
        .order_by(OParlAgendaItem.order)
    )
    return list(result.scalars().all())


# --- Papers ---


@router.get("/bodies/{body_id}/papers", response_model=PaginatedResponse)
async def list_body_papers(
    body_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List papers for a body."""
    # Count total
    count_result = await db.execute(
        select(func.count(OParlPaper.id)).where(OParlPaper.body_id == body_id)
    )
    total = count_result.scalar() or 0

    # Get paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        select(OParlPaper)
        .where(OParlPaper.body_id == body_id)
        .order_by(OParlPaper.date.desc())
        .offset(offset)
        .limit(page_size)
    )
    items = list(result.scalars().all())

    return {
        "items": [OParlPaperResponse.model_validate(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/papers/{paper_id}", response_model=OParlPaperDetail)
async def get_paper(
    paper_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> OParlPaper:
    """Get a single paper by ID."""
    result = await db.execute(select(OParlPaper).where(OParlPaper.id == paper_id))
    paper = result.scalar_one_or_none()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found")
    return paper


@router.get("/papers/{paper_id}/files", response_model=list[OParlFileResponse])
async def get_paper_files(
    paper_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[OParlFile]:
    """Get files attached to a paper."""
    result = await db.execute(
        select(OParlFile).where(OParlFile.paper_id == paper_id).order_by(OParlFile.name)
    )
    return list(result.scalars().all())


# --- Persons ---


@router.get("/bodies/{body_id}/persons", response_model=PaginatedResponse)
async def list_body_persons(
    body_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List persons for a body."""
    # Count total
    count_result = await db.execute(
        select(func.count(OParlPerson.id)).where(OParlPerson.body_id == body_id)
    )
    total = count_result.scalar() or 0

    # Get paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        select(OParlPerson)
        .where(OParlPerson.body_id == body_id)
        .order_by(OParlPerson.name)
        .offset(offset)
        .limit(page_size)
    )
    items = list(result.scalars().all())

    return {
        "items": [OParlPersonResponse.model_validate(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/persons/{person_id}", response_model=OParlPersonResponse)
async def get_person(
    person_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> OParlPerson:
    """Get a single person by ID."""
    result = await db.execute(select(OParlPerson).where(OParlPerson.id == person_id))
    person = result.scalar_one_or_none()
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


# --- Organizations ---


@router.get("/bodies/{body_id}/organizations", response_model=PaginatedResponse)
async def list_body_organizations(
    body_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List organizations for a body."""
    # Count total
    count_result = await db.execute(
        select(func.count(OParlOrganization.id)).where(OParlOrganization.body_id == body_id)
    )
    total = count_result.scalar() or 0

    # Get paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        select(OParlOrganization)
        .where(OParlOrganization.body_id == body_id)
        .order_by(OParlOrganization.name)
        .offset(offset)
        .limit(page_size)
    )
    items = list(result.scalars().all())

    return {
        "items": [OParlOrganizationResponse.model_validate(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
    }


@router.get("/organizations/{org_id}", response_model=OParlOrganizationResponse)
async def get_organization(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> OParlOrganization:
    """Get a single organization by ID."""
    result = await db.execute(
        select(OParlOrganization).where(OParlOrganization.id == org_id)
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org

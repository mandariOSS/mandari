"""
OParl Pydantic Schemas

API request/response schemas for OParl entities.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class OParlSourceBase(BaseModel):
    """Base schema for OParl source."""

    name: str
    url: str
    contact_email: str | None = None
    contact_name: str | None = None
    website: str | None = None


class OParlSourceCreate(OParlSourceBase):
    """Schema for creating a new OParl source."""

    pass


class OParlSourceResponse(OParlSourceBase):
    """Schema for OParl source response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    last_sync: datetime | None
    created_at: datetime
    updated_at: datetime


class OParlBodyBase(BaseModel):
    """Base schema for OParl body."""

    name: str
    short_name: str | None = None
    website: str | None = None
    classification: str | None = None


class OParlBodyResponse(OParlBodyBase):
    """Schema for OParl body response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    source_id: UUID
    license: str | None = None
    oparl_created: datetime | None = None
    oparl_modified: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OParlBodyDetail(OParlBodyResponse):
    """Detailed body response with raw JSON."""

    raw_json: dict[str, Any]


class OParlMeetingBase(BaseModel):
    """Base schema for OParl meeting."""

    name: str | None = None
    meeting_state: str | None = None
    cancelled: bool = False
    start: datetime | None = None
    end: datetime | None = None
    location_name: str | None = None
    location_address: str | None = None


class OParlMeetingResponse(OParlMeetingBase):
    """Schema for OParl meeting response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    body_id: UUID
    oparl_created: datetime | None = None
    oparl_modified: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OParlMeetingDetail(OParlMeetingResponse):
    """Detailed meeting response with agenda items."""

    raw_json: dict[str, Any]


class OParlPaperBase(BaseModel):
    """Base schema for OParl paper."""

    name: str | None = None
    reference: str | None = None
    paper_type: str | None = None
    date: datetime | None = None


class OParlPaperResponse(OParlPaperBase):
    """Schema for OParl paper response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    body_id: UUID
    summary: str | None = None
    oparl_created: datetime | None = None
    oparl_modified: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OParlPaperDetail(OParlPaperResponse):
    """Detailed paper response."""

    raw_json: dict[str, Any]
    locations: dict[str, Any] | None = None


class OParlPersonBase(BaseModel):
    """Base schema for OParl person."""

    name: str | None = None
    family_name: str | None = None
    given_name: str | None = None
    title: str | None = None
    gender: str | None = None


class OParlPersonResponse(OParlPersonBase):
    """Schema for OParl person response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    body_id: UUID
    email: str | None = None
    phone: str | None = None
    oparl_created: datetime | None = None
    oparl_modified: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OParlOrganizationBase(BaseModel):
    """Base schema for OParl organization."""

    name: str | None = None
    short_name: str | None = None
    organization_type: str | None = None
    classification: str | None = None


class OParlOrganizationResponse(OParlOrganizationBase):
    """Schema for OParl organization response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    body_id: UUID
    start_date: datetime | None = None
    end_date: datetime | None = None
    website: str | None = None
    oparl_created: datetime | None = None
    oparl_modified: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OParlAgendaItemResponse(BaseModel):
    """Schema for OParl agenda item response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    meeting_id: UUID
    number: str | None = None
    order: int | None = None
    name: str | None = None
    public: bool = True
    result: str | None = None
    resolution_text: str | None = None
    oparl_created: datetime | None = None
    oparl_modified: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OParlFileResponse(BaseModel):
    """Schema for OParl file response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    external_id: str
    paper_id: UUID | None = None
    name: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    access_url: str | None = None
    download_url: str | None = None
    oparl_created: datetime | None = None
    oparl_modified: datetime | None = None
    created_at: datetime
    updated_at: datetime


# Pagination
class PaginatedResponse(BaseModel):
    """Generic paginated response."""

    items: list[Any]
    total: int
    page: int
    page_size: int
    pages: int

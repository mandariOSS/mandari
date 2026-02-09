"""
OParl Entity Models

Pydantic models for all OParl entity types.
Date-only fields (start_date, end_date, Paper.date) use `date` type
to match the OParl spec and Django DateField columns.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import Field

from .base import ProcessedEntity


class ProcessedBody(ProcessedEntity):
    """Processed OParl Body."""

    name: str
    short_name: str | None = None
    website: str | None = None
    license: str | None = None
    classification: str | None = None

    # List URLs for fetching
    organization_list_url: str | None = None
    person_list_url: str | None = None
    meeting_list_url: str | None = None
    paper_list_url: str | None = None
    membership_list_url: str | None = None
    location_list_url: str | None = None
    agenda_item_list_url: str | None = None
    consultation_list_url: str | None = None
    file_list_url: str | None = None
    legislative_term_list_url: str | None = None


class ProcessedMeeting(ProcessedEntity):
    """Processed OParl Meeting."""

    name: str | None = None
    meeting_state: str | None = None
    cancelled: bool = False
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    location_external_id: str | None = None
    location_name: str | None = None
    location_address: str | None = None


class ProcessedPaper(ProcessedEntity):
    """Processed OParl Paper."""

    name: str | None = None
    reference: str | None = None
    paper_type: str | None = None
    date: dt.date | None = None


class ProcessedPerson(ProcessedEntity):
    """Processed OParl Person."""

    name: str | None = None
    family_name: str | None = None
    given_name: str | None = None
    title: str | None = None
    gender: str | None = None
    email: str | None = None
    phone: str | None = None


class ProcessedOrganization(ProcessedEntity):
    """Processed OParl Organization."""

    name: str | None = None
    short_name: str | None = None
    organization_type: str | None = None
    classification: str | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    website: str | None = None


class ProcessedAgendaItem(ProcessedEntity):
    """Processed OParl AgendaItem."""

    number: str | None = None
    order: int | None = None
    name: str | None = None
    public: bool = True
    result: str | None = None
    resolution_text: str | None = None
    meeting_external_id: str | None = None


class ProcessedFile(ProcessedEntity):
    """Processed OParl File."""

    name: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    access_url: str | None = None
    download_url: str | None = None
    date: dt.datetime | None = None

    # Back-references (from standalone files fetched via /files endpoint)
    # OParl spec: File objects contain 'paper' and 'meeting' arrays
    # when fetched individually (not embedded)
    paper_external_ids: list[str] = Field(default_factory=list)
    meeting_external_ids: list[str] = Field(default_factory=list)


class ProcessedLocation(ProcessedEntity):
    """Processed OParl Location."""

    description: str | None = None
    street_address: str | None = None
    room: str | None = None
    postal_code: str | None = None
    locality: str | None = None
    geojson: dict[str, Any] | None = None


class ProcessedConsultation(ProcessedEntity):
    """Processed OParl Consultation."""

    paper_external_id: str | None = None
    meeting_external_id: str | None = None
    agenda_item_external_id: str | None = None
    role: str | None = None
    authoritative: bool = False


class ProcessedMembership(ProcessedEntity):
    """Processed OParl Membership."""

    person_external_id: str | None = None
    organization_external_id: str | None = None
    role: str | None = None
    voting_right: bool = True
    start_date: dt.date | None = None
    end_date: dt.date | None = None


class ProcessedLegislativeTerm(ProcessedEntity):
    """Processed OParl LegislativeTerm."""

    name: str | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None

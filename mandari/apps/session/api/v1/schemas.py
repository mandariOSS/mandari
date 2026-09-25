# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic-Schemas der Session-API v1 (Antwort- und Anfrageformen, erzeugen das OpenAPI-Dokument)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from ninja import Field, Schema
from pydantic import field_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ApplicationType = Literal["motion", "inquiry", "resolution", "urgent", "amendment", "other"]


class OrganizationRef(Schema):
    id: UUID
    name: str


class ListMeta(Schema):
    total: int = Field(description="Gesamtzahl der Treffer (vor limit/offset)")
    limit: int
    offset: int
    authenticated: bool = Field(description="Ob der Aufrufer per Token oder Sitzung identifiziert ist")


class MeetingOut(Schema):
    id: UUID
    name: str
    organization: OrganizationRef
    joint_organizations: list[OrganizationRef] = Field(
        default_factory=list, description="Gemeinsame Sitzung: weitere beteiligte Gremien (leer bei einem Gremium)"
    )
    start: datetime | None
    end: datetime | None
    location: str = ""
    meeting_state: str = ""
    cancelled: bool
    is_public: bool
    internal_notes: str | None = Field(None, description="Nur mit Recht view_non_public_meetings, nur bei NÖ-Sitzungen")


class MeetingList(Schema):
    data: list[MeetingOut]
    meta: ListMeta


class PaperOut(Schema):
    id: UUID
    reference: str
    name: str
    paper_type: str = ""
    status: str = ""
    date: date | None
    is_public: bool
    main_organization: OrganizationRef | None
    main_text: str | None = Field(None, description="Nur mit Recht view_non_public_papers")
    resolution_text: str | None = Field(None, description="Nur mit Recht view_non_public_papers")


class PaperList(Schema):
    data: list[PaperOut]
    meta: ListMeta


class ApplicationOut(Schema):
    id: UUID
    reference: str
    title: str
    application_type: str
    status: str
    submitter_name: str
    submitting_organization: OrganizationRef | None
    target_organization: OrganizationRef | None
    is_urgent: bool
    submitted_at: datetime | None


class ApplicationList(Schema):
    data: list[ApplicationOut]
    meta: ListMeta


class ApplicationIn(Schema):
    """Antrag einreichen (z. B. aus dem Work-Portal)."""

    title: Annotated[str, Field(min_length=1, max_length=500)]
    justification: Annotated[str, Field(min_length=1, max_length=50000)]
    resolution_proposal: Annotated[str, Field(min_length=1, max_length=50000)]
    submitter_name: Annotated[str, Field(min_length=1, max_length=200)]
    submitter_email: Annotated[str, Field(min_length=3, max_length=254)]
    application_type: ApplicationType = "motion"
    financial_impact: Annotated[str, Field(max_length=10000)] = ""
    submitter_phone: Annotated[str, Field(max_length=50)] = ""
    co_signers: Annotated[str, Field(max_length=5000)] = ""
    is_urgent: bool = False
    urgency_reason: Annotated[str, Field(max_length=5000)] = ""
    deadline: date | None = None
    submitting_organization_id: UUID | None = Field(None, description="Organisation im Work-Portal (optional)")
    target_organization_id: UUID | None = Field(None, description="Zuständiges Gremium des Mandanten (optional)")

    @field_validator("submitter_email")
    @classmethod
    def _email(cls, value: str) -> str:
        if not EMAIL_RE.match(value):
            raise ValueError("keine gültige E-Mail-Adresse")
        return value

    @field_validator("application_type", mode="before")
    @classmethod
    def _legacy_types(cls, value: object) -> object:
        # Aliase früherer API-Versionen bleiben gültig
        return {"proposal": "motion", "urgent_motion": "urgent"}.get(str(value), value)


class ApplicationCreated(Schema):
    id: UUID
    reference: str
    status: str
    feedback: str | None = Field(None, description="URL des Rückmeldestands (mit demselben Token abrufbar)")


class FeedbackPaper(Schema):
    converted: bool = Field(description="Aus dem Antrag ist eine Vorlage entstanden")
    public: bool = Field(description="Die Vorlage ist veröffentlicht (öffentlich und freigegeben)")
    reference: str | None = Field(
        None, description="Vorlagen- bzw. Drucksachennummer, nur bei veröffentlichter Vorlage"
    )
    reference_label: str = Field(description="Bezeichnung der Nummer beim Mandanten, z. B. „Drucksache“")


class FeedbackStation(Schema):
    """Station der Beratungsfolge. Nicht-öffentliche Stationen tragen nur ``order``, ``public`` und ``label``."""

    order: int
    public: bool
    label: str | None = Field(None, description="„nicht-öffentlich beraten“ bei nicht-öffentlichen Stationen")
    organization: str | None = None
    role: str | None = None
    role_label: str | None = None
    decisive: bool | None = None
    meeting_name: str | None = None
    start: datetime | None = None
    cancelled: bool | None = None
    agenda_number: str | None = None
    removed_from_agenda: bool | None = None
    result: str | None = None
    result_label: str | None = None
    resolution_number: str | None = None


class FeedbackDecision(Schema):
    result: Literal["approved", "rejected", "noted", "withdrawn"]
    result_label: str
    organization: str
    decided_on: date | None
    resolution_number: str | None = None


class ApplicationFeedbackOut(Schema):
    """Rückmeldestand eines eingereichten Antrags – nur öffentlich zulässige Angaben (Issue #316)."""

    id: UUID
    reference: str = Field(description="Eingangsnummer")
    status: str
    status_label: str
    submitted_at: datetime | None
    received_at: datetime | None
    paper: FeedbackPaper
    stations: list[FeedbackStation]
    decision: FeedbackDecision | None = Field(None, description="Nur aus einer öffentlichen, entscheidenden Beratung")


class TenantRoot(Schema):
    name: str
    version: str
    tenant: str
    oparl: str = Field(description="OParl-1.1-System-Endpunkt (öffentlich, anonym)")
    meetings: str
    papers: str
    applications: str
    submit_application: str
    openapi: str
    docs: str

"""
OParl Database Models

SQLAlchemy models for OParl entities (mirror of external OParl data).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class OParlSource(Base):
    """A registered OParl data source (e.g., a city's RIS API)."""

    __tablename__ = "oparl_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text, unique=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Sync configuration
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Raw OParl data
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    bodies: Mapped[list["OParlBody"]] = relationship(back_populates="source")


class OParlBody(Base):
    """A body/municipality (Kommune/Körperschaft)."""

    __tablename__ = "oparl_bodies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("oparl_sources.id"))

    name: Mapped[str] = mapped_column(String(255))
    short_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)
    license: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_valid_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    classification: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # OParl timestamps
    oparl_created: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oparl_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Raw OParl data
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    source: Mapped["OParlSource"] = relationship(back_populates="bodies")
    meetings: Mapped[list["OParlMeeting"]] = relationship(back_populates="body")
    papers: Mapped[list["OParlPaper"]] = relationship(back_populates="body")
    persons: Mapped[list["OParlPerson"]] = relationship(back_populates="body")
    organizations: Mapped[list["OParlOrganization"]] = relationship(back_populates="body")


class OParlMeeting(Base):
    """A meeting (Sitzung)."""

    __tablename__ = "oparl_meetings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    body_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("oparl_bodies.id"))

    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    meeting_state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)

    start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    location_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    location_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OParl timestamps
    oparl_created: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oparl_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Raw OParl data
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    body: Mapped["OParlBody"] = relationship(back_populates="meetings")
    agenda_items: Mapped[list["OParlAgendaItem"]] = relationship(back_populates="meeting")


class OParlPaper(Base):
    """A paper/document (Vorlage/Vorgang)."""

    __tablename__ = "oparl_papers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    body_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("oparl_bodies.id"))

    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    paper_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # OParl timestamps
    oparl_created: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oparl_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Raw OParl data
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # AI-enhanced fields
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    locations: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    body: Mapped["OParlBody"] = relationship(back_populates="papers")
    files: Mapped[list["OParlFile"]] = relationship(back_populates="paper")


class OParlPerson(Base):
    """A person (council member)."""

    __tablename__ = "oparl_persons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    body_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("oparl_bodies.id"))

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    family_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    given_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(50), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # OParl timestamps
    oparl_created: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oparl_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Raw OParl data
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    body: Mapped["OParlBody"] = relationship(back_populates="persons")


class OParlOrganization(Base):
    """An organization (Gremium/Fraktion)."""

    __tablename__ = "oparl_organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    body_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("oparl_bodies.id"))

    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    short_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    organization_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    classification: Mapped[str | None] = mapped_column(String(100), nullable=True)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    website: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OParl timestamps
    oparl_created: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oparl_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Raw OParl data
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    body: Mapped["OParlBody"] = relationship(back_populates="organizations")


class OParlAgendaItem(Base):
    """An agenda item (Tagesordnungspunkt)."""

    __tablename__ = "oparl_agenda_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("oparl_meetings.id"))

    number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    order: Mapped[int | None] = mapped_column(nullable=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    public: Mapped[bool] = mapped_column(Boolean, default=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OParl timestamps
    oparl_created: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oparl_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Raw OParl data
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    meeting: Mapped["OParlMeeting"] = relationship(back_populates="agenda_items")


class OParlFile(Base):
    """A file/document attachment."""

    __tablename__ = "oparl_files"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(Text, unique=True, index=True)
    paper_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("oparl_papers.id"), nullable=True
    )

    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size: Mapped[int | None] = mapped_column(nullable=True)
    access_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Local storage
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    # OParl timestamps
    oparl_created: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    oparl_modified: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Raw OParl data
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    paper: Mapped["OParlPaper | None"] = relationship(back_populates="files")

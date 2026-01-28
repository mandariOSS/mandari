"""
Work Module Database Models

SQLAlchemy models for political organizations (Parteien & Fraktionen).
Implements the flexible document workflow with coalition voting.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


# =============================================================================
# Enums
# =============================================================================


class MembershipRole(StrEnum):
    """Roles a member can have in an organization."""

    ADMIN = "admin"
    CHAIR = "chair"  # Vorsitzende/r
    VICE_CHAIR = "vice_chair"  # Stellvertretende/r Vorsitzende/r
    MANAGING_DIRECTOR = "managing_director"  # Geschäftsführung
    COUNCIL_MEMBER = "council_member"  # Ratsmitglied
    FACTION_MEMBER = "faction_member"  # Fraktionsmitglied
    EXPERT_CITIZEN = "expert_citizen"  # Sachkundige/r Bürger*in
    MEMBER = "member"  # Einfaches Mitglied
    VIEWER = "viewer"  # Nur Lesezugriff


class MotionStatus(StrEnum):
    """Status of a motion in its lifecycle."""

    DRAFT = "draft"
    INTERNAL_REVIEW = "internal_review"  # Interne Genehmigung
    EXTERNAL_REVIEW = "external_review"  # Koalitionsabstimmung
    ON_AGENDA = "on_agenda"  # Auf Tagesordnung
    APPROVED = "approved"
    SUBMITTED = "submitted"  # An Verwaltung gesendet
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class MotionVisibility(StrEnum):
    """Visibility levels for motions."""

    PRIVATE = "private"  # Only author
    SHARED = "shared"  # Explicitly shared
    FACTION = "faction"  # All faction members
    ORGANIZATION = "organization"  # All organization members
    PUBLIC = "public"  # Everyone


class ApprovalStatus(StrEnum):
    """Status of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class CoalitionResult(StrEnum):
    """Result of coalition consultation."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    MODIFIED = "modified"  # With changes
    NO_RESPONSE = "no_response"


class CoAuthorInviteStatus(StrEnum):
    """Status of co-author invitation."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class ShareMethod(StrEnum):
    """Method used to share a motion."""

    EMAIL = "email"
    LINK = "link"
    DOWNLOAD = "download"


class WorkGroupRole(StrEnum):
    """Roles in a working group."""

    SPEAKER = "speaker"  # AG-Sprecher*in
    MEMBER = "member"


# -----------------------------------------------------------------------------
# Faction Meeting Enums
# -----------------------------------------------------------------------------


class MeetingStatus(StrEnum):
    """Status of a faction meeting."""

    DRAFT = "draft"  # Sitzung wird geplant
    SCHEDULED = "scheduled"  # Einladungen versendet
    IN_PROGRESS = "in_progress"  # Sitzung läuft
    COMPLETED = "completed"  # Sitzung beendet
    PROTOCOL_APPROVED = "protocol_approved"  # Protokoll genehmigt
    CANCELLED = "cancelled"  # Sitzung abgesagt


class AgendaItemVisibility(StrEnum):
    """Visibility of an agenda item."""

    PUBLIC = "public"  # Öffentlich zugänglich
    INTERNAL = "internal"  # Fraktion + Sachkundige Bürger*innen
    CONFIDENTIAL = "confidential"  # Nur Fraktionsmitglieder


class AgendaSuggestionStatus(StrEnum):
    """Status of an agenda item suggestion."""

    PENDING = "pending"  # Wartet auf Genehmigung
    APPROVED = "approved"  # Genehmigt, auf TO
    REJECTED = "rejected"  # Abgelehnt


class AttendanceType(StrEnum):
    """Type of attendance at a meeting."""

    IN_PERSON = "in_person"  # Vor-Ort
    ONLINE = "online"  # Online-Teilnahme
    GUEST = "guest"  # Gast


class AttendanceStatus(StrEnum):
    """Status of attendance."""

    PRESENT = "present"  # Anwesend
    ABSENT = "absent"  # Abwesend
    EXCUSED = "excused"  # Entschuldigt
    LATE = "late"  # Verspätet erschienen
    LEFT_EARLY = "left_early"  # Vorzeitig gegangen


class ProtocolEntryType(StrEnum):
    """Type of protocol entry."""

    DISCUSSION = "discussion"  # Diskussionspunkt
    DECISION = "decision"  # Beschluss
    INFORMATION = "information"  # Information
    ACTION_ITEM = "action_item"  # Aufgabe (Task-Board-Link)
    NOTE = "note"  # Notiz


class DecisionType(StrEnum):
    """Type of decision on a motion or agenda item."""

    APPROVED_UNANIMOUS = "approved_unanimous"  # Einstimmig angenommen
    APPROVED_MAJORITY = "approved_majority"  # Mehrheitlich angenommen
    REJECTED_UNANIMOUS = "rejected_unanimous"  # Einstimmig abgelehnt
    REJECTED_MAJORITY = "rejected_majority"  # Mehrheitlich abgelehnt
    POSTPONED = "postponed"  # Vertagt
    WITHDRAWN = "withdrawn"  # Zurückgezogen
    NOTED = "noted"  # Zur Kenntnis genommen
    REFERRED = "referred"  # Überwiesen


class RSVPStatus(StrEnum):
    """RSVP status for meeting invitations."""

    PENDING = "pending"  # Keine Antwort
    ACCEPTED = "accepted"  # Zugesagt
    DECLINED = "declined"  # Abgesagt
    TENTATIVE = "tentative"  # Unter Vorbehalt


# =============================================================================
# Association Tables (Many-to-Many)
# =============================================================================

# Co-authors of motions
motion_co_authors = Table(
    "motion_co_authors",
    Base.metadata,
    mapped_column("motion_id", UUID(as_uuid=True), ForeignKey("motions.id"), primary_key=True),
    mapped_column(
        "membership_id", UUID(as_uuid=True), ForeignKey("memberships.id"), primary_key=True
    ),
)


# =============================================================================
# User Model (Basic - to be extended with auth)
# =============================================================================


class User(Base):
    """A user of the system."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    memberships: Mapped[list["Membership"]] = relationship(back_populates="user")


# =============================================================================
# Organization Models
# =============================================================================


class Organization(Base):
    """
    A political organization (Fraktion, Ortsverband, etc.).

    This is the tenant for the work module - users access the system
    through their membership in an organization.
    """

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255))
    short_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)

    # Hierarchy
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )

    # Link to OParl organization (faction in council)
    oparl_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("oparl_organizations.id"), nullable=True
    )

    # Link to OParl body (municipality)
    oparl_body_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("oparl_bodies.id"), nullable=True
    )

    # Branding
    primary_color: Mapped[str | None] = mapped_column(String(7), nullable=True)  # #RRGGBB
    secondary_color: Mapped[str | None] = mapped_column(String(7), nullable=True)

    # Settings
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    parent: Mapped["Organization | None"] = relationship(
        "Organization", remote_side="Organization.id", back_populates="children"
    )
    children: Mapped[list["Organization"]] = relationship(back_populates="parent")
    memberships: Mapped[list["Membership"]] = relationship(back_populates="organization")
    motion_types: Mapped[list["MotionType"]] = relationship(back_populates="organization")
    motions: Mapped[list["Motion"]] = relationship(back_populates="organization")
    council_parties: Mapped[list["CouncilParty"]] = relationship(back_populates="organization")
    workgroups: Mapped[list["WorkGroup"]] = relationship(back_populates="organization")
    faction_meetings: Mapped[list["FactionMeeting"]] = relationship(back_populates="organization")
    meeting_settings: Mapped["FactionMeetingSettings | None"] = relationship(
        back_populates="organization", uselist=False
    )


class Membership(Base):
    """
    A user's membership in an organization.

    Represents the relationship between a user and an organization,
    including their role and permissions.
    """

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))

    # Role and permissions
    role: Mapped[MembershipRole] = mapped_column(
        Enum(MembershipRole, name="membership_role"),
        default=MembershipRole.MEMBER,
    )
    permissions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict
    )  # Additional permissions

    # Profile within organization
    title: Mapped[str | None] = mapped_column(String(100), nullable=True)  # e.g., "Sprecher AG Umwelt"
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Link to OParl person (if council member)
    oparl_person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("oparl_persons.id"), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Timestamps
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "organization_id", name="uq_user_organization"),)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="memberships")
    organization: Mapped["Organization"] = relationship(back_populates="memberships")
    created_motions: Mapped[list["Motion"]] = relationship(
        back_populates="created_by", foreign_keys="Motion.created_by_id"
    )
    co_authored_motions: Mapped[list["Motion"]] = relationship(
        secondary=motion_co_authors, back_populates="co_authors"
    )
    approvals_given: Mapped[list["MotionApproval"]] = relationship(
        back_populates="approved_by", foreign_keys="MotionApproval.approved_by_id"
    )
    co_author_invitations: Mapped[list["MotionCoAuthorInvite"]] = relationship(
        back_populates="invited_member", foreign_keys="MotionCoAuthorInvite.invited_member_id"
    )
    created_meetings: Mapped[list["FactionMeeting"]] = relationship(
        back_populates="created_by", foreign_keys="FactionMeeting.created_by_id"
    )
    faction_attendances: Mapped[list["FactionAttendance"]] = relationship(
        back_populates="membership", foreign_keys="FactionAttendance.membership_id"
    )
    meeting_invitations: Mapped[list["FactionMeetingInvitation"]] = relationship(
        back_populates="membership", foreign_keys="FactionMeetingInvitation.membership_id"
    )

    @property
    def is_council_member(self) -> bool:
        """Check if this membership represents a council member."""
        return self.role in (
            MembershipRole.COUNCIL_MEMBER,
            MembershipRole.CHAIR,
            MembershipRole.VICE_CHAIR,
        )

    @property
    def is_expert_citizen(self) -> bool:
        """Check if this membership represents a Sachkundige/r Bürger*in."""
        return self.role == MembershipRole.EXPERT_CITIZEN

    @property
    def can_approve_motions(self) -> bool:
        """Check if this member can approve motions."""
        return self.role in (
            MembershipRole.ADMIN,
            MembershipRole.CHAIR,
            MembershipRole.VICE_CHAIR,
            MembershipRole.MANAGING_DIRECTOR,
        )

    @property
    def can_add_to_agenda(self) -> bool:
        """Check if this member can add motions to meeting agenda."""
        return self.role in (
            MembershipRole.ADMIN,
            MembershipRole.CHAIR,
            MembershipRole.VICE_CHAIR,
            MembershipRole.MANAGING_DIRECTOR,
            MembershipRole.COUNCIL_MEMBER,
        )

    @property
    def can_add_agenda_directly(self) -> bool:
        """
        Check if this member can add agenda items directly without approval.
        Vorsitzende, Stellvertreter, Geschäftsführung, Ratspersonen can add directly.
        Sachkundige Bürger*innen and other members need approval from chair.
        """
        return self.role in (
            MembershipRole.ADMIN,
            MembershipRole.CHAIR,
            MembershipRole.VICE_CHAIR,
            MembershipRole.MANAGING_DIRECTOR,
            MembershipRole.COUNCIL_MEMBER,
        )

    @property
    def can_approve_agenda_suggestions(self) -> bool:
        """Check if this member can approve agenda item suggestions."""
        return self.role in (
            MembershipRole.ADMIN,
            MembershipRole.CHAIR,
            MembershipRole.VICE_CHAIR,
        )


class CouncilParty(Base):
    """
    A party in the municipal council.

    Used for coalition management and sharing documents.
    """

    __tablename__ = "council_parties"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))

    name: Mapped[str] = mapped_column(String(255))
    short_name: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Contact for sharing
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Coalition status
    is_coalition_member: Mapped[bool] = mapped_column(Boolean, default=False)
    coalition_order: Mapped[int] = mapped_column(Integer, default=0)  # Display order

    # Link to OParl organization (faction)
    oparl_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("oparl_organizations.id"), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="council_parties")
    coalition_consultations: Mapped[list["CoalitionConsultation"]] = relationship(
        back_populates="party"
    )


# =============================================================================
# Motion Type (Workflow Configuration)
# =============================================================================


class MotionType(Base):
    """
    Configuration for a type of motion/document.

    Defines the workflow, required approvals, and behavior for each
    document type (Antrag, Anfrage, Pressemitteilung, etc.).
    """

    __tablename__ = "motion_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))

    name: Mapped[str] = mapped_column(String(100))  # e.g., "Antrag", "Anfrage"
    slug: Mapped[str] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Display
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)  # Icon name
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)  # #RRGGBB

    # Workflow configuration
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    requires_coalition_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_faction_decision: Mapped[bool] = mapped_column(Boolean, default=False)

    # Default approvers (roles that must approve)
    default_approvers: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
    )  # e.g., ["chair", "managing_director"]

    # Workflow steps definition
    workflow_steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
    )

    # Creator restrictions
    allowed_creator_roles: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
    )  # Empty = all roles allowed

    # Co-author recommendations (for expert citizens creating inquiries)
    recommend_co_author: Mapped[bool] = mapped_column(Boolean, default=False)
    recommend_co_author_roles: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
    )  # e.g., ["council_member", "faction_member"]
    co_author_recommendation_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ordering
    display_order: Mapped[int] = mapped_column(Integer, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_org_motion_type_slug"),
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="motion_types")
    motions: Mapped[list["Motion"]] = relationship(back_populates="motion_type")


# =============================================================================
# Motion (Document)
# =============================================================================


class Motion(Base):
    """
    A motion/document (Antrag, Anfrage, Pressemitteilung, etc.).

    Core entity for document management in the work module.
    """

    __tablename__ = "motions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    motion_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("motion_types.id"), nullable=True
    )

    # Basic info
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Status and visibility
    status: Mapped[MotionStatus] = mapped_column(
        Enum(MotionStatus, name="motion_status"),
        default=MotionStatus.DRAFT,
    )
    visibility: Mapped[MotionVisibility] = mapped_column(
        Enum(MotionVisibility, name="motion_visibility"),
        default=MotionVisibility.PRIVATE,
    )

    # Authorship
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    # Working group assignment (optional)
    assigned_workgroup_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workgroups.id"), nullable=True
    )
    workgroup_recommendation: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # approve, reject, modify
    workgroup_recommendation_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Link to OParl paper (if submitted to council)
    oparl_paper_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("oparl_papers.id"), nullable=True
    )

    # Archiving
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Metadata
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="motions")
    motion_type: Mapped["MotionType | None"] = relationship(back_populates="motions")
    created_by: Mapped["Membership"] = relationship(
        back_populates="created_motions", foreign_keys=[created_by_id]
    )
    co_authors: Mapped[list["Membership"]] = relationship(
        secondary=motion_co_authors, back_populates="co_authored_motions"
    )
    co_author_invites: Mapped[list["MotionCoAuthorInvite"]] = relationship(
        back_populates="motion", cascade="all, delete-orphan"
    )
    approvals: Mapped[list["MotionApproval"]] = relationship(
        back_populates="motion", cascade="all, delete-orphan"
    )
    coalition_consultations: Mapped[list["CoalitionConsultation"]] = relationship(
        back_populates="motion", cascade="all, delete-orphan"
    )
    share_logs: Mapped[list["MotionShareLog"]] = relationship(
        back_populates="motion", cascade="all, delete-orphan"
    )
    assigned_workgroup: Mapped["WorkGroup | None"] = relationship(back_populates="assigned_motions")
    faction_agenda_items: Mapped[list["FactionAgendaItem"]] = relationship(
        back_populates="related_motion"
    )
    faction_decisions: Mapped[list["FactionDecision"]] = relationship(
        back_populates="motion"
    )

    @property
    def all_authors(self) -> list["Membership"]:
        """Get all authors (creator + co-authors)."""
        return [self.created_by, *self.co_authors]

    @property
    def coalition_status(self) -> str:
        """Get overall coalition consultation status."""
        consultations = self.coalition_consultations
        if not consultations:
            return "not_started"
        if any(c.result == CoalitionResult.REJECTED for c in consultations):
            return "rejected"
        if any(c.result == CoalitionResult.PENDING for c in consultations):
            return "pending"
        return "approved"


# =============================================================================
# Co-Author Invite
# =============================================================================


class MotionCoAuthorInvite(Base):
    """
    An invitation to become a co-author of a motion.

    Used when authors want to add additional co-authors to a document.
    """

    __tablename__ = "motion_co_author_invites"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    motion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("motions.id"))
    invited_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))
    invited_member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    status: Mapped[CoAuthorInviteStatus] = mapped_column(
        Enum(CoAuthorInviteStatus, name="co_author_invite_status"),
        default=CoAuthorInviteStatus.PENDING,
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("motion_id", "invited_member_id", name="uq_motion_invited_member"),
    )

    # Relationships
    motion: Mapped["Motion"] = relationship(back_populates="co_author_invites")
    invited_by: Mapped["Membership"] = relationship(foreign_keys=[invited_by_id])
    invited_member: Mapped["Membership"] = relationship(
        back_populates="co_author_invitations", foreign_keys=[invited_member_id]
    )


# =============================================================================
# Motion Approval
# =============================================================================


class MotionApproval(Base):
    """
    An approval request/response for a motion.

    Tracks the approval process for documents that require sign-off.
    """

    __tablename__ = "motion_approvals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    motion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("motions.id"))

    # What kind of approval (role-based)
    approval_type: Mapped[str] = mapped_column(
        String(50)
    )  # e.g., "chair", "managing_director"

    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="approval_status"),
        default=ApprovalStatus.PENDING,
    )

    # Who approved (optional until approved)
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memberships.id"), nullable=True
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    motion: Mapped["Motion"] = relationship(back_populates="approvals")
    approved_by: Mapped["Membership | None"] = relationship(
        back_populates="approvals_given", foreign_keys=[approved_by_id]
    )


# =============================================================================
# Coalition Consultation
# =============================================================================


class CoalitionConsultation(Base):
    """
    Documents coalition partner consultation for a motion.

    Tracks the status of consultation with each coalition partner.
    """

    __tablename__ = "coalition_consultations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    motion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("motions.id"))
    party_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("council_parties.id"))

    # Result of consultation
    result: Mapped[CoalitionResult] = mapped_column(
        Enum(CoalitionResult, name="coalition_result"),
        default=CoalitionResult.PENDING,
    )

    # Communication tracking
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memberships.id"), nullable=True
    )
    sent_via: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # email, meeting, other

    # Response tracking
    response_received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("motion_id", "party_id", name="uq_motion_party"),)

    # Relationships
    motion: Mapped["Motion"] = relationship(back_populates="coalition_consultations")
    party: Mapped["CouncilParty"] = relationship(back_populates="coalition_consultations")
    sent_by: Mapped["Membership | None"] = relationship(foreign_keys=[sent_by_id])


# =============================================================================
# Motion Share Log
# =============================================================================


class MotionShareLog(Base):
    """
    Logs all sharing actions for motions.

    Provides audit trail for document sharing.
    """

    __tablename__ = "motion_share_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    motion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("motions.id"))
    shared_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    # What was shared with
    shared_with_party_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("council_parties.id"), nullable=True
    )
    shared_with_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    method: Mapped[ShareMethod] = mapped_column(
        Enum(ShareMethod, name="share_method"),
        default=ShareMethod.EMAIL,
    )

    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamp
    shared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    motion: Mapped["Motion"] = relationship(back_populates="share_logs")
    shared_by: Mapped["Membership"] = relationship(foreign_keys=[shared_by_id])
    shared_with_party: Mapped["CouncilParty | None"] = relationship(
        foreign_keys=[shared_with_party_id]
    )


# =============================================================================
# Working Groups (AG-System)
# =============================================================================


class WorkGroup(Base):
    """
    A working group (Arbeitsgruppe/AG) within an organization.

    Used for optional pre-consultation of motions.
    """

    __tablename__ = "workgroups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))

    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Leadership
    speaker_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memberships.id"), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_org_workgroup_slug"),)

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="workgroups")
    speaker: Mapped["Membership | None"] = relationship(foreign_keys=[speaker_id])
    members: Mapped[list["WorkGroupMembership"]] = relationship(
        back_populates="workgroup", cascade="all, delete-orphan"
    )
    assigned_motions: Mapped[list["Motion"]] = relationship(back_populates="assigned_workgroup")


class WorkGroupMembership(Base):
    """
    Membership in a working group.
    """

    __tablename__ = "workgroup_memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workgroup_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workgroups.id"))
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    role: Mapped[WorkGroupRole] = mapped_column(
        Enum(WorkGroupRole, name="workgroup_role"),
        default=WorkGroupRole.MEMBER,
    )

    # Timestamps
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("workgroup_id", "member_id", name="uq_workgroup_member"),
    )

    # Relationships
    workgroup: Mapped["WorkGroup"] = relationship(back_populates="members")
    member: Mapped["Membership"] = relationship(foreign_keys=[member_id])


# =============================================================================
# Faction Meeting Models
# =============================================================================


class FactionMeetingSettings(Base):
    """
    Organization-specific settings for faction meetings.
    One-to-one relationship with Organization.
    """

    __tablename__ = "faction_meeting_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"), unique=True
    )

    # Defaults for new meetings
    default_location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    default_conference_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    default_start_time: Mapped[str | None] = mapped_column(
        String(5), nullable=True
    )  # HH:MM format
    default_duration_minutes: Mapped[int] = mapped_column(Integer, default=120)

    # Standard agenda items
    auto_create_approval_item: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_create_various_item: Mapped[bool] = mapped_column(Boolean, default=True)
    political_work_section_title: Mapped[str] = mapped_column(
        String(200), default="Politische Arbeit: Vorlagen und Anträge"
    )
    press_section_title: Mapped[str] = mapped_column(
        String(200), default="SoMe & Presse"
    )

    # Email settings
    invitation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    invitation_days_before: Mapped[int] = mapped_column(Integer, default=7)
    reminder_1_day_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    protocol_notification_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Email templates (JSON)
    email_templates: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Protocol settings
    require_active_checkout: Mapped[bool] = mapped_column(Boolean, default=True)
    attendance_timeout_minutes: Mapped[int] = mapped_column(Integer, default=120)
    auto_lock_protocol_on_end: Mapped[bool] = mapped_column(Boolean, default=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(
        back_populates="meeting_settings"
    )


class FactionMeeting(Base):
    """
    A faction meeting with agenda, protocol, and attendance tracking.
    Supports meeting chaining for protocol approval workflow.
    """

    __tablename__ = "faction_meetings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))

    # Public ID for URLs (not exposing internal UUID)
    public_id: Mapped[str] = mapped_column(
        String(12), unique=True, index=True, nullable=False
    )

    # Basic info
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Scheduling
    scheduled_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Location
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    conference_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    conference_details: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Status
    status: Mapped[MeetingStatus] = mapped_column(
        Enum(MeetingStatus, name="meeting_status"),
        default=MeetingStatus.DRAFT,
    )

    # Meeting chaining - for protocol approval workflow
    previous_meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("faction_meetings.id"), nullable=True
    )

    # Protocol workflow
    protocol_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    protocol_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    protocol_approved_in_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("faction_meetings.id"), nullable=True
    )

    # Protocol locking (revision-safe)
    protocol_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    protocol_locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    protocol_locked_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memberships.id"), nullable=True
    )

    # Attendance locking
    attendance_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    attendance_locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Public protocol
    public_protocol_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Metadata
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    organization: Mapped["Organization"] = relationship(back_populates="faction_meetings")
    created_by: Mapped["Membership"] = relationship(
        foreign_keys=[created_by_id], back_populates="created_meetings"
    )
    protocol_locked_by: Mapped["Membership | None"] = relationship(
        foreign_keys=[protocol_locked_by_id]
    )
    previous_meeting: Mapped["FactionMeeting | None"] = relationship(
        "FactionMeeting",
        remote_side="FactionMeeting.id",
        foreign_keys=[previous_meeting_id],
        back_populates="next_meeting",
    )
    next_meeting: Mapped["FactionMeeting | None"] = relationship(
        "FactionMeeting",
        foreign_keys=[previous_meeting_id],
        back_populates="previous_meeting",
        uselist=False,
    )
    protocol_approved_in: Mapped["FactionMeeting | None"] = relationship(
        "FactionMeeting",
        remote_side="FactionMeeting.id",
        foreign_keys=[protocol_approved_in_id],
    )
    agenda_items: Mapped[list["FactionAgendaItem"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    attendances: Mapped[list["FactionAttendance"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    invitations: Mapped[list["FactionMeetingInvitation"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    protocol_revisions: Mapped[list["FactionProtocolRevision"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )

    @property
    def is_past(self) -> bool:
        """Check if the meeting is in the past."""
        return self.scheduled_date < datetime.now(self.scheduled_date.tzinfo)

    @property
    def can_start(self) -> bool:
        """Check if the meeting can be started."""
        return self.status == MeetingStatus.SCHEDULED

    @property
    def is_running(self) -> bool:
        """Check if the meeting is currently running."""
        return self.status == MeetingStatus.IN_PROGRESS

    @property
    def can_edit_protocol(self) -> bool:
        """Check if the protocol can be edited."""
        return not self.protocol_locked

    @property
    def has_pending_approval(self) -> bool:
        """Check if there's a previous meeting protocol pending approval."""
        return self.previous_meeting is not None and not self.previous_meeting.protocol_approved


class FactionAgendaItem(Base):
    """
    An agenda item for a faction meeting.
    Supports hierarchy (sub-items like 5.1, 5.2) and approval workflow.
    """

    __tablename__ = "faction_agenda_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("faction_meetings.id"))

    # Hierarchy (for sub-items)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("faction_agenda_items.id"), nullable=True
    )

    # Basic info
    number: Mapped[str] = mapped_column(String(20), default="")  # Auto-generated: 1, 2, 5.1
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Visibility and ordering
    visibility: Mapped[AgendaItemVisibility] = mapped_column(
        Enum(AgendaItemVisibility, name="agenda_item_visibility"),
        default=AgendaItemVisibility.INTERNAL,
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_public_section: Mapped[bool] = mapped_column(Boolean, default=False)

    # Special item types
    is_approval_item: Mapped[bool] = mapped_column(Boolean, default=False)  # TOP 1
    is_various_item: Mapped[bool] = mapped_column(Boolean, default=False)  # "Verschiedenes"

    # Time estimate
    estimated_duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Suggestion workflow
    suggestion_status: Mapped[AgendaSuggestionStatus] = mapped_column(
        Enum(AgendaSuggestionStatus, name="agenda_suggestion_status"),
        default=AgendaSuggestionStatus.APPROVED,
    )
    suggested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memberships.id"), nullable=True
    )
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memberships.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Links to related content
    related_paper_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("oparl_papers.id"), nullable=True
    )
    related_motion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("motions.id"), nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    meeting: Mapped["FactionMeeting"] = relationship(back_populates="agenda_items")
    parent: Mapped["FactionAgendaItem | None"] = relationship(
        "FactionAgendaItem",
        remote_side="FactionAgendaItem.id",
        back_populates="sub_items",
    )
    sub_items: Mapped[list["FactionAgendaItem"]] = relationship(
        "FactionAgendaItem",
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    suggested_by: Mapped["Membership | None"] = relationship(
        foreign_keys=[suggested_by_id]
    )
    approved_by: Mapped["Membership | None"] = relationship(
        foreign_keys=[approved_by_id]
    )
    related_motion: Mapped["Motion | None"] = relationship(
        back_populates="faction_agenda_items"
    )
    decisions: Mapped[list["FactionDecision"]] = relationship(
        back_populates="agenda_item", cascade="all, delete-orphan"
    )
    protocol_entries: Mapped[list["FactionProtocolEntry"]] = relationship(
        back_populates="agenda_item", cascade="all, delete-orphan"
    )

    @property
    def display_number(self) -> str:
        """Get display number including parent number for sub-items."""
        if self.parent:
            return f"{self.parent.display_number}.{self.sort_order + 1}"
        return self.number or str(self.sort_order + 1)

    @property
    def is_sub_item(self) -> bool:
        """Check if this is a sub-item."""
        return self.parent_id is not None


class FactionDecision(Base):
    """
    A decision/resolution for an agenda item.
    Records voting results and links to motions.
    """

    __tablename__ = "faction_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agenda_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("faction_agenda_items.id")
    )

    # Decision details
    decision_type: Mapped[DecisionType] = mapped_column(
        Enum(DecisionType, name="decision_type"),
    )
    decision_text: Mapped[str] = mapped_column(Text)

    # Voting results
    votes_for: Mapped[int | None] = mapped_column(Integer, nullable=True)
    votes_against: Mapped[int | None] = mapped_column(Integer, nullable=True)
    votes_abstain: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_unanimous: Mapped[bool] = mapped_column(Boolean, default=False)

    # Link to motion (if this decision affects a motion)
    motion_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("motions.id"), nullable=True
    )

    # Metadata
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Relationships
    agenda_item: Mapped["FactionAgendaItem"] = relationship(back_populates="decisions")
    motion: Mapped["Motion | None"] = relationship(back_populates="faction_decisions")
    created_by: Mapped["Membership"] = relationship(foreign_keys=[created_by_id])


class FactionAttendance(Base):
    """
    Attendance record for a faction meeting.
    Supports members and guests, with check-in/out tracking.
    """

    __tablename__ = "faction_attendances"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("faction_meetings.id"))

    # Member or guest
    membership_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memberships.id"), nullable=True
    )
    guest_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)

    # Attendance details
    attendance_type: Mapped[AttendanceType] = mapped_column(
        Enum(AttendanceType, name="attendance_type"),
        default=AttendanceType.IN_PERSON,
    )
    status: Mapped[AttendanceStatus] = mapped_column(
        Enum(AttendanceStatus, name="attendance_status"),
        default=AttendanceStatus.PRESENT,
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Check-in/out tracking
    check_in_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    check_out_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_checked_in: Mapped[bool] = mapped_column(Boolean, default=False)
    last_activity_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Calculated duration
    calculated_duration_minutes: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    # Digital signature for verification
    signature_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    signature_ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Manual adjustment tracking
    manually_adjusted: Mapped[bool] = mapped_column(Boolean, default=False)
    adjustment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    adjusted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memberships.id"), nullable=True
    )
    adjusted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Metadata
    recorded_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    # Timestamps
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    meeting: Mapped["FactionMeeting"] = relationship(back_populates="attendances")
    membership: Mapped["Membership | None"] = relationship(
        foreign_keys=[membership_id], back_populates="faction_attendances"
    )
    recorded_by: Mapped["Membership"] = relationship(foreign_keys=[recorded_by_id])
    adjusted_by: Mapped["Membership | None"] = relationship(
        foreign_keys=[adjusted_by_id]
    )

    @property
    def display_name(self) -> str:
        """Get display name (member name or guest name)."""
        if self.is_guest:
            return self.guest_name or "Unbekannter Gast"
        elif self.membership:
            return self.membership.user.name
        return "Unbekannt"

    @property
    def duration_minutes(self) -> int | None:
        """Calculate attendance duration in minutes."""
        if self.calculated_duration_minutes:
            return self.calculated_duration_minutes
        if self.check_in_time and self.check_out_time:
            delta = self.check_out_time - self.check_in_time
            return int(delta.total_seconds() / 60)
        return None


class FactionProtocolEntry(Base):
    """
    A protocol entry for an agenda item.
    Supports different entry types (discussion, decision, task, etc.).
    """

    __tablename__ = "faction_protocol_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agenda_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("faction_agenda_items.id")
    )

    # Entry details
    entry_type: Mapped[ProtocolEntryType] = mapped_column(
        Enum(ProtocolEntryType, name="protocol_entry_type"),
        default=ProtocolEntryType.DISCUSSION,
    )
    content: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # For action items
    assigned_to_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memberships.id"), nullable=True
    )
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Visibility override (inherits from agenda item by default)
    visibility_override: Mapped[AgendaItemVisibility | None] = mapped_column(
        Enum(AgendaItemVisibility, name="agenda_item_visibility", create_constraint=False),
        nullable=True,
    )

    # Metadata
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    agenda_item: Mapped["FactionAgendaItem"] = relationship(
        back_populates="protocol_entries"
    )
    assigned_to: Mapped["Membership | None"] = relationship(
        foreign_keys=[assigned_to_id]
    )
    created_by: Mapped["Membership"] = relationship(foreign_keys=[created_by_id])

    @property
    def effective_visibility(self) -> AgendaItemVisibility:
        """Get effective visibility (override or inherit from agenda item)."""
        return self.visibility_override or self.agenda_item.visibility


class FactionProtocolRevision(Base):
    """
    Revision-safe snapshot of a meeting protocol.
    Created when protocol is locked or approved.
    """

    __tablename__ = "faction_protocol_revisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("faction_meetings.id"))

    # Revision info
    revision_number: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(
        String(50), default="draft"
    )  # draft, finalized, approved, correction

    # Full snapshot as JSON
    content_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)

    # Hash for integrity verification
    content_hash: Mapped[str] = mapped_column(String(64))

    # Notes
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Metadata
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("meeting_id", "revision_number", name="uq_meeting_revision"),
    )

    # Relationships
    meeting: Mapped["FactionMeeting"] = relationship(back_populates="protocol_revisions")
    created_by: Mapped["Membership"] = relationship(foreign_keys=[created_by_id])

    def verify_integrity(self) -> bool:
        """Verify the integrity of the stored protocol."""
        import hashlib
        import json

        content_str = json.dumps(self.content_snapshot, sort_keys=True, ensure_ascii=False)
        calculated_hash = hashlib.sha256(content_str.encode()).hexdigest()
        return calculated_hash == self.content_hash


class FactionMeetingInvitation(Base):
    """
    Meeting invitation sent to a member.
    Tracks RSVP status and email sending.
    """

    __tablename__ = "faction_meeting_invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("faction_meetings.id"))
    membership_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    # RSVP status
    rsvp_status: Mapped[RSVPStatus] = mapped_column(
        Enum(RSVPStatus, name="rsvp_status"),
        default=RSVPStatus.PENDING,
    )
    rsvp_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rsvp_responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Email tracking
    invitation_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Metadata
    invited_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memberships.id"))

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("meeting_id", "membership_id", name="uq_meeting_member_invitation"),
    )

    # Relationships
    meeting: Mapped["FactionMeeting"] = relationship(back_populates="invitations")
    membership: Mapped["Membership"] = relationship(
        foreign_keys=[membership_id], back_populates="meeting_invitations"
    )
    invited_by: Mapped["Membership"] = relationship(foreign_keys=[invited_by_id])

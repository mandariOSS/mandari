"""
Work Module Pydantic Schemas

API request/response schemas for the work module (political organizations).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.work.models import (
    AgendaItemVisibility,
    AgendaSuggestionStatus,
    ApprovalStatus,
    AttendanceStatus,
    AttendanceType,
    CoalitionResult,
    CoAuthorInviteStatus,
    DecisionType,
    MeetingStatus,
    MembershipRole,
    MotionStatus,
    MotionVisibility,
    ProtocolEntryType,
    RSVPStatus,
    ShareMethod,
    WorkGroupRole,
)


# =============================================================================
# User Schemas
# =============================================================================


class UserBase(BaseModel):
    """Base schema for user."""

    email: EmailStr
    name: str


class UserCreate(UserBase):
    """Schema for creating a user."""

    pass


class UserResponse(UserBase):
    """Schema for user response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    is_active: bool
    created_at: datetime


class UserBrief(BaseModel):
    """Brief user info for embedding in other responses."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    email: str


# =============================================================================
# Organization Schemas
# =============================================================================


class OrganizationBase(BaseModel):
    """Base schema for organization."""

    name: str
    short_name: str | None = None
    slug: str


class OrganizationCreate(OrganizationBase):
    """Schema for creating an organization."""

    parent_id: UUID | None = None
    oparl_organization_id: UUID | None = None
    oparl_body_id: UUID | None = None
    primary_color: str | None = None
    secondary_color: str | None = None


class OrganizationUpdate(BaseModel):
    """Schema for updating an organization."""

    name: str | None = None
    short_name: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    settings: dict[str, Any] | None = None


class OrganizationResponse(OrganizationBase):
    """Schema for organization response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    parent_id: UUID | None = None
    oparl_organization_id: UUID | None = None
    oparl_body_id: UUID | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class OrganizationDetail(OrganizationResponse):
    """Detailed organization response."""

    settings: dict[str, Any]


# =============================================================================
# Membership Schemas
# =============================================================================


class MembershipBase(BaseModel):
    """Base schema for membership."""

    role: MembershipRole = MembershipRole.MEMBER
    title: str | None = None
    bio: str | None = None


class MembershipCreate(MembershipBase):
    """Schema for creating a membership."""

    user_id: UUID
    organization_id: UUID
    oparl_person_id: UUID | None = None


class MembershipUpdate(BaseModel):
    """Schema for updating a membership."""

    role: MembershipRole | None = None
    title: str | None = None
    bio: str | None = None
    permissions: dict[str, Any] | None = None
    is_active: bool | None = None


class MembershipResponse(MembershipBase):
    """Schema for membership response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    organization_id: UUID
    oparl_person_id: UUID | None = None
    permissions: dict[str, Any]
    is_active: bool
    joined_at: datetime
    left_at: datetime | None = None

    # Computed properties
    is_council_member: bool
    is_expert_citizen: bool
    can_approve_motions: bool
    can_add_to_agenda: bool


class MembershipBrief(BaseModel):
    """Brief membership info for embedding."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: MembershipRole
    title: str | None = None


class MembershipWithUser(MembershipResponse):
    """Membership with user details."""

    user: UserBrief


# =============================================================================
# Council Party Schemas
# =============================================================================


class CouncilPartyBase(BaseModel):
    """Base schema for council party."""

    name: str
    short_name: str | None = None
    email: EmailStr | None = None
    contact_name: str | None = None
    is_coalition_member: bool = False
    coalition_order: int = 0


class CouncilPartyCreate(CouncilPartyBase):
    """Schema for creating a council party."""

    organization_id: UUID
    oparl_organization_id: UUID | None = None


class CouncilPartyUpdate(BaseModel):
    """Schema for updating a council party."""

    name: str | None = None
    short_name: str | None = None
    email: EmailStr | None = None
    contact_name: str | None = None
    is_coalition_member: bool | None = None
    coalition_order: int | None = None
    is_active: bool | None = None


class CouncilPartyResponse(CouncilPartyBase):
    """Schema for council party response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    oparl_organization_id: UUID | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# =============================================================================
# Motion Type Schemas
# =============================================================================


class MotionTypeBase(BaseModel):
    """Base schema for motion type."""

    name: str
    slug: str
    description: str | None = None
    icon: str | None = None
    color: str | None = None


class MotionTypeCreate(MotionTypeBase):
    """Schema for creating a motion type."""

    organization_id: UUID
    requires_approval: bool = True
    requires_coalition_approval: bool = False
    requires_faction_decision: bool = False
    default_approvers: list[str] = Field(default_factory=list)
    workflow_steps: list[dict[str, Any]] = Field(default_factory=list)
    allowed_creator_roles: list[str] = Field(default_factory=list)
    recommend_co_author: bool = False
    recommend_co_author_roles: list[str] = Field(default_factory=list)
    co_author_recommendation_message: str | None = None
    display_order: int = 0


class MotionTypeUpdate(BaseModel):
    """Schema for updating a motion type."""

    name: str | None = None
    description: str | None = None
    icon: str | None = None
    color: str | None = None
    requires_approval: bool | None = None
    requires_coalition_approval: bool | None = None
    requires_faction_decision: bool | None = None
    default_approvers: list[str] | None = None
    workflow_steps: list[dict[str, Any]] | None = None
    allowed_creator_roles: list[str] | None = None
    recommend_co_author: bool | None = None
    recommend_co_author_roles: list[str] | None = None
    co_author_recommendation_message: str | None = None
    display_order: int | None = None
    is_active: bool | None = None


class MotionTypeResponse(MotionTypeBase):
    """Schema for motion type response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    requires_approval: bool
    requires_coalition_approval: bool
    requires_faction_decision: bool
    default_approvers: list[str]
    workflow_steps: list[dict[str, Any]]
    allowed_creator_roles: list[str]
    recommend_co_author: bool
    recommend_co_author_roles: list[str]
    co_author_recommendation_message: str | None = None
    display_order: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


# =============================================================================
# Motion Schemas
# =============================================================================


class MotionBase(BaseModel):
    """Base schema for motion."""

    title: str
    content: str = ""
    summary: str | None = None


class MotionCreate(MotionBase):
    """Schema for creating a motion."""

    organization_id: UUID
    motion_type_id: UUID | None = None
    visibility: MotionVisibility = MotionVisibility.PRIVATE
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MotionUpdate(BaseModel):
    """Schema for updating a motion."""

    title: str | None = None
    content: str | None = None
    summary: str | None = None
    visibility: MotionVisibility | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None
    assigned_workgroup_id: UUID | None = None


class MotionStatusUpdate(BaseModel):
    """Schema for updating motion status."""

    status: MotionStatus
    comment: str | None = None


class MotionResponse(MotionBase):
    """Schema for motion response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    motion_type_id: UUID | None = None
    status: MotionStatus
    visibility: MotionVisibility
    created_by_id: UUID
    assigned_workgroup_id: UUID | None = None
    workgroup_recommendation: str | None = None
    workgroup_recommendation_note: str | None = None
    oparl_paper_id: UUID | None = None
    is_archived: bool
    archived_at: datetime | None = None
    tags: list[str]
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None = None

    # Computed
    coalition_status: str


class MotionBrief(BaseModel):
    """Brief motion info for lists."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    status: MotionStatus
    created_at: datetime


class MotionDetail(MotionResponse):
    """Detailed motion response with related data."""

    created_by: MembershipBrief
    motion_type: MotionTypeResponse | None = None
    co_authors: list[MembershipBrief]
    approvals: list["MotionApprovalResponse"]
    coalition_consultations: list["CoalitionConsultationResponse"]


# =============================================================================
# Co-Author Invite Schemas
# =============================================================================


class CoAuthorInviteCreate(BaseModel):
    """Schema for creating a co-author invite."""

    motion_id: UUID
    invited_member_id: UUID
    message: str | None = None


class CoAuthorInviteResponse(BaseModel):
    """Schema for co-author invite response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    motion_id: UUID
    invited_by_id: UUID
    invited_member_id: UUID
    status: CoAuthorInviteStatus
    message: str | None = None
    created_at: datetime
    responded_at: datetime | None = None


class CoAuthorInviteUpdate(BaseModel):
    """Schema for responding to a co-author invite."""

    status: CoAuthorInviteStatus


# =============================================================================
# Motion Approval Schemas
# =============================================================================


class MotionApprovalCreate(BaseModel):
    """Schema for creating an approval request."""

    motion_id: UUID
    approval_type: str


class MotionApprovalResponse(BaseModel):
    """Schema for approval response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    motion_id: UUID
    approval_type: str
    status: ApprovalStatus
    approved_by_id: UUID | None = None
    comment: str | None = None
    created_at: datetime
    responded_at: datetime | None = None


class MotionApprovalUpdate(BaseModel):
    """Schema for responding to an approval request."""

    status: ApprovalStatus
    comment: str | None = None


# =============================================================================
# Coalition Consultation Schemas
# =============================================================================


class CoalitionConsultationCreate(BaseModel):
    """Schema for creating a coalition consultation."""

    motion_id: UUID
    party_id: UUID


class CoalitionConsultationResponse(BaseModel):
    """Schema for coalition consultation response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    motion_id: UUID
    party_id: UUID
    result: CoalitionResult
    sent_at: datetime | None = None
    sent_by_id: UUID | None = None
    sent_via: str | None = None
    response_received_at: datetime | None = None
    response_note: str | None = None
    created_at: datetime
    updated_at: datetime


class CoalitionConsultationWithParty(CoalitionConsultationResponse):
    """Coalition consultation with party details."""

    party: CouncilPartyResponse


class CoalitionConsultationUpdate(BaseModel):
    """Schema for updating a coalition consultation."""

    result: CoalitionResult
    response_note: str | None = None


class CoalitionSendRequest(BaseModel):
    """Schema for sending motion to coalition."""

    party_ids: list[UUID] | None = None  # None = all coalition parties
    custom_message: str | None = None
    send_via: str = "email"


# =============================================================================
# Motion Share Schemas
# =============================================================================


class MotionShareRequest(BaseModel):
    """Schema for sharing a motion."""

    party_id: UUID | None = None
    email: EmailStr | None = None
    method: ShareMethod = ShareMethod.EMAIL
    custom_message: str | None = None


class MotionShareLogResponse(BaseModel):
    """Schema for share log response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    motion_id: UUID
    shared_by_id: UUID
    shared_with_party_id: UUID | None = None
    shared_with_email: str | None = None
    method: ShareMethod
    success: bool
    error_message: str | None = None
    note: str | None = None
    shared_at: datetime


# =============================================================================
# Working Group Schemas
# =============================================================================


class WorkGroupBase(BaseModel):
    """Base schema for working group."""

    name: str
    slug: str
    description: str | None = None


class WorkGroupCreate(WorkGroupBase):
    """Schema for creating a working group."""

    organization_id: UUID
    speaker_id: UUID | None = None


class WorkGroupUpdate(BaseModel):
    """Schema for updating a working group."""

    name: str | None = None
    description: str | None = None
    speaker_id: UUID | None = None
    is_active: bool | None = None


class WorkGroupResponse(WorkGroupBase):
    """Schema for working group response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    speaker_id: UUID | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WorkGroupDetail(WorkGroupResponse):
    """Detailed working group response."""

    speaker: MembershipBrief | None = None
    members: list["WorkGroupMembershipResponse"]


class WorkGroupMembershipCreate(BaseModel):
    """Schema for adding a member to a working group."""

    workgroup_id: UUID
    member_id: UUID
    role: WorkGroupRole = WorkGroupRole.MEMBER


class WorkGroupMembershipResponse(BaseModel):
    """Schema for working group membership response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workgroup_id: UUID
    member_id: UUID
    role: WorkGroupRole
    joined_at: datetime
    left_at: datetime | None = None


class WorkGroupRecommendation(BaseModel):
    """Schema for working group recommendation on a motion."""

    recommendation: str = Field(..., pattern="^(approve|reject|modify)$")
    note: str | None = None


# =============================================================================
# Workflow Schemas
# =============================================================================


class WorkflowStatusResponse(BaseModel):
    """Schema for workflow status response."""

    motion_id: UUID
    current_step: str
    required_steps: list[str]
    completed_steps: list[str]
    can_proceed: bool
    block_reason: str | None = None
    next_actions: list[str]


class SubmitForApprovalRequest(BaseModel):
    """Schema for submitting motion for approval."""

    comment: str | None = None


class SubmitToCoalitionRequest(BaseModel):
    """Schema for submitting motion to coalition."""

    custom_message: str | None = None


class ScheduleForMeetingRequest(BaseModel):
    """Schema for scheduling motion for meeting."""

    meeting_id: UUID
    agenda_position: int | None = None


# =============================================================================
# Pagination
# =============================================================================


class PaginatedResponse(BaseModel):
    """Generic paginated response."""

    items: list[Any]
    total: int
    page: int
    page_size: int
    pages: int


# =============================================================================
# Faction Meeting Settings Schemas
# =============================================================================


class FactionMeetingSettingsBase(BaseModel):
    """Base schema for meeting settings."""

    default_location: str | None = None
    default_conference_link: str | None = None
    default_start_time: str | None = None
    default_duration_minutes: int = 120
    auto_create_approval_item: bool = True
    auto_create_various_item: bool = True
    political_work_section_title: str = "Politische Arbeit: Vorlagen und Anträge"
    press_section_title: str = "SoMe & Presse"
    invitation_enabled: bool = True
    invitation_days_before: int = 7
    reminder_1_day_enabled: bool = True
    protocol_notification_enabled: bool = True
    require_active_checkout: bool = True
    attendance_timeout_minutes: int = 120
    auto_lock_protocol_on_end: bool = True


class FactionMeetingSettingsCreate(FactionMeetingSettingsBase):
    """Schema for creating meeting settings."""

    organization_id: UUID


class FactionMeetingSettingsUpdate(BaseModel):
    """Schema for updating meeting settings."""

    default_location: str | None = None
    default_conference_link: str | None = None
    default_start_time: str | None = None
    default_duration_minutes: int | None = None
    auto_create_approval_item: bool | None = None
    auto_create_various_item: bool | None = None
    political_work_section_title: str | None = None
    press_section_title: str | None = None
    invitation_enabled: bool | None = None
    invitation_days_before: int | None = None
    reminder_1_day_enabled: bool | None = None
    protocol_notification_enabled: bool | None = None
    require_active_checkout: bool | None = None
    attendance_timeout_minutes: int | None = None
    auto_lock_protocol_on_end: bool | None = None
    email_templates: dict[str, Any] | None = None


class FactionMeetingSettingsResponse(FactionMeetingSettingsBase):
    """Schema for meeting settings response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    email_templates: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# =============================================================================
# Faction Meeting Schemas
# =============================================================================


class FactionMeetingBase(BaseModel):
    """Base schema for faction meeting."""

    title: str
    description: str | None = None
    scheduled_date: datetime
    scheduled_end: datetime | None = None
    location: str | None = None
    conference_link: str | None = None
    conference_details: str | None = None


class FactionMeetingCreate(FactionMeetingBase):
    """Schema for creating a faction meeting."""

    organization_id: UUID
    previous_meeting_id: UUID | None = None


class FactionMeetingUpdate(BaseModel):
    """Schema for updating a faction meeting."""

    title: str | None = None
    description: str | None = None
    scheduled_date: datetime | None = None
    scheduled_end: datetime | None = None
    location: str | None = None
    conference_link: str | None = None
    conference_details: str | None = None


class FactionMeetingResponse(FactionMeetingBase):
    """Schema for faction meeting response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    public_id: str
    status: MeetingStatus
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    previous_meeting_id: UUID | None = None
    protocol_approved: bool
    protocol_approved_at: datetime | None = None
    protocol_locked: bool
    protocol_locked_at: datetime | None = None
    attendance_locked: bool
    public_protocol_enabled: bool
    created_by_id: UUID
    created_at: datetime
    updated_at: datetime


class FactionMeetingBrief(BaseModel):
    """Brief meeting info for lists."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    public_id: str
    title: str
    scheduled_date: datetime
    status: MeetingStatus


class FactionMeetingDetail(FactionMeetingResponse):
    """Detailed meeting response with related data."""

    created_by: MembershipBrief
    previous_meeting: "FactionMeetingBrief | None" = None
    agenda_item_count: int = 0
    attendance_count: int = 0


# =============================================================================
# Faction Agenda Item Schemas
# =============================================================================


class FactionAgendaItemBase(BaseModel):
    """Base schema for agenda item."""

    title: str
    description: str | None = None
    visibility: AgendaItemVisibility = AgendaItemVisibility.INTERNAL
    estimated_duration_minutes: int | None = None


class FactionAgendaItemCreate(FactionAgendaItemBase):
    """Schema for creating an agenda item."""

    meeting_id: UUID
    parent_id: UUID | None = None
    related_paper_id: UUID | None = None
    related_motion_id: UUID | None = None


class FactionAgendaItemUpdate(BaseModel):
    """Schema for updating an agenda item."""

    title: str | None = None
    description: str | None = None
    visibility: AgendaItemVisibility | None = None
    estimated_duration_minutes: int | None = None
    sort_order: int | None = None


class FactionAgendaItemResponse(FactionAgendaItemBase):
    """Schema for agenda item response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    meeting_id: UUID
    parent_id: UUID | None = None
    number: str
    sort_order: int
    is_public_section: bool
    is_approval_item: bool
    is_various_item: bool
    suggestion_status: AgendaSuggestionStatus
    suggested_by_id: UUID | None = None
    approved_by_id: UUID | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None
    related_paper_id: UUID | None = None
    related_motion_id: UUID | None = None
    created_at: datetime
    updated_at: datetime

    # Computed
    display_number: str
    is_sub_item: bool


class FactionAgendaItemDetail(FactionAgendaItemResponse):
    """Detailed agenda item with sub-items and entries."""

    sub_items: list["FactionAgendaItemResponse"] = Field(default_factory=list)
    decisions: list["FactionDecisionResponse"] = Field(default_factory=list)
    protocol_entries: list["FactionProtocolEntryResponse"] = Field(default_factory=list)
    related_motion: MotionBrief | None = None


class AgendaReorderRequest(BaseModel):
    """Schema for reordering agenda items."""

    item_orders: list[dict[str, Any]]  # [{id: UUID, sort_order: int}, ...]


class AddMotionToAgendaRequest(BaseModel):
    """Schema for adding a motion to agenda."""

    motion_id: UUID
    parent_id: UUID | None = None  # Parent agenda item (e.g., "Politische Arbeit")


# =============================================================================
# Faction Decision Schemas
# =============================================================================


class FactionDecisionBase(BaseModel):
    """Base schema for decision."""

    decision_type: DecisionType
    decision_text: str
    votes_for: int | None = None
    votes_against: int | None = None
    votes_abstain: int | None = None
    is_unanimous: bool = False


class FactionDecisionCreate(FactionDecisionBase):
    """Schema for creating a decision."""

    agenda_item_id: UUID
    motion_id: UUID | None = None


class FactionDecisionResponse(FactionDecisionBase):
    """Schema for decision response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agenda_item_id: UUID
    motion_id: UUID | None = None
    created_by_id: UUID
    created_at: datetime


# =============================================================================
# Faction Attendance Schemas
# =============================================================================


class FactionAttendanceBase(BaseModel):
    """Base schema for attendance."""

    attendance_type: AttendanceType = AttendanceType.IN_PERSON
    status: AttendanceStatus = AttendanceStatus.PRESENT
    note: str | None = None


class FactionAttendanceCreate(FactionAttendanceBase):
    """Schema for creating attendance."""

    meeting_id: UUID
    membership_id: UUID | None = None
    guest_name: str | None = None
    is_guest: bool = False


class FactionAttendanceUpdate(BaseModel):
    """Schema for updating attendance."""

    attendance_type: AttendanceType | None = None
    status: AttendanceStatus | None = None
    note: str | None = None


class FactionAttendanceResponse(FactionAttendanceBase):
    """Schema for attendance response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    meeting_id: UUID
    membership_id: UUID | None = None
    guest_name: str | None = None
    is_guest: bool
    check_in_time: datetime | None = None
    check_out_time: datetime | None = None
    is_checked_in: bool
    calculated_duration_minutes: int | None = None
    manually_adjusted: bool
    recorded_by_id: UUID
    recorded_at: datetime

    # Computed
    display_name: str
    duration_minutes: int | None = None


class CheckInRequest(BaseModel):
    """Schema for check-in request."""

    ip_address: str | None = None
    user_agent: str | None = None


class CheckOutRequest(BaseModel):
    """Schema for check-out request."""

    ip_address: str | None = None
    user_agent: str | None = None


# =============================================================================
# Faction Protocol Entry Schemas
# =============================================================================


class FactionProtocolEntryBase(BaseModel):
    """Base schema for protocol entry."""

    entry_type: ProtocolEntryType = ProtocolEntryType.DISCUSSION
    content: str


class FactionProtocolEntryCreate(FactionProtocolEntryBase):
    """Schema for creating a protocol entry."""

    agenda_item_id: UUID
    assigned_to_id: UUID | None = None
    due_date: datetime | None = None
    visibility_override: AgendaItemVisibility | None = None


class FactionProtocolEntryUpdate(BaseModel):
    """Schema for updating a protocol entry."""

    entry_type: ProtocolEntryType | None = None
    content: str | None = None
    assigned_to_id: UUID | None = None
    due_date: datetime | None = None
    is_completed: bool | None = None
    visibility_override: AgendaItemVisibility | None = None
    sort_order: int | None = None


class FactionProtocolEntryResponse(FactionProtocolEntryBase):
    """Schema for protocol entry response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agenda_item_id: UUID
    sort_order: int
    assigned_to_id: UUID | None = None
    due_date: datetime | None = None
    is_completed: bool
    visibility_override: AgendaItemVisibility | None = None
    created_by_id: UUID
    created_at: datetime
    updated_at: datetime


# =============================================================================
# Faction Protocol Revision Schemas
# =============================================================================


class FactionProtocolRevisionResponse(BaseModel):
    """Schema for protocol revision response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    meeting_id: UUID
    revision_number: int
    reason: str
    content_hash: str
    notes: str | None = None
    created_by_id: UUID
    created_at: datetime


class ProtocolLockRequest(BaseModel):
    """Schema for locking protocol."""

    notes: str | None = None


class ProtocolExportRequest(BaseModel):
    """Schema for protocol export."""

    format: str = "pdf"  # pdf, html, markdown
    include_confidential: bool = False


# =============================================================================
# Faction Meeting Invitation Schemas
# =============================================================================


class FactionMeetingInvitationResponse(BaseModel):
    """Schema for invitation response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    meeting_id: UUID
    membership_id: UUID
    rsvp_status: RSVPStatus
    rsvp_note: str | None = None
    rsvp_responded_at: datetime | None = None
    invitation_sent_at: datetime | None = None
    reminder_sent_at: datetime | None = None
    invited_by_id: UUID
    created_at: datetime
    updated_at: datetime


class RSVPRequest(BaseModel):
    """Schema for RSVP response."""

    status: RSVPStatus
    note: str | None = None


class InviteAllRequest(BaseModel):
    """Schema for inviting all members."""

    send_email: bool = True
    custom_message: str | None = None


class SendInvitationsRequest(BaseModel):
    """Schema for sending invitations."""

    invitation_ids: list[UUID] | None = None  # None = all pending
    custom_message: str | None = None


# =============================================================================
# Public API Schemas (Reduced data for public access)
# =============================================================================


class PublicMeetingResponse(BaseModel):
    """Public meeting response (limited data)."""

    model_config = ConfigDict(from_attributes=True)

    public_id: str
    title: str
    scheduled_date: datetime
    location: str | None = None
    status: MeetingStatus


class PublicAgendaItemResponse(BaseModel):
    """Public agenda item response (only public items)."""

    model_config = ConfigDict(from_attributes=True)

    number: str
    title: str
    description: str | None = None


class PublicDecisionResponse(BaseModel):
    """Public decision response."""

    model_config = ConfigDict(from_attributes=True)

    decision_type: DecisionType
    decision_text: str
    votes_for: int | None = None
    votes_against: int | None = None
    votes_abstain: int | None = None
    is_unanimous: bool


# =============================================================================
# Meeting Lifecycle Schemas
# =============================================================================


class MeetingScheduleRequest(BaseModel):
    """Schema for scheduling a meeting (send invitations)."""

    send_invitations: bool = True
    custom_message: str | None = None


class MeetingStartRequest(BaseModel):
    """Schema for starting a meeting."""

    pass


class MeetingEndRequest(BaseModel):
    """Schema for ending a meeting."""

    lock_protocol: bool = True
    lock_attendance: bool = True


class MeetingCancelRequest(BaseModel):
    """Schema for cancelling a meeting."""

    reason: str | None = None
    notify_members: bool = True


# Update forward references
MotionDetail.model_rebuild()
WorkGroupDetail.model_rebuild()
FactionMeetingDetail.model_rebuild()
FactionAgendaItemDetail.model_rebuild()

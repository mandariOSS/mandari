"""
Work API Router

Endpoints for political organizations (Säule 2: Parteien & Fraktionen).
Implements flexible document workflow with coalition voting.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.auth.dependencies import get_current_user
from src.core.database import get_db
from src.work.models import (
    AgendaSuggestionStatus,
    ApprovalStatus,
    CoalitionConsultation,
    CoalitionResult,
    CouncilParty,
    FactionAgendaItem,
    FactionAttendance,
    FactionDecision,
    FactionMeeting,
    FactionMeetingInvitation,
    FactionProtocolEntry,
    FactionProtocolRevision,
    Membership,
    MeetingStatus,
    Motion,
    MotionApproval,
    MotionCoAuthorInvite,
    MotionShareLog,
    MotionStatus,
    MotionType,
    Organization,
    User,
    WorkGroup,
    WorkGroupMembership,
)
from src.work.schemas import (
    AddMotionToAgendaRequest,
    AgendaReorderRequest,
    CheckInRequest,
    CheckOutRequest,
    CoalitionConsultationResponse,
    CoalitionConsultationUpdate,
    CoalitionConsultationWithParty,
    CoalitionSendRequest,
    CoAuthorInviteCreate,
    CoAuthorInviteResponse,
    CoAuthorInviteUpdate,
    CouncilPartyCreate,
    CouncilPartyResponse,
    CouncilPartyUpdate,
    FactionAgendaItemCreate,
    FactionAgendaItemDetail,
    FactionAgendaItemResponse,
    FactionAgendaItemUpdate,
    FactionAttendanceCreate,
    FactionAttendanceResponse,
    FactionAttendanceUpdate,
    FactionDecisionCreate,
    FactionDecisionResponse,
    FactionMeetingCreate,
    FactionMeetingDetail,
    FactionMeetingInvitationResponse,
    FactionMeetingResponse,
    FactionMeetingSettingsResponse,
    FactionMeetingSettingsUpdate,
    FactionMeetingUpdate,
    FactionProtocolEntryCreate,
    FactionProtocolEntryResponse,
    FactionProtocolEntryUpdate,
    FactionProtocolRevisionResponse,
    InviteAllRequest,
    MeetingCancelRequest,
    MeetingEndRequest,
    MeetingScheduleRequest,
    MeetingStartRequest,
    MembershipCreate,
    MembershipResponse,
    MembershipUpdate,
    MembershipWithUser,
    MotionApprovalResponse,
    MotionApprovalUpdate,
    MotionCreate,
    MotionDetail,
    MotionResponse,
    MotionShareLogResponse,
    MotionShareRequest,
    MotionStatusUpdate,
    MotionTypeCreate,
    MotionTypeResponse,
    MotionTypeUpdate,
    MotionUpdate,
    OrganizationCreate,
    OrganizationDetail,
    OrganizationResponse,
    OrganizationUpdate,
    PaginatedResponse,
    ProtocolLockRequest,
    PublicAgendaItemResponse,
    PublicDecisionResponse,
    PublicMeetingResponse,
    RSVPRequest,
    ScheduleForMeetingRequest,
    SendInvitationsRequest,
    SubmitForApprovalRequest,
    SubmitToCoalitionRequest,
    WorkflowStatusResponse,
    WorkGroupCreate,
    WorkGroupDetail,
    WorkGroupMembershipCreate,
    WorkGroupMembershipResponse,
    WorkGroupRecommendation,
    WorkGroupResponse,
    WorkGroupUpdate,
)
from src.work.services import (
    AgendaSuggestionService,
    AttendanceService,
    CoAuthorService,
    DocumentWorkflowService,
    MeetingInvitationService,
    MeetingLifecycleService,
    MeetingSetupService,
    MotionAgendaService,
    MotionShareService,
    MotionTypeService,
    ProtocolService,
)

router = APIRouter(prefix="/work", tags=["work"])


# =============================================================================
# Helper Functions
# =============================================================================


async def get_membership(
    db: AsyncSession,
    user_id: str,
    org_id: UUID,
) -> Membership:
    """Get membership for user in organization."""
    # For now, create a mock membership since auth is placeholder
    # TODO: Replace with actual user lookup
    result = await db.execute(
        select(User).where(User.email == "user@example.com")
    )
    user = result.scalar_one_or_none()

    if not user:
        # Create placeholder user
        user = User(email="user@example.com", name="Test User")
        db.add(user)
        await db.flush()

    result = await db.execute(
        select(Membership)
        .where(Membership.user_id == user.id)
        .where(Membership.organization_id == org_id)
    )
    membership = result.scalar_one_or_none()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this organization",
        )

    return membership


async def get_motion_or_404(
    db: AsyncSession,
    motion_id: UUID,
    org_id: UUID,
) -> Motion:
    """Get motion or raise 404."""
    result = await db.execute(
        select(Motion)
        .where(Motion.id == motion_id)
        .where(Motion.organization_id == org_id)
        .options(
            selectinload(Motion.motion_type),
            selectinload(Motion.created_by),
            selectinload(Motion.co_authors),
            selectinload(Motion.approvals),
            selectinload(Motion.coalition_consultations),
        )
    )
    motion = result.scalar_one_or_none()

    if not motion:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Motion not found",
        )

    return motion


# =============================================================================
# Index
# =============================================================================


@router.get("/")
async def work_index() -> dict:
    """Work API index - shows available endpoints."""
    return {
        "message": "Mandari Work API - Portal für politische Organisationen",
        "description": "Flexibler Dokumenten-Workflow mit Koalitionsabstimmung",
        "endpoints": {
            "organizations": "/api/v1/work/org - Organisationen verwalten",
            "motions": "/api/v1/work/org/{id}/motions - Dokumente & Anträge",
            "workflow": "/api/v1/work/org/{id}/motions/{id}/workflow - Workflow-Steuerung",
            "coalition": "/api/v1/work/org/{id}/motions/{id}/coalition - Koalitionsabstimmung",
            "parties": "/api/v1/work/org/{id}/parties - Ratsparteien",
            "workgroups": "/api/v1/work/org/{id}/workgroups - Arbeitsgruppen",
        },
    }


# =============================================================================
# Organizations
# =============================================================================


@router.get("/org", response_model=list[OrganizationResponse])
async def list_my_organizations(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[OrganizationResponse]:
    """List organizations the current user belongs to."""
    # TODO: Filter by actual user memberships
    result = await db.execute(
        select(Organization).where(Organization.is_active == True)  # noqa: E712
    )
    return list(result.scalars().all())


@router.post("/org", response_model=OrganizationResponse, status_code=status.HTTP_201_CREATED)
async def create_organization(
    data: OrganizationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> OrganizationResponse:
    """Create a new organization."""
    org = Organization(**data.model_dump())
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


@router.get("/org/{org_id}", response_model=OrganizationDetail)
async def get_organization_detail(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> OrganizationDetail:
    """Get details of an organization."""
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()

    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    return org


@router.patch("/org/{org_id}", response_model=OrganizationResponse)
async def update_organization(
    org_id: UUID,
    data: OrganizationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> OrganizationResponse:
    """Update an organization."""
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()

    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(org, key, value)

    await db.commit()
    await db.refresh(org)
    return org


# =============================================================================
# Members
# =============================================================================


@router.get("/org/{org_id}/members", response_model=list[MembershipWithUser])
async def list_members(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[MembershipWithUser]:
    """List members of an organization."""
    result = await db.execute(
        select(Membership)
        .where(Membership.organization_id == org_id)
        .where(Membership.is_active == True)  # noqa: E712
        .options(selectinload(Membership.user))
    )
    return list(result.scalars().all())


@router.post(
    "/org/{org_id}/members",
    response_model=MembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    org_id: UUID,
    data: MembershipCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MembershipResponse:
    """Add a member to an organization."""
    membership = Membership(
        user_id=data.user_id,
        organization_id=org_id,
        role=data.role,
        title=data.title,
        bio=data.bio,
        oparl_person_id=data.oparl_person_id,
    )
    db.add(membership)
    await db.commit()
    await db.refresh(membership)
    return membership


@router.patch("/org/{org_id}/members/{member_id}", response_model=MembershipResponse)
async def update_member(
    org_id: UUID,
    member_id: UUID,
    data: MembershipUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MembershipResponse:
    """Update a member's details."""
    result = await db.execute(
        select(Membership)
        .where(Membership.id == member_id)
        .where(Membership.organization_id == org_id)
    )
    membership = result.scalar_one_or_none()

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found",
        )

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(membership, key, value)

    await db.commit()
    await db.refresh(membership)
    return membership


# =============================================================================
# Motion Types
# =============================================================================


@router.get("/org/{org_id}/motion-types", response_model=list[MotionTypeResponse])
async def list_motion_types(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[MotionTypeResponse]:
    """List motion types for an organization."""
    service = MotionTypeService(db)
    return await service.get_types_for_organization(org_id)


@router.post(
    "/org/{org_id}/motion-types",
    response_model=MotionTypeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_motion_type(
    org_id: UUID,
    data: MotionTypeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MotionTypeResponse:
    """Create a new motion type."""
    motion_type = MotionType(
        organization_id=org_id,
        **data.model_dump(exclude={"organization_id"}),
    )
    db.add(motion_type)
    await db.commit()
    await db.refresh(motion_type)
    return motion_type


@router.patch("/org/{org_id}/motion-types/{type_id}", response_model=MotionTypeResponse)
async def update_motion_type(
    org_id: UUID,
    type_id: UUID,
    data: MotionTypeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MotionTypeResponse:
    """Update a motion type."""
    result = await db.execute(
        select(MotionType)
        .where(MotionType.id == type_id)
        .where(MotionType.organization_id == org_id)
    )
    motion_type = result.scalar_one_or_none()

    if not motion_type:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Motion type not found",
        )

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(motion_type, key, value)

    await db.commit()
    await db.refresh(motion_type)
    return motion_type


@router.post("/org/{org_id}/motion-types/seed-defaults")
async def seed_default_motion_types(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Seed default motion types for an organization."""
    defaults = MotionTypeService.get_default_types()
    created = 0

    for type_data in defaults:
        # Check if already exists
        result = await db.execute(
            select(MotionType)
            .where(MotionType.organization_id == org_id)
            .where(MotionType.slug == type_data["slug"])
        )
        if result.scalar_one_or_none():
            continue

        motion_type = MotionType(organization_id=org_id, **type_data)
        db.add(motion_type)
        created += 1

    await db.commit()
    return {"created": created, "message": f"{created} motion types created"}


# =============================================================================
# Motions
# =============================================================================


@router.get("/org/{org_id}/motions", response_model=list[MotionResponse])
async def list_motions(
    org_id: UUID,
    status_filter: MotionStatus | None = None,
    type_id: UUID | None = None,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[MotionResponse]:
    """List motions for an organization."""
    query = (
        select(Motion)
        .where(Motion.organization_id == org_id)
        .where(Motion.is_archived == False)  # noqa: E712
    )

    if status_filter:
        query = query.where(Motion.status == status_filter)
    if type_id:
        query = query.where(Motion.motion_type_id == type_id)

    query = query.order_by(Motion.updated_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    return list(result.scalars().all())


@router.post(
    "/org/{org_id}/motions",
    response_model=MotionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_motion(
    org_id: UUID,
    data: MotionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MotionResponse:
    """Create a new motion."""
    membership = await get_membership(db, current_user["id"], org_id)

    # Check if member can create this type
    if data.motion_type_id:
        result = await db.execute(
            select(MotionType).where(MotionType.id == data.motion_type_id)
        )
        motion_type = result.scalar_one_or_none()

        if motion_type and motion_type.allowed_creator_roles:
            if membership.role.value not in motion_type.allowed_creator_roles:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not allowed to create this type of motion",
                )

    motion = Motion(
        organization_id=org_id,
        created_by_id=membership.id,
        **data.model_dump(exclude={"organization_id"}),
    )
    db.add(motion)
    await db.commit()
    await db.refresh(motion)
    return motion


@router.get("/org/{org_id}/motions/{motion_id}", response_model=MotionDetail)
async def get_motion(
    org_id: UUID,
    motion_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MotionDetail:
    """Get details of a motion."""
    return await get_motion_or_404(db, motion_id, org_id)


@router.patch("/org/{org_id}/motions/{motion_id}", response_model=MotionResponse)
async def update_motion(
    org_id: UUID,
    motion_id: UUID,
    data: MotionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MotionResponse:
    """Update a motion."""
    motion = await get_motion_or_404(db, motion_id, org_id)

    if motion.status != MotionStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only edit motions in draft status",
        )

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(motion, key, value)

    await db.commit()
    await db.refresh(motion)
    return motion


@router.delete("/org/{org_id}/motions/{motion_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_motion(
    org_id: UUID,
    motion_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> None:
    """Delete a motion (only drafts)."""
    motion = await get_motion_or_404(db, motion_id, org_id)

    if motion.status != MotionStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only delete motions in draft status",
        )

    await db.delete(motion)
    await db.commit()


# =============================================================================
# Motion Workflow
# =============================================================================


@router.get(
    "/org/{org_id}/motions/{motion_id}/workflow",
    response_model=WorkflowStatusResponse,
)
async def get_workflow_status(
    org_id: UUID,
    motion_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> WorkflowStatusResponse:
    """Get the workflow status of a motion."""
    motion = await get_motion_or_404(db, motion_id, org_id)
    service = DocumentWorkflowService(db, motion)
    return await service.get_workflow_status()


@router.post("/org/{org_id}/motions/{motion_id}/workflow/submit-for-approval")
async def submit_for_approval(
    org_id: UUID,
    motion_id: UUID,
    data: SubmitForApprovalRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Submit a motion for internal approval."""
    membership = await get_membership(db, current_user["id"], org_id)
    motion = await get_motion_or_404(db, motion_id, org_id)

    service = DocumentWorkflowService(db, motion)

    try:
        await service.submit_for_approval(membership)
        await db.commit()
        return {"success": True, "status": motion.status.value}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/org/{org_id}/motions/{motion_id}/workflow/submit-to-coalition")
async def submit_to_coalition(
    org_id: UUID,
    motion_id: UUID,
    data: SubmitToCoalitionRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Submit a motion to coalition partners."""
    membership = await get_membership(db, current_user["id"], org_id)
    motion = await get_motion_or_404(db, motion_id, org_id)

    service = DocumentWorkflowService(db, motion)

    try:
        consultations = await service.submit_to_coalition(membership)
        await db.commit()
        return {
            "success": True,
            "status": motion.status.value,
            "parties_notified": len(consultations),
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/org/{org_id}/motions/{motion_id}/workflow/submit")
async def submit_motion(
    org_id: UUID,
    motion_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Submit a motion (final step)."""
    membership = await get_membership(db, current_user["id"], org_id)
    motion = await get_motion_or_404(db, motion_id, org_id)

    service = DocumentWorkflowService(db, motion)

    try:
        await service.submit_motion(membership)
        await db.commit()
        return {"success": True, "status": motion.status.value}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/org/{org_id}/motions/{motion_id}/workflow/withdraw")
async def withdraw_motion(
    org_id: UUID,
    motion_id: UUID,
    reason: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Withdraw a motion."""
    membership = await get_membership(db, current_user["id"], org_id)
    motion = await get_motion_or_404(db, motion_id, org_id)

    service = DocumentWorkflowService(db, motion)

    try:
        await service.withdraw_motion(membership, reason)
        await db.commit()
        return {"success": True, "status": motion.status.value}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


# =============================================================================
# Motion Approvals
# =============================================================================


@router.get(
    "/org/{org_id}/motions/{motion_id}/approvals",
    response_model=list[MotionApprovalResponse],
)
async def list_approvals(
    org_id: UUID,
    motion_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[MotionApprovalResponse]:
    """List approval requests for a motion."""
    result = await db.execute(
        select(MotionApproval).where(MotionApproval.motion_id == motion_id)
    )
    return list(result.scalars().all())


@router.patch(
    "/org/{org_id}/motions/{motion_id}/approvals/{approval_id}",
    response_model=MotionApprovalResponse,
)
async def respond_to_approval(
    org_id: UUID,
    motion_id: UUID,
    approval_id: UUID,
    data: MotionApprovalUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MotionApprovalResponse:
    """Respond to an approval request."""
    membership = await get_membership(db, current_user["id"], org_id)
    motion = await get_motion_or_404(db, motion_id, org_id)

    if not membership.can_approve_motions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to approve motions",
        )

    service = DocumentWorkflowService(db, motion)

    try:
        approval = await service.process_approval(
            approval_id=approval_id,
            approved_by=membership,
            status=data.status,
            comment=data.comment,
        )
        await db.commit()
        return approval
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


# =============================================================================
# Co-Authors
# =============================================================================


@router.get(
    "/org/{org_id}/motions/{motion_id}/co-authors",
    response_model=list[CoAuthorInviteResponse],
)
async def list_co_author_invites(
    org_id: UUID,
    motion_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[CoAuthorInviteResponse]:
    """List co-author invitations for a motion."""
    result = await db.execute(
        select(MotionCoAuthorInvite).where(MotionCoAuthorInvite.motion_id == motion_id)
    )
    return list(result.scalars().all())


@router.post(
    "/org/{org_id}/motions/{motion_id}/co-authors",
    response_model=CoAuthorInviteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def invite_co_author(
    org_id: UUID,
    motion_id: UUID,
    data: CoAuthorInviteCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> CoAuthorInviteResponse:
    """Invite a member as co-author."""
    membership = await get_membership(db, current_user["id"], org_id)
    motion = await get_motion_or_404(db, motion_id, org_id)

    service = CoAuthorService(db)

    try:
        invite = await service.invite_co_author(
            motion=motion,
            invited_by=membership,
            invited_member_id=data.invited_member_id,
            message=data.message,
        )
        await db.commit()
        return invite
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/org/{org_id}/my-co-author-invites", response_model=list[CoAuthorInviteResponse])
async def list_my_invites(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[CoAuthorInviteResponse]:
    """List pending co-author invitations for current user."""
    membership = await get_membership(db, current_user["id"], org_id)
    service = CoAuthorService(db)
    return await service.get_pending_invitations(membership)


@router.patch(
    "/org/{org_id}/co-author-invites/{invite_id}",
    response_model=CoAuthorInviteResponse,
)
async def respond_to_co_author_invite(
    org_id: UUID,
    invite_id: UUID,
    data: CoAuthorInviteUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> CoAuthorInviteResponse:
    """Respond to a co-author invitation."""
    membership = await get_membership(db, current_user["id"], org_id)
    service = CoAuthorService(db)

    try:
        accept = data.status == "accepted"
        invite = await service.respond_to_invite(invite_id, membership, accept)
        await db.commit()
        return invite
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


# =============================================================================
# Coalition Consultation
# =============================================================================


@router.get(
    "/org/{org_id}/motions/{motion_id}/coalition",
    response_model=list[CoalitionConsultationWithParty],
)
async def list_coalition_consultations(
    org_id: UUID,
    motion_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[CoalitionConsultationWithParty]:
    """List coalition consultations for a motion."""
    result = await db.execute(
        select(CoalitionConsultation)
        .where(CoalitionConsultation.motion_id == motion_id)
        .options(selectinload(CoalitionConsultation.party))
    )
    return list(result.scalars().all())


@router.patch(
    "/org/{org_id}/motions/{motion_id}/coalition/{party_id}",
    response_model=CoalitionConsultationResponse,
)
async def update_coalition_response(
    org_id: UUID,
    motion_id: UUID,
    party_id: UUID,
    data: CoalitionConsultationUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> CoalitionConsultationResponse:
    """Record a coalition partner's response."""
    motion = await get_motion_or_404(db, motion_id, org_id)
    service = DocumentWorkflowService(db, motion)

    try:
        consultation = await service.record_coalition_response(
            party_id=party_id,
            result=data.result,
            response_note=data.response_note,
        )
        await db.commit()
        return consultation
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


# =============================================================================
# Sharing
# =============================================================================


@router.post("/org/{org_id}/motions/{motion_id}/share", response_model=MotionShareLogResponse)
async def share_motion(
    org_id: UUID,
    motion_id: UUID,
    data: MotionShareRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MotionShareLogResponse:
    """Share a motion with a party or email."""
    membership = await get_membership(db, current_user["id"], org_id)
    motion = await get_motion_or_404(db, motion_id, org_id)

    if not data.party_id and not data.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must specify party_id or email",
        )

    service = MotionShareService(db)

    if data.party_id:
        result = await db.execute(
            select(CouncilParty).where(CouncilParty.id == data.party_id)
        )
        party = result.scalar_one_or_none()

        if not party:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Party not found",
            )

        log = await service.share_with_party(
            motion=motion,
            shared_by=membership,
            party=party,
            method=data.method,
            custom_message=data.custom_message,
        )
    else:
        # Share with email directly
        log = MotionShareLog(
            motion_id=motion.id,
            shared_by_id=membership.id,
            shared_with_email=data.email,
            method=data.method,
            success=True,
            note=data.custom_message,
        )
        db.add(log)

    await db.commit()
    await db.refresh(log)
    return log


@router.post("/org/{org_id}/motions/{motion_id}/share-coalition")
async def share_with_coalition(
    org_id: UUID,
    motion_id: UUID,
    data: CoalitionSendRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Share a motion with all coalition partners."""
    membership = await get_membership(db, current_user["id"], org_id)
    motion = await get_motion_or_404(db, motion_id, org_id)

    service = MotionShareService(db)
    custom_message = data.custom_message if data else None

    logs = await service.share_with_coalition(
        motion=motion,
        shared_by=membership,
        custom_message=custom_message,
    )

    await db.commit()

    return {
        "success": True,
        "sent": sum(1 for log in logs if log.success),
        "failed": sum(1 for log in logs if not log.success),
    }


@router.get(
    "/org/{org_id}/motions/{motion_id}/share-history",
    response_model=list[MotionShareLogResponse],
)
async def get_share_history(
    org_id: UUID,
    motion_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[MotionShareLogResponse]:
    """Get sharing history for a motion."""
    motion = await get_motion_or_404(db, motion_id, org_id)
    service = MotionShareService(db)
    return await service.get_share_history(motion)


# =============================================================================
# Council Parties
# =============================================================================


@router.get("/org/{org_id}/parties", response_model=list[CouncilPartyResponse])
async def list_parties(
    org_id: UUID,
    coalition_only: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[CouncilPartyResponse]:
    """List council parties."""
    query = (
        select(CouncilParty)
        .where(CouncilParty.organization_id == org_id)
        .where(CouncilParty.is_active == True)  # noqa: E712
    )

    if coalition_only:
        query = query.where(CouncilParty.is_coalition_member == True)  # noqa: E712

    query = query.order_by(CouncilParty.coalition_order, CouncilParty.name)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.post(
    "/org/{org_id}/parties",
    response_model=CouncilPartyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_party(
    org_id: UUID,
    data: CouncilPartyCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> CouncilPartyResponse:
    """Create a council party."""
    party = CouncilParty(
        organization_id=org_id,
        **data.model_dump(exclude={"organization_id"}),
    )
    db.add(party)
    await db.commit()
    await db.refresh(party)
    return party


@router.patch("/org/{org_id}/parties/{party_id}", response_model=CouncilPartyResponse)
async def update_party(
    org_id: UUID,
    party_id: UUID,
    data: CouncilPartyUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> CouncilPartyResponse:
    """Update a council party."""
    result = await db.execute(
        select(CouncilParty)
        .where(CouncilParty.id == party_id)
        .where(CouncilParty.organization_id == org_id)
    )
    party = result.scalar_one_or_none()

    if not party:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Party not found",
        )

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(party, key, value)

    await db.commit()
    await db.refresh(party)
    return party


# =============================================================================
# Working Groups
# =============================================================================


@router.get("/org/{org_id}/workgroups", response_model=list[WorkGroupResponse])
async def list_workgroups(
    org_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[WorkGroupResponse]:
    """List working groups."""
    result = await db.execute(
        select(WorkGroup)
        .where(WorkGroup.organization_id == org_id)
        .where(WorkGroup.is_active == True)  # noqa: E712
    )
    return list(result.scalars().all())


@router.post(
    "/org/{org_id}/workgroups",
    response_model=WorkGroupResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workgroup(
    org_id: UUID,
    data: WorkGroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> WorkGroupResponse:
    """Create a working group."""
    workgroup = WorkGroup(
        organization_id=org_id,
        **data.model_dump(exclude={"organization_id"}),
    )
    db.add(workgroup)
    await db.commit()
    await db.refresh(workgroup)
    return workgroup


@router.get("/org/{org_id}/workgroups/{wg_id}", response_model=WorkGroupDetail)
async def get_workgroup(
    org_id: UUID,
    wg_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> WorkGroupDetail:
    """Get working group details."""
    result = await db.execute(
        select(WorkGroup)
        .where(WorkGroup.id == wg_id)
        .where(WorkGroup.organization_id == org_id)
        .options(
            selectinload(WorkGroup.speaker),
            selectinload(WorkGroup.members),
        )
    )
    workgroup = result.scalar_one_or_none()

    if not workgroup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Working group not found",
        )

    return workgroup


@router.patch("/org/{org_id}/workgroups/{wg_id}", response_model=WorkGroupResponse)
async def update_workgroup(
    org_id: UUID,
    wg_id: UUID,
    data: WorkGroupUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> WorkGroupResponse:
    """Update a working group."""
    result = await db.execute(
        select(WorkGroup)
        .where(WorkGroup.id == wg_id)
        .where(WorkGroup.organization_id == org_id)
    )
    workgroup = result.scalar_one_or_none()

    if not workgroup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Working group not found",
        )

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(workgroup, key, value)

    await db.commit()
    await db.refresh(workgroup)
    return workgroup


@router.post(
    "/org/{org_id}/workgroups/{wg_id}/members",
    response_model=WorkGroupMembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_workgroup_member(
    org_id: UUID,
    wg_id: UUID,
    data: WorkGroupMembershipCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> WorkGroupMembershipResponse:
    """Add a member to a working group."""
    membership = WorkGroupMembership(
        workgroup_id=wg_id,
        member_id=data.member_id,
        role=data.role,
    )
    db.add(membership)
    await db.commit()
    await db.refresh(membership)
    return membership


@router.post(
    "/org/{org_id}/motions/{motion_id}/workgroup-recommendation",
    response_model=MotionResponse,
)
async def submit_workgroup_recommendation(
    org_id: UUID,
    motion_id: UUID,
    data: WorkGroupRecommendation,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> MotionResponse:
    """Submit a working group recommendation for a motion."""
    motion = await get_motion_or_404(db, motion_id, org_id)

    if not motion.assigned_workgroup_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Motion is not assigned to a working group",
        )

    # TODO: Check if user is speaker of the workgroup

    motion.workgroup_recommendation = data.recommendation
    motion.workgroup_recommendation_note = data.note

    await db.commit()
    await db.refresh(motion)
    return motion


@router.delete("/org/{org_id}/workgroups/{workgroup_id}/members/{membership_id}")
async def remove_workgroup_member(
    org_id: UUID,
    workgroup_id: UUID,
    membership_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a member from a working group."""
    result = await db.execute(
        select(WorkGroupMembership)
        .where(WorkGroupMembership.workgroup_id == workgroup_id)
        .where(WorkGroupMembership.member_id == membership_id)
    )
    wg_membership = result.scalar_one_or_none()

    if not wg_membership:
        raise HTTPException(status_code=404, detail="Membership not found")

    await db.delete(wg_membership)
    await db.commit()

    return {"status": "success"}


# =============================================================================
# Faction Meeting Endpoints
# =============================================================================


@router.get("/org/{org_id}/meetings", response_model=list[FactionMeetingResponse])
async def list_meetings(
    org_id: UUID,
    status_filter: MeetingStatus | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[FactionMeeting]:
    """List faction meetings for an organization."""
    query = (
        select(FactionMeeting)
        .where(FactionMeeting.organization_id == org_id)
        .order_by(FactionMeeting.scheduled_date.desc())
        .offset(offset)
        .limit(limit)
    )
    if status_filter:
        query = query.where(FactionMeeting.status == status_filter)

    result = await db.execute(query)
    return list(result.scalars().all())


@router.post("/org/{org_id}/meetings", response_model=FactionMeetingResponse, status_code=201)
async def create_meeting(
    org_id: UUID,
    data: FactionMeetingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionMeeting:
    """Create a new faction meeting."""
    membership = await get_membership(db, current_user["id"], org_id)

    setup_service = MeetingSetupService(db)
    meeting = await setup_service.create_meeting(
        organization_id=org_id,
        created_by=membership,
        title=data.title,
        scheduled_date=data.scheduled_date,
        scheduled_end=data.scheduled_end,
        location=data.location,
        conference_link=data.conference_link,
        description=data.description,
        previous_meeting_id=data.previous_meeting_id,
    )
    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.get("/org/{org_id}/meetings/{meeting_id}", response_model=FactionMeetingDetail)
async def get_meeting(
    org_id: UUID,
    meeting_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> FactionMeeting:
    """Get a faction meeting by ID."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
        .options(
            selectinload(FactionMeeting.created_by).selectinload(Membership.user),
            selectinload(FactionMeeting.previous_meeting),
        )
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.patch("/org/{org_id}/meetings/{meeting_id}", response_model=FactionMeetingResponse)
async def update_meeting(
    org_id: UUID,
    meeting_id: UUID,
    data: FactionMeetingUpdate,
    db: AsyncSession = Depends(get_db),
) -> FactionMeeting:
    """Update a faction meeting."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(meeting, field, value)

    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/org/{org_id}/meetings/{meeting_id}/schedule", response_model=FactionMeetingResponse)
async def schedule_meeting(
    org_id: UUID,
    meeting_id: UUID,
    data: MeetingScheduleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionMeeting:
    """Schedule a meeting (send invitations)."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    membership = await get_membership(db, current_user["id"], org_id)

    lifecycle = MeetingLifecycleService(db, meeting)
    meeting = await lifecycle.schedule_meeting(membership)

    if data.send_invitations:
        invitation_service = MeetingInvitationService(db)
        await invitation_service.invite_all_members(meeting, membership)
        await invitation_service.send_invitations(meeting)

    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/org/{org_id}/meetings/{meeting_id}/start", response_model=FactionMeetingResponse)
async def start_meeting(
    org_id: UUID,
    meeting_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionMeeting:
    """Start a meeting."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    membership = await get_membership(db, current_user["id"], org_id)

    lifecycle = MeetingLifecycleService(db, meeting)
    meeting = await lifecycle.start_meeting(membership)
    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/org/{org_id}/meetings/{meeting_id}/end", response_model=FactionMeetingResponse)
async def end_meeting(
    org_id: UUID,
    meeting_id: UUID,
    data: MeetingEndRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionMeeting:
    """End a meeting."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    membership = await get_membership(db, current_user["id"], org_id)

    lifecycle = MeetingLifecycleService(db, meeting)
    meeting = await lifecycle.end_meeting(
        membership,
        lock_protocol=data.lock_protocol,
        lock_attendance=data.lock_attendance,
    )
    await db.commit()
    await db.refresh(meeting)
    return meeting


@router.post("/org/{org_id}/meetings/{meeting_id}/cancel", response_model=FactionMeetingResponse)
async def cancel_meeting(
    org_id: UUID,
    meeting_id: UUID,
    data: MeetingCancelRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionMeeting:
    """Cancel a meeting."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    membership = await get_membership(db, current_user["id"], org_id)

    lifecycle = MeetingLifecycleService(db, meeting)
    meeting = await lifecycle.cancel_meeting(membership, data.reason)
    await db.commit()
    await db.refresh(meeting)
    return meeting


# =============================================================================
# Agenda Item Endpoints
# =============================================================================


@router.get(
    "/org/{org_id}/meetings/{meeting_id}/agenda",
    response_model=list[FactionAgendaItemResponse],
)
async def list_agenda_items(
    org_id: UUID,
    meeting_id: UUID,
    include_pending: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[FactionAgendaItem]:
    """List agenda items for a meeting."""
    query = (
        select(FactionAgendaItem)
        .where(FactionAgendaItem.meeting_id == meeting_id)
        .where(FactionAgendaItem.parent_id == None)  # noqa: E711
        .order_by(FactionAgendaItem.sort_order)
    )
    if not include_pending:
        query = query.where(
            FactionAgendaItem.suggestion_status == AgendaSuggestionStatus.APPROVED
        )

    result = await db.execute(query)
    return list(result.scalars().all())


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/agenda",
    response_model=FactionAgendaItemResponse,
    status_code=201,
)
async def create_agenda_item(
    org_id: UUID,
    meeting_id: UUID,
    data: FactionAgendaItemCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionAgendaItem:
    """Create or suggest an agenda item."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    membership = await get_membership(db, current_user["id"], org_id)

    suggestion_service = AgendaSuggestionService(db)
    item = await suggestion_service.suggest_agenda_item(
        meeting=meeting,
        suggested_by=membership,
        title=data.title,
        description=data.description,
        visibility=data.visibility,
        parent_id=data.parent_id,
        related_motion_id=data.related_motion_id,
        estimated_duration_minutes=data.estimated_duration_minutes,
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.get(
    "/org/{org_id}/meetings/{meeting_id}/agenda/{item_id}",
    response_model=FactionAgendaItemDetail,
)
async def get_agenda_item(
    org_id: UUID,
    meeting_id: UUID,
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> FactionAgendaItem:
    """Get an agenda item with details."""
    result = await db.execute(
        select(FactionAgendaItem)
        .where(FactionAgendaItem.id == item_id)
        .where(FactionAgendaItem.meeting_id == meeting_id)
        .options(
            selectinload(FactionAgendaItem.sub_items),
            selectinload(FactionAgendaItem.decisions),
            selectinload(FactionAgendaItem.protocol_entries),
            selectinload(FactionAgendaItem.related_motion),
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")
    return item


@router.patch(
    "/org/{org_id}/meetings/{meeting_id}/agenda/{item_id}",
    response_model=FactionAgendaItemResponse,
)
async def update_agenda_item(
    org_id: UUID,
    meeting_id: UUID,
    item_id: UUID,
    data: FactionAgendaItemUpdate,
    db: AsyncSession = Depends(get_db),
) -> FactionAgendaItem:
    """Update an agenda item."""
    result = await db.execute(
        select(FactionAgendaItem)
        .where(FactionAgendaItem.id == item_id)
        .where(FactionAgendaItem.meeting_id == meeting_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(item, field, value)

    await db.commit()
    await db.refresh(item)
    return item


@router.delete("/org/{org_id}/meetings/{meeting_id}/agenda/{item_id}")
async def delete_agenda_item(
    org_id: UUID,
    meeting_id: UUID,
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete an agenda item."""
    result = await db.execute(
        select(FactionAgendaItem)
        .where(FactionAgendaItem.id == item_id)
        .where(FactionAgendaItem.meeting_id == meeting_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")

    await db.delete(item)
    await db.commit()
    return {"status": "success"}


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/agenda/{item_id}/approve",
    response_model=FactionAgendaItemResponse,
)
async def approve_agenda_suggestion(
    org_id: UUID,
    meeting_id: UUID,
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionAgendaItem:
    """Approve a pending agenda item suggestion."""
    result = await db.execute(
        select(FactionAgendaItem)
        .where(FactionAgendaItem.id == item_id)
        .where(FactionAgendaItem.meeting_id == meeting_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")

    membership = await get_membership(db, current_user["id"], org_id)

    suggestion_service = AgendaSuggestionService(db)
    try:
        item = await suggestion_service.approve_suggestion(item, membership)
        await db.commit()
        await db.refresh(item)
        return item
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/agenda/{item_id}/reject",
    response_model=FactionAgendaItemResponse,
)
async def reject_agenda_suggestion(
    org_id: UUID,
    meeting_id: UUID,
    item_id: UUID,
    reason: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionAgendaItem:
    """Reject a pending agenda item suggestion."""
    result = await db.execute(
        select(FactionAgendaItem)
        .where(FactionAgendaItem.id == item_id)
        .where(FactionAgendaItem.meeting_id == meeting_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")

    membership = await get_membership(db, current_user["id"], org_id)

    suggestion_service = AgendaSuggestionService(db)
    try:
        item = await suggestion_service.reject_suggestion(item, membership, reason)
        await db.commit()
        await db.refresh(item)
        return item
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/agenda/add-motion",
    response_model=FactionAgendaItemResponse,
)
async def add_motion_to_agenda(
    org_id: UUID,
    meeting_id: UUID,
    data: AddMotionToAgendaRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionAgendaItem:
    """Add a motion to the meeting agenda."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    result = await db.execute(
        select(Motion)
        .where(Motion.id == data.motion_id)
        .where(Motion.organization_id == org_id)
    )
    motion = result.scalar_one_or_none()
    if not motion:
        raise HTTPException(status_code=404, detail="Motion not found")

    membership = await get_membership(db, current_user["id"], org_id)

    motion_service = MotionAgendaService(db)
    item = await motion_service.add_motion_to_agenda(
        meeting, motion, membership, data.parent_id
    )
    await db.commit()
    await db.refresh(item)
    return item


@router.post("/org/{org_id}/meetings/{meeting_id}/agenda/reorder")
async def reorder_agenda_items(
    org_id: UUID,
    meeting_id: UUID,
    data: AgendaReorderRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Reorder agenda items."""
    for item_order in data.item_orders:
        result = await db.execute(
            select(FactionAgendaItem)
            .where(FactionAgendaItem.id == item_order["id"])
            .where(FactionAgendaItem.meeting_id == meeting_id)
        )
        item = result.scalar_one_or_none()
        if item:
            item.sort_order = item_order["sort_order"]

    await db.commit()
    return {"status": "success"}


@router.get(
    "/org/{org_id}/meetings/{meeting_id}/suggestions",
    response_model=list[FactionAgendaItemResponse],
)
async def list_pending_suggestions(
    org_id: UUID,
    meeting_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[FactionAgendaItem]:
    """List pending agenda item suggestions."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    suggestion_service = AgendaSuggestionService(db)
    return await suggestion_service.get_pending_suggestions(meeting)


# =============================================================================
# Protocol Endpoints
# =============================================================================


@router.get(
    "/org/{org_id}/meetings/{meeting_id}/protocol",
    response_model=list[FactionAgendaItemDetail],
)
async def get_protocol(
    org_id: UUID,
    meeting_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[FactionAgendaItem]:
    """Get the full protocol for a meeting."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    protocol_service = ProtocolService(db)
    return await protocol_service.get_protocol_for_meeting(meeting)


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/agenda/{item_id}/entries",
    response_model=FactionProtocolEntryResponse,
    status_code=201,
)
async def create_protocol_entry(
    org_id: UUID,
    meeting_id: UUID,
    item_id: UUID,
    data: FactionProtocolEntryCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionProtocolEntry:
    """Create a protocol entry for an agenda item."""
    result = await db.execute(
        select(FactionAgendaItem)
        .where(FactionAgendaItem.id == item_id)
        .where(FactionAgendaItem.meeting_id == meeting_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")

    membership = await get_membership(db, current_user["id"], org_id)

    protocol_service = ProtocolService(db)
    entry = await protocol_service.add_entry(
        agenda_item=item,
        created_by=membership,
        entry_type=data.entry_type,
        content=data.content,
        assigned_to_id=data.assigned_to_id,
        due_date=data.due_date,
        visibility_override=data.visibility_override,
    )
    await db.commit()
    await db.refresh(entry)
    return entry


@router.patch(
    "/org/{org_id}/meetings/{meeting_id}/protocol/{entry_id}",
    response_model=FactionProtocolEntryResponse,
)
async def update_protocol_entry(
    org_id: UUID,
    meeting_id: UUID,
    entry_id: UUID,
    data: FactionProtocolEntryUpdate,
    db: AsyncSession = Depends(get_db),
) -> FactionProtocolEntry:
    """Update a protocol entry."""
    result = await db.execute(
        select(FactionProtocolEntry)
        .where(FactionProtocolEntry.id == entry_id)
        .options(selectinload(FactionProtocolEntry.agenda_item))
    )
    entry = result.scalar_one_or_none()
    if not entry or entry.agenda_item.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="Protocol entry not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(entry, field, value)

    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/org/{org_id}/meetings/{meeting_id}/protocol/{entry_id}")
async def delete_protocol_entry(
    org_id: UUID,
    meeting_id: UUID,
    entry_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a protocol entry."""
    result = await db.execute(
        select(FactionProtocolEntry)
        .where(FactionProtocolEntry.id == entry_id)
        .options(selectinload(FactionProtocolEntry.agenda_item))
    )
    entry = result.scalar_one_or_none()
    if not entry or entry.agenda_item.meeting_id != meeting_id:
        raise HTTPException(status_code=404, detail="Protocol entry not found")

    await db.delete(entry)
    await db.commit()
    return {"status": "success"}


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/agenda/{item_id}/decision",
    response_model=FactionDecisionResponse,
    status_code=201,
)
async def create_decision(
    org_id: UUID,
    meeting_id: UUID,
    item_id: UUID,
    data: FactionDecisionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionDecision:
    """Record a decision for an agenda item."""
    result = await db.execute(
        select(FactionAgendaItem)
        .where(FactionAgendaItem.id == item_id)
        .where(FactionAgendaItem.meeting_id == meeting_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Agenda item not found")

    membership = await get_membership(db, current_user["id"], org_id)

    # If item has a related motion, use MotionAgendaService
    if item.related_motion_id:
        motion_service = MotionAgendaService(db)
        decision = await motion_service.record_decision_for_motion(
            agenda_item=item,
            decision_type=data.decision_type,
            decision_text=data.decision_text,
            recorded_by=membership,
            votes_for=data.votes_for,
            votes_against=data.votes_against,
            votes_abstain=data.votes_abstain,
        )
    else:
        protocol_service = ProtocolService(db)
        decision = await protocol_service.add_decision(
            agenda_item=item,
            created_by=membership,
            decision_type=data.decision_type,
            decision_text=data.decision_text,
            votes_for=data.votes_for,
            votes_against=data.votes_against,
            votes_abstain=data.votes_abstain,
        )

    await db.commit()
    await db.refresh(decision)
    return decision


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/protocol/lock",
    response_model=FactionProtocolRevisionResponse,
)
async def lock_protocol(
    org_id: UUID,
    meeting_id: UUID,
    data: ProtocolLockRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionProtocolRevision:
    """Lock the protocol and create a revision."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    if meeting.protocol_locked:
        raise HTTPException(status_code=400, detail="Protocol is already locked")

    membership = await get_membership(db, current_user["id"], org_id)

    lifecycle = MeetingLifecycleService(db, meeting)
    revision = await lifecycle._lock_protocol(membership)
    await db.commit()
    await db.refresh(revision)
    return revision


@router.get(
    "/org/{org_id}/meetings/{meeting_id}/protocol/revisions",
    response_model=list[FactionProtocolRevisionResponse],
)
async def list_protocol_revisions(
    org_id: UUID,
    meeting_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[FactionProtocolRevision]:
    """List all protocol revisions for a meeting."""
    result = await db.execute(
        select(FactionProtocolRevision)
        .where(FactionProtocolRevision.meeting_id == meeting_id)
        .order_by(FactionProtocolRevision.revision_number.desc())
    )
    return list(result.scalars().all())


# =============================================================================
# Attendance Endpoints
# =============================================================================


@router.get(
    "/org/{org_id}/meetings/{meeting_id}/attendance",
    response_model=list[FactionAttendanceResponse],
)
async def list_attendance(
    org_id: UUID,
    meeting_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[FactionAttendance]:
    """List attendance for a meeting."""
    result = await db.execute(
        select(FactionAttendance)
        .where(FactionAttendance.meeting_id == meeting_id)
        .options(selectinload(FactionAttendance.membership).selectinload(Membership.user))
        .order_by(FactionAttendance.is_guest, FactionAttendance.recorded_at)
    )
    return list(result.scalars().all())


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/attendance",
    response_model=FactionAttendanceResponse,
    status_code=201,
)
async def create_attendance(
    org_id: UUID,
    meeting_id: UUID,
    data: FactionAttendanceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionAttendance:
    """Record attendance for a member or guest."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    membership = await get_membership(db, current_user["id"], org_id)

    attendance_service = AttendanceService(db)
    attendance = await attendance_service.record_attendance(
        meeting=meeting,
        recorded_by=membership,
        membership_id=data.membership_id,
        guest_name=data.guest_name,
        attendance_type=data.attendance_type,
        status=data.status,
        note=data.note,
    )
    await db.commit()
    await db.refresh(attendance)
    return attendance


@router.patch(
    "/org/{org_id}/meetings/{meeting_id}/attendance/{attendance_id}",
    response_model=FactionAttendanceResponse,
)
async def update_attendance(
    org_id: UUID,
    meeting_id: UUID,
    attendance_id: UUID,
    data: FactionAttendanceUpdate,
    db: AsyncSession = Depends(get_db),
) -> FactionAttendance:
    """Update attendance record."""
    result = await db.execute(
        select(FactionAttendance)
        .where(FactionAttendance.id == attendance_id)
        .where(FactionAttendance.meeting_id == meeting_id)
    )
    attendance = result.scalar_one_or_none()
    if not attendance:
        raise HTTPException(status_code=404, detail="Attendance not found")

    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(attendance, field, value)

    await db.commit()
    await db.refresh(attendance)
    return attendance


@router.delete("/org/{org_id}/meetings/{meeting_id}/attendance/{attendance_id}")
async def delete_attendance(
    org_id: UUID,
    meeting_id: UUID,
    attendance_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete an attendance record."""
    result = await db.execute(
        select(FactionAttendance)
        .where(FactionAttendance.id == attendance_id)
        .where(FactionAttendance.meeting_id == meeting_id)
    )
    attendance = result.scalar_one_or_none()
    if not attendance:
        raise HTTPException(status_code=404, detail="Attendance not found")

    await db.delete(attendance)
    await db.commit()
    return {"status": "success"}


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/check-in",
    response_model=FactionAttendanceResponse,
)
async def check_in(
    org_id: UUID,
    meeting_id: UUID,
    data: CheckInRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionAttendance:
    """Check in to a meeting."""
    membership = await get_membership(db, current_user["id"], org_id)

    # Find or create attendance record
    result = await db.execute(
        select(FactionAttendance)
        .where(FactionAttendance.meeting_id == meeting_id)
        .where(FactionAttendance.membership_id == membership.id)
    )
    attendance = result.scalar_one_or_none()

    if not attendance:
        # Create new attendance
        result = await db.execute(
            select(FactionMeeting)
            .where(FactionMeeting.id == meeting_id)
            .where(FactionMeeting.organization_id == org_id)
        )
        meeting = result.scalar_one_or_none()
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")

        attendance_service = AttendanceService(db)
        attendance = await attendance_service.record_attendance(
            meeting=meeting,
            recorded_by=membership,
            membership_id=membership.id,
        )

    attendance_service = AttendanceService(db)
    attendance = await attendance_service.check_in(
        attendance, data.ip_address, data.user_agent
    )
    await db.commit()
    await db.refresh(attendance)
    return attendance


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/check-out",
    response_model=FactionAttendanceResponse,
)
async def check_out(
    org_id: UUID,
    meeting_id: UUID,
    data: CheckOutRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> FactionAttendance:
    """Check out from a meeting."""
    membership = await get_membership(db, current_user["id"], org_id)

    result = await db.execute(
        select(FactionAttendance)
        .where(FactionAttendance.meeting_id == meeting_id)
        .where(FactionAttendance.membership_id == membership.id)
    )
    attendance = result.scalar_one_or_none()
    if not attendance:
        raise HTTPException(status_code=404, detail="Attendance not found")

    attendance_service = AttendanceService(db)
    attendance = await attendance_service.check_out(
        attendance, data.ip_address, data.user_agent
    )
    await db.commit()
    await db.refresh(attendance)
    return attendance


# =============================================================================
# Invitation Endpoints
# =============================================================================


@router.get(
    "/org/{org_id}/meetings/{meeting_id}/invitations",
    response_model=list[FactionMeetingInvitationResponse],
)
async def list_invitations(
    org_id: UUID,
    meeting_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> list[FactionMeetingInvitation]:
    """List invitations for a meeting."""
    result = await db.execute(
        select(FactionMeetingInvitation)
        .where(FactionMeetingInvitation.meeting_id == meeting_id)
        .order_by(FactionMeetingInvitation.created_at)
    )
    return list(result.scalars().all())


@router.post(
    "/org/{org_id}/meetings/{meeting_id}/invitations/invite-all",
    response_model=list[FactionMeetingInvitationResponse],
)
async def invite_all_members(
    org_id: UUID,
    meeting_id: UUID,
    data: InviteAllRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[FactionMeetingInvitation]:
    """Invite all organization members to a meeting."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    membership = await get_membership(db, current_user["id"], org_id)

    invitation_service = MeetingInvitationService(db)
    invitations = await invitation_service.invite_all_members(meeting, membership)

    if data.send_email:
        await invitation_service.send_invitations(meeting)

    await db.commit()
    return invitations


@router.post("/org/{org_id}/meetings/{meeting_id}/invitations/send")
async def send_invitations(
    org_id: UUID,
    meeting_id: UUID,
    data: SendInvitationsRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send invitation emails."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.id == meeting_id)
        .where(FactionMeeting.organization_id == org_id)
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    invitation_service = MeetingInvitationService(db)
    sent_count = await invitation_service.send_invitations(
        meeting, data.invitation_ids
    )
    await db.commit()

    return {"status": "success", "sent_count": sent_count}


@router.patch(
    "/org/{org_id}/meetings/{meeting_id}/invitations/{invitation_id}/rsvp",
    response_model=FactionMeetingInvitationResponse,
)
async def respond_to_invitation(
    org_id: UUID,
    meeting_id: UUID,
    invitation_id: UUID,
    data: RSVPRequest,
    db: AsyncSession = Depends(get_db),
) -> FactionMeetingInvitation:
    """Respond to a meeting invitation (RSVP)."""
    result = await db.execute(
        select(FactionMeetingInvitation)
        .where(FactionMeetingInvitation.id == invitation_id)
        .where(FactionMeetingInvitation.meeting_id == meeting_id)
    )
    invitation = result.scalar_one_or_none()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")

    invitation_service = MeetingInvitationService(db)
    invitation = await invitation_service.update_rsvp(
        invitation, data.status, data.note
    )
    await db.commit()
    await db.refresh(invitation)
    return invitation


# =============================================================================
# Public API Endpoints
# =============================================================================


@router.get("/public/meetings/{public_id}", response_model=PublicMeetingResponse)
async def get_public_meeting(
    public_id: str,
    db: AsyncSession = Depends(get_db),
) -> FactionMeeting:
    """Get public meeting info by public ID."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.public_id == public_id)
        .where(FactionMeeting.public_protocol_enabled == True)  # noqa: E712
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting


@router.get(
    "/public/meetings/{public_id}/agenda",
    response_model=list[PublicAgendaItemResponse],
)
async def get_public_agenda(
    public_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[FactionAgendaItem]:
    """Get public agenda items for a meeting."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.public_id == public_id)
        .where(FactionMeeting.public_protocol_enabled == True)  # noqa: E712
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    result = await db.execute(
        select(FactionAgendaItem)
        .where(FactionAgendaItem.meeting_id == meeting.id)
        .where(FactionAgendaItem.visibility == "public")
        .where(FactionAgendaItem.suggestion_status == AgendaSuggestionStatus.APPROVED)
        .order_by(FactionAgendaItem.sort_order)
    )
    return list(result.scalars().all())


@router.get(
    "/public/meetings/{public_id}/decisions",
    response_model=list[PublicDecisionResponse],
)
async def get_public_decisions(
    public_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[FactionDecision]:
    """Get public decisions for a meeting."""
    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.public_id == public_id)
        .where(FactionMeeting.public_protocol_enabled == True)  # noqa: E712
    )
    meeting = result.scalar_one_or_none()
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    # Get decisions for public agenda items only
    result = await db.execute(
        select(FactionDecision)
        .join(FactionAgendaItem)
        .where(FactionAgendaItem.meeting_id == meeting.id)
        .where(FactionAgendaItem.visibility == "public")
    )
    return list(result.scalars().all())


@router.get(
    "/public/org/{org_slug}/meetings",
    response_model=list[PublicMeetingResponse],
)
async def list_public_meetings(
    org_slug: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
) -> list[FactionMeeting]:
    """List public meetings for an organization."""
    result = await db.execute(
        select(Organization).where(Organization.slug == org_slug)
    )
    org = result.scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    result = await db.execute(
        select(FactionMeeting)
        .where(FactionMeeting.organization_id == org.id)
        .where(FactionMeeting.public_protocol_enabled == True)  # noqa: E712
        .order_by(FactionMeeting.scheduled_date.desc())
        .limit(limit)
    )
    return list(result.scalars().all())

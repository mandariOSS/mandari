"""
Work Module Services

Business logic and workflow orchestration for the work module.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.work.models import (
    AgendaItemVisibility,
    AgendaSuggestionStatus,
    ApprovalStatus,
    AttendanceStatus,
    AttendanceType,
    CoalitionConsultation,
    CoalitionResult,
    CouncilParty,
    DecisionType,
    FactionAgendaItem,
    FactionAttendance,
    FactionDecision,
    FactionMeeting,
    FactionMeetingInvitation,
    FactionMeetingSettings,
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
    ProtocolEntryType,
    RSVPStatus,
    ShareMethod,
    CoAuthorInviteStatus,
)

if TYPE_CHECKING:
    pass


class DocumentWorkflowService:
    """
    Orchestrates document workflows based on motion type configuration.

    Handles the progression of motions through their lifecycle:
    - Draft creation
    - Internal approval
    - Coalition consultation
    - Faction meeting scheduling
    - Submission
    """

    def __init__(self, db: AsyncSession, motion: Motion):
        self.db = db
        self.motion = motion
        self._motion_type: MotionType | None = None

    async def get_motion_type(self) -> MotionType | None:
        """Get the motion type with caching."""
        if self._motion_type is None and self.motion.motion_type_id:
            result = await self.db.execute(
                select(MotionType).where(MotionType.id == self.motion.motion_type_id)
            )
            self._motion_type = result.scalar_one_or_none()
        return self._motion_type

    async def get_required_steps(self) -> list[str]:
        """Get the required workflow steps for this motion."""
        steps = ["draft"]
        motion_type = await self.get_motion_type()

        if motion_type is None:
            # Default workflow for untyped motions
            steps.append("approval")
            return steps

        if motion_type.requires_approval:
            steps.append("approval")

        if motion_type.requires_coalition_approval:
            steps.append("coalition")

        if motion_type.requires_faction_decision:
            steps.append("faction_meeting")

        steps.append("submitted")
        return steps

    def get_current_step(self) -> str:
        """Get the current workflow step based on motion status."""
        status_to_step = {
            MotionStatus.DRAFT: "draft",
            MotionStatus.INTERNAL_REVIEW: "approval",
            MotionStatus.EXTERNAL_REVIEW: "coalition",
            MotionStatus.ON_AGENDA: "faction_meeting",
            MotionStatus.APPROVED: "approved",
            MotionStatus.SUBMITTED: "submitted",
            MotionStatus.ACCEPTED: "completed",
            MotionStatus.REJECTED: "completed",
            MotionStatus.WITHDRAWN: "withdrawn",
        }
        return status_to_step.get(self.motion.status, self.motion.status.value)

    async def get_completed_steps(self) -> list[str]:
        """Get the list of completed workflow steps."""
        completed = []
        current = self.get_current_step()
        required = await self.get_required_steps()

        for step in required:
            if step == current:
                break
            completed.append(step)

        return completed

    async def can_proceed(self) -> tuple[bool, str | None]:
        """
        Check if the motion can proceed to the next workflow step.

        Returns:
            Tuple of (can_proceed, reason_if_blocked)
        """
        current = self.get_current_step()

        if current == "draft":
            # Can always submit draft for approval
            return True, None

        if current == "approval":
            # Check if all required approvals are granted
            pending = await self.db.execute(
                select(MotionApproval)
                .where(MotionApproval.motion_id == self.motion.id)
                .where(MotionApproval.status == ApprovalStatus.PENDING)
            )
            if pending.scalars().first():
                return False, "Ausstehende Genehmigungen vorhanden"

            rejected = await self.db.execute(
                select(MotionApproval)
                .where(MotionApproval.motion_id == self.motion.id)
                .where(MotionApproval.status == ApprovalStatus.REJECTED)
            )
            if rejected.scalars().first():
                return False, "Genehmigung wurde abgelehnt"

            return True, None

        if current == "coalition":
            # Check coalition consultation status
            if self.motion.coalition_status == "pending":
                return False, "Koalitionsabstimmung noch nicht abgeschlossen"
            if self.motion.coalition_status == "rejected":
                return False, "Koalition hat abgelehnt"
            return True, None

        if current == "faction_meeting":
            # Check if meeting has occurred
            # This would need meeting integration
            return True, None

        return True, None

    async def get_next_actions(self) -> list[str]:
        """Get available next actions for the motion."""
        actions = []
        current = self.get_current_step()
        can_proceed, _ = await self.can_proceed()

        if current == "draft":
            actions.append("submit_for_approval")

        elif current == "approval":
            actions.append("approve")
            actions.append("reject")
            actions.append("request_changes")
            if can_proceed:
                motion_type = await self.get_motion_type()
                if motion_type and motion_type.requires_coalition_approval:
                    actions.append("submit_to_coalition")
                elif motion_type and motion_type.requires_faction_decision:
                    actions.append("schedule_for_meeting")
                else:
                    actions.append("submit")

        elif current == "coalition":
            actions.append("record_response")
            if can_proceed:
                motion_type = await self.get_motion_type()
                if motion_type and motion_type.requires_faction_decision:
                    actions.append("schedule_for_meeting")
                else:
                    actions.append("submit")

        elif current == "faction_meeting":
            actions.append("record_decision")
            if can_proceed:
                actions.append("submit")

        elif current == "approved":
            actions.append("submit")

        # Always available
        if current not in ("submitted", "completed", "withdrawn"):
            actions.append("withdraw")

        return actions

    async def get_workflow_status(self) -> dict:
        """Get comprehensive workflow status."""
        can_proceed, block_reason = await self.can_proceed()

        return {
            "motion_id": self.motion.id,
            "current_step": self.get_current_step(),
            "required_steps": await self.get_required_steps(),
            "completed_steps": await self.get_completed_steps(),
            "can_proceed": can_proceed,
            "block_reason": block_reason,
            "next_actions": await self.get_next_actions(),
        }

    async def submit_for_approval(self, submitted_by: Membership) -> None:
        """Submit the motion for internal approval."""
        if self.motion.status != MotionStatus.DRAFT:
            raise ValueError("Motion must be in draft status to submit for approval")

        self.motion.status = MotionStatus.INTERNAL_REVIEW

        motion_type = await self.get_motion_type()
        approvers = motion_type.default_approvers if motion_type else ["chair"]

        for approval_type in approvers:
            approval = MotionApproval(
                motion_id=self.motion.id,
                approval_type=approval_type,
                status=ApprovalStatus.PENDING,
            )
            self.db.add(approval)

        await self.db.flush()

    async def process_approval(
        self,
        approval_id: UUID,
        approved_by: Membership,
        status: ApprovalStatus,
        comment: str | None = None,
    ) -> MotionApproval:
        """Process an approval decision."""
        result = await self.db.execute(
            select(MotionApproval).where(MotionApproval.id == approval_id)
        )
        approval = result.scalar_one_or_none()

        if not approval:
            raise ValueError("Approval not found")

        if approval.motion_id != self.motion.id:
            raise ValueError("Approval does not belong to this motion")

        approval.status = status
        approval.approved_by_id = approved_by.id
        approval.comment = comment
        approval.responded_at = datetime.now()

        # Check if all approvals are complete
        await self._check_approval_completion()

        await self.db.flush()
        return approval

    async def _check_approval_completion(self) -> None:
        """Check if all approvals are complete and update status."""
        result = await self.db.execute(
            select(MotionApproval)
            .where(MotionApproval.motion_id == self.motion.id)
        )
        approvals = result.scalars().all()

        if not approvals:
            return

        # Check for any pending or rejected
        has_pending = any(a.status == ApprovalStatus.PENDING for a in approvals)
        has_rejected = any(a.status == ApprovalStatus.REJECTED for a in approvals)

        if has_rejected:
            # Keep in internal_review but mark as blocked
            return

        if not has_pending:
            # All approved - move to next step
            motion_type = await self.get_motion_type()

            if motion_type and motion_type.requires_coalition_approval:
                self.motion.status = MotionStatus.EXTERNAL_REVIEW
            elif motion_type and motion_type.requires_faction_decision:
                self.motion.status = MotionStatus.ON_AGENDA
            else:
                self.motion.status = MotionStatus.APPROVED

    async def submit_to_coalition(self, submitted_by: Membership) -> list[CoalitionConsultation]:
        """Start coalition consultation for all coalition partners."""
        # Get all active coalition parties
        result = await self.db.execute(
            select(CouncilParty)
            .where(CouncilParty.organization_id == self.motion.organization_id)
            .where(CouncilParty.is_coalition_member == True)  # noqa: E712
            .where(CouncilParty.is_active == True)  # noqa: E712
            .order_by(CouncilParty.coalition_order)
        )
        parties = result.scalars().all()

        consultations = []
        for party in parties:
            # Check if consultation already exists
            existing = await self.db.execute(
                select(CoalitionConsultation)
                .where(CoalitionConsultation.motion_id == self.motion.id)
                .where(CoalitionConsultation.party_id == party.id)
            )
            consultation = existing.scalar_one_or_none()

            if not consultation:
                consultation = CoalitionConsultation(
                    motion_id=self.motion.id,
                    party_id=party.id,
                    sent_by_id=submitted_by.id,
                    sent_at=datetime.now(),
                    sent_via="email",
                )
                self.db.add(consultation)

            consultations.append(consultation)

        self.motion.status = MotionStatus.EXTERNAL_REVIEW
        await self.db.flush()

        return consultations

    async def record_coalition_response(
        self,
        party_id: UUID,
        result: CoalitionResult,
        response_note: str | None = None,
    ) -> CoalitionConsultation:
        """Record a coalition partner's response."""
        query_result = await self.db.execute(
            select(CoalitionConsultation)
            .where(CoalitionConsultation.motion_id == self.motion.id)
            .where(CoalitionConsultation.party_id == party_id)
        )
        consultation = query_result.scalar_one_or_none()

        if not consultation:
            raise ValueError("Coalition consultation not found")

        consultation.result = result
        consultation.response_note = response_note
        consultation.response_received_at = datetime.now()

        # Check if all consultations are complete
        await self._check_coalition_completion()

        await self.db.flush()
        return consultation

    async def _check_coalition_completion(self) -> None:
        """Check if coalition consultation is complete and update status."""
        if self.motion.coalition_status == "rejected":
            # Keep in external_review but blocked
            return

        if self.motion.coalition_status == "approved":
            motion_type = await self.get_motion_type()

            if motion_type and motion_type.requires_faction_decision:
                self.motion.status = MotionStatus.ON_AGENDA
            else:
                self.motion.status = MotionStatus.APPROVED

    async def submit_motion(self, submitted_by: Membership) -> None:
        """Submit the motion (final step)."""
        can_proceed, reason = await self.can_proceed()

        if not can_proceed:
            raise ValueError(f"Cannot submit motion: {reason}")

        self.motion.status = MotionStatus.SUBMITTED
        self.motion.submitted_at = datetime.now()

        await self.db.flush()

    async def withdraw_motion(self, withdrawn_by: Membership, reason: str | None = None) -> None:
        """Withdraw the motion."""
        if self.motion.status in (MotionStatus.SUBMITTED, MotionStatus.ACCEPTED):
            raise ValueError("Cannot withdraw submitted or accepted motion")

        self.motion.status = MotionStatus.WITHDRAWN
        self.motion.metadata["withdrawal_reason"] = reason
        self.motion.metadata["withdrawn_by"] = str(withdrawn_by.id)
        self.motion.metadata["withdrawn_at"] = datetime.now().isoformat()

        await self.db.flush()


class CoAuthorService:
    """Service for managing co-authors on motions."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def invite_co_author(
        self,
        motion: Motion,
        invited_by: Membership,
        invited_member_id: UUID,
        message: str | None = None,
    ) -> MotionCoAuthorInvite:
        """Invite a member to become a co-author."""
        # Check if already a co-author
        if any(ca.id == invited_member_id for ca in motion.co_authors):
            raise ValueError("Member is already a co-author")

        # Check if already invited
        result = await self.db.execute(
            select(MotionCoAuthorInvite)
            .where(MotionCoAuthorInvite.motion_id == motion.id)
            .where(MotionCoAuthorInvite.invited_member_id == invited_member_id)
            .where(MotionCoAuthorInvite.status == CoAuthorInviteStatus.PENDING)
        )
        if result.scalar_one_or_none():
            raise ValueError("Member already has a pending invitation")

        invite = MotionCoAuthorInvite(
            motion_id=motion.id,
            invited_by_id=invited_by.id,
            invited_member_id=invited_member_id,
            message=message,
        )
        self.db.add(invite)
        await self.db.flush()

        return invite

    async def respond_to_invite(
        self,
        invite_id: UUID,
        member: Membership,
        accept: bool,
    ) -> MotionCoAuthorInvite:
        """Respond to a co-author invitation."""
        result = await self.db.execute(
            select(MotionCoAuthorInvite)
            .where(MotionCoAuthorInvite.id == invite_id)
            .options(selectinload(MotionCoAuthorInvite.motion))
        )
        invite = result.scalar_one_or_none()

        if not invite:
            raise ValueError("Invitation not found")

        if invite.invited_member_id != member.id:
            raise ValueError("This invitation is not for you")

        if invite.status != CoAuthorInviteStatus.PENDING:
            raise ValueError("Invitation already responded to")

        invite.status = CoAuthorInviteStatus.ACCEPTED if accept else CoAuthorInviteStatus.DECLINED
        invite.responded_at = datetime.now()

        if accept:
            # Add to co-authors
            invite.motion.co_authors.append(member)

        await self.db.flush()
        return invite

    async def get_pending_invitations(self, member: Membership) -> list[MotionCoAuthorInvite]:
        """Get all pending co-author invitations for a member."""
        result = await self.db.execute(
            select(MotionCoAuthorInvite)
            .where(MotionCoAuthorInvite.invited_member_id == member.id)
            .where(MotionCoAuthorInvite.status == CoAuthorInviteStatus.PENDING)
            .options(selectinload(MotionCoAuthorInvite.motion))
        )
        return list(result.scalars().all())


class MotionShareService:
    """Service for sharing motions."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def share_with_party(
        self,
        motion: Motion,
        shared_by: Membership,
        party: CouncilParty,
        method: ShareMethod = ShareMethod.EMAIL,
        custom_message: str | None = None,
    ) -> MotionShareLog:
        """Share a motion with a council party."""
        # TODO: Integrate with email service

        success = True
        error_message = None

        # For now, just log the share
        # In production, this would send an email

        log = MotionShareLog(
            motion_id=motion.id,
            shared_by_id=shared_by.id,
            shared_with_party_id=party.id,
            shared_with_email=party.email,
            method=method,
            success=success,
            error_message=error_message,
            note=custom_message,
        )
        self.db.add(log)

        # If this is a coalition party, update consultation
        if party.is_coalition_member:
            result = await self.db.execute(
                select(CoalitionConsultation)
                .where(CoalitionConsultation.motion_id == motion.id)
                .where(CoalitionConsultation.party_id == party.id)
            )
            consultation = result.scalar_one_or_none()

            if consultation:
                consultation.sent_at = datetime.now()
                consultation.sent_by_id = shared_by.id
                consultation.sent_via = method.value
            else:
                consultation = CoalitionConsultation(
                    motion_id=motion.id,
                    party_id=party.id,
                    sent_by_id=shared_by.id,
                    sent_at=datetime.now(),
                    sent_via=method.value,
                )
                self.db.add(consultation)

        await self.db.flush()
        return log

    async def share_with_coalition(
        self,
        motion: Motion,
        shared_by: Membership,
        custom_message: str | None = None,
    ) -> list[MotionShareLog]:
        """Share a motion with all coalition partners."""
        result = await self.db.execute(
            select(CouncilParty)
            .where(CouncilParty.organization_id == motion.organization_id)
            .where(CouncilParty.is_coalition_member == True)  # noqa: E712
            .where(CouncilParty.is_active == True)  # noqa: E712
        )
        parties = result.scalars().all()

        logs = []
        for party in parties:
            log = await self.share_with_party(
                motion=motion,
                shared_by=shared_by,
                party=party,
                custom_message=custom_message,
            )
            logs.append(log)

        return logs

    async def get_share_history(self, motion: Motion) -> list[MotionShareLog]:
        """Get the sharing history for a motion."""
        result = await self.db.execute(
            select(MotionShareLog)
            .where(MotionShareLog.motion_id == motion.id)
            .order_by(MotionShareLog.shared_at.desc())
        )
        return list(result.scalars().all())


class MotionTypeService:
    """Service for managing motion types."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_types_for_organization(self, organization_id: UUID) -> list[MotionType]:
        """Get all motion types for an organization."""
        result = await self.db.execute(
            select(MotionType)
            .where(MotionType.organization_id == organization_id)
            .where(MotionType.is_active == True)  # noqa: E712
            .order_by(MotionType.display_order)
        )
        return list(result.scalars().all())

    async def get_available_types_for_member(
        self,
        organization_id: UUID,
        member: Membership,
    ) -> list[MotionType]:
        """Get motion types that a member is allowed to create."""
        types = await self.get_types_for_organization(organization_id)

        available = []
        for motion_type in types:
            if not motion_type.allowed_creator_roles:
                # No restrictions
                available.append(motion_type)
            elif member.role.value in motion_type.allowed_creator_roles:
                available.append(motion_type)

        return available

    async def should_recommend_co_author(
        self,
        motion_type: MotionType,
        member: Membership,
    ) -> tuple[bool, str | None]:
        """
        Check if co-author should be recommended for this member and motion type.

        Returns:
            Tuple of (should_recommend, recommendation_message)
        """
        if not motion_type.recommend_co_author:
            return False, None

        # Check if member's role triggers the recommendation
        if motion_type.recommend_co_author_roles:
            if member.role.value not in motion_type.recommend_co_author_roles:
                # Member's role doesn't need recommendation
                return False, None

        # Get the recommendation message
        message = motion_type.co_author_recommendation_message
        if not message:
            message = "Für diesen Dokumenttyp wird empfohlen, einen Co-Autor hinzuzufügen."

        return True, message

    @staticmethod
    def get_default_types() -> list[dict]:
        """Get default motion type configurations."""
        return [
            {
                "name": "Antrag",
                "slug": "antrag",
                "description": "Formeller Antrag zur Abstimmung im Rat",
                "icon": "document-text",
                "requires_approval": True,
                "requires_coalition_approval": True,
                "requires_faction_decision": True,
                "default_approvers": ["chair", "managing_director"],
                "recommend_co_author": False,
            },
            {
                "name": "Anfrage",
                "slug": "anfrage",
                "description": "Anfrage an die Verwaltung",
                "icon": "question-mark-circle",
                "requires_approval": True,
                "requires_coalition_approval": False,
                "requires_faction_decision": False,
                "default_approvers": ["chair"],
                "recommend_co_author": True,
                "recommend_co_author_roles": ["expert_citizen"],
                "co_author_recommendation_message": (
                    "Für Anfragen von Sachkundigen Bürger*innen empfehlen wir, "
                    "eine Ratsperson als Co-Autor hinzuzufügen."
                ),
            },
            {
                "name": "Pressemitteilung",
                "slug": "pressemitteilung",
                "description": "Öffentliche Pressemitteilung",
                "icon": "newspaper",
                "requires_approval": True,
                "requires_coalition_approval": False,
                "requires_faction_decision": False,
                "default_approvers": ["managing_director"],
                "recommend_co_author": False,
            },
            {
                "name": "Stellungnahme",
                "slug": "stellungnahme",
                "description": "Stellungnahme zu einem Thema",
                "icon": "chat-bubble-left-right",
                "requires_approval": True,
                "requires_coalition_approval": True,
                "requires_faction_decision": False,
                "default_approvers": ["chair"],
                "recommend_co_author": False,
            },
            {
                "name": "Internes Dokument",
                "slug": "intern",
                "description": "Internes Arbeitsdokument ohne Genehmigungsprozess",
                "icon": "document",
                "requires_approval": False,
                "requires_coalition_approval": False,
                "requires_faction_decision": False,
                "default_approvers": [],
                "recommend_co_author": False,
            },
        ]


# =============================================================================
# Faction Meeting Services
# =============================================================================


def generate_public_id(length: int = 12) -> str:
    """Generate a URL-safe public ID."""
    import secrets
    import string
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class MeetingLifecycleService:
    """
    Service for managing meeting lifecycle:
    DRAFT → SCHEDULED → IN_PROGRESS → COMPLETED → PROTOCOL_APPROVED
    """

    def __init__(self, db: AsyncSession, meeting: FactionMeeting):
        self.db = db
        self.meeting = meeting

    async def schedule_meeting(self, scheduled_by: Membership) -> FactionMeeting:
        """
        Schedule a meeting (transition from DRAFT to SCHEDULED).
        Creates invitations and sends them if enabled.
        """
        if self.meeting.status != MeetingStatus.DRAFT:
            raise ValueError("Only draft meetings can be scheduled")

        self.meeting.status = MeetingStatus.SCHEDULED
        await self.db.flush()

        return self.meeting

    async def start_meeting(self, started_by: Membership) -> FactionMeeting:
        """Start a meeting (transition from SCHEDULED to IN_PROGRESS)."""
        if self.meeting.status != MeetingStatus.SCHEDULED:
            raise ValueError("Only scheduled meetings can be started")

        self.meeting.status = MeetingStatus.IN_PROGRESS
        self.meeting.actual_start = datetime.now()
        await self.db.flush()

        return self.meeting

    async def end_meeting(
        self,
        ended_by: Membership,
        lock_protocol: bool = True,
        lock_attendance: bool = True,
    ) -> FactionMeeting:
        """End a meeting (transition from IN_PROGRESS to COMPLETED)."""
        if self.meeting.status != MeetingStatus.IN_PROGRESS:
            raise ValueError("Only in-progress meetings can be ended")

        self.meeting.status = MeetingStatus.COMPLETED
        self.meeting.actual_end = datetime.now()

        if lock_protocol:
            await self._lock_protocol(ended_by)

        if lock_attendance:
            self.meeting.attendance_locked = True
            self.meeting.attendance_locked_at = datetime.now()

        await self.db.flush()
        return self.meeting

    async def cancel_meeting(
        self,
        cancelled_by: Membership,
        reason: str | None = None,
    ) -> FactionMeeting:
        """Cancel a meeting."""
        if self.meeting.status in (MeetingStatus.COMPLETED, MeetingStatus.PROTOCOL_APPROVED):
            raise ValueError("Cannot cancel completed meetings")

        self.meeting.status = MeetingStatus.CANCELLED
        await self.db.flush()
        return self.meeting

    async def approve_previous_protocol(self, approved_by: Membership) -> bool:
        """
        Approve the protocol of the previous meeting.
        Called when TOP 1 is resolved.
        """
        if not self.meeting.previous_meeting:
            return False

        prev = self.meeting.previous_meeting
        if prev.protocol_approved:
            return False  # Already approved

        prev.protocol_approved = True
        prev.protocol_approved_at = datetime.now()
        prev.protocol_approved_in_id = self.meeting.id
        prev.public_protocol_enabled = True
        prev.status = MeetingStatus.PROTOCOL_APPROVED

        await self.db.flush()
        return True

    async def _lock_protocol(self, locked_by: Membership) -> FactionProtocolRevision:
        """Lock the protocol and create a revision."""
        import hashlib
        import json

        if self.meeting.protocol_locked:
            raise ValueError("Protocol is already locked")

        self.meeting.protocol_locked = True
        self.meeting.protocol_locked_at = datetime.now()
        self.meeting.protocol_locked_by_id = locked_by.id

        # Create protocol snapshot
        snapshot = await self._create_protocol_snapshot()

        # Calculate hash
        content_str = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        content_hash = hashlib.sha256(content_str.encode()).hexdigest()

        # Get next revision number
        result = await self.db.execute(
            select(FactionProtocolRevision)
            .where(FactionProtocolRevision.meeting_id == self.meeting.id)
            .order_by(FactionProtocolRevision.revision_number.desc())
        )
        last_rev = result.scalar_one_or_none()
        next_rev_num = (last_rev.revision_number + 1) if last_rev else 1

        revision = FactionProtocolRevision(
            meeting_id=self.meeting.id,
            revision_number=next_rev_num,
            reason="finalized",
            content_snapshot=snapshot,
            content_hash=content_hash,
            notes="Protokoll nach Sitzungsende gesperrt",
            created_by_id=locked_by.id,
        )
        self.db.add(revision)
        await self.db.flush()

        return revision

    async def _create_protocol_snapshot(self) -> dict:
        """Create a snapshot of the full protocol."""
        result = await self.db.execute(
            select(FactionAgendaItem)
            .where(FactionAgendaItem.meeting_id == self.meeting.id)
            .options(
                selectinload(FactionAgendaItem.protocol_entries),
                selectinload(FactionAgendaItem.decisions),
            )
            .order_by(FactionAgendaItem.sort_order)
        )
        agenda_items = result.scalars().all()

        snapshot = {
            "meeting": {
                "id": str(self.meeting.id),
                "title": self.meeting.title,
                "scheduled_date": self.meeting.scheduled_date.isoformat(),
                "actual_start": self.meeting.actual_start.isoformat() if self.meeting.actual_start else None,
                "actual_end": self.meeting.actual_end.isoformat() if self.meeting.actual_end else None,
            },
            "agenda_items": [],
        }

        for item in agenda_items:
            item_data = {
                "id": str(item.id),
                "number": item.number,
                "title": item.title,
                "visibility": item.visibility.value,
                "entries": [
                    {
                        "id": str(entry.id),
                        "type": entry.entry_type.value,
                        "content": entry.content,
                    }
                    for entry in item.protocol_entries
                ],
                "decisions": [
                    {
                        "id": str(decision.id),
                        "type": decision.decision_type.value,
                        "text": decision.decision_text,
                        "votes_for": decision.votes_for,
                        "votes_against": decision.votes_against,
                        "votes_abstain": decision.votes_abstain,
                    }
                    for decision in item.decisions
                ],
            }
            snapshot["agenda_items"].append(item_data)

        return snapshot


class AgendaSuggestionService:
    """
    Service for managing agenda item suggestions.
    Implements role-based approval workflow.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    def requires_approval(self, member: Membership) -> bool:
        """Check if member needs approval for agenda suggestions."""
        return not member.can_add_agenda_directly

    async def suggest_agenda_item(
        self,
        meeting: FactionMeeting,
        suggested_by: Membership,
        title: str,
        description: str | None = None,
        visibility: AgendaItemVisibility = AgendaItemVisibility.INTERNAL,
        parent_id: UUID | None = None,
        related_motion_id: UUID | None = None,
        estimated_duration_minutes: int | None = None,
    ) -> FactionAgendaItem:
        """
        Create an agenda item suggestion.
        If member can add directly, item is auto-approved.
        """
        needs_approval = self.requires_approval(suggested_by)

        # Get next sort order
        result = await self.db.execute(
            select(FactionAgendaItem)
            .where(FactionAgendaItem.meeting_id == meeting.id)
            .where(FactionAgendaItem.parent_id == parent_id)
            .order_by(FactionAgendaItem.sort_order.desc())
        )
        last_item = result.scalar_one_or_none()
        next_order = (last_item.sort_order + 1) if last_item else 0

        item = FactionAgendaItem(
            meeting_id=meeting.id,
            parent_id=parent_id,
            title=title,
            description=description,
            visibility=visibility,
            sort_order=next_order,
            suggestion_status=(
                AgendaSuggestionStatus.PENDING if needs_approval
                else AgendaSuggestionStatus.APPROVED
            ),
            suggested_by_id=suggested_by.id,
            approved_by_id=None if needs_approval else suggested_by.id,
            approved_at=None if needs_approval else datetime.now(),
            related_motion_id=related_motion_id,
            estimated_duration_minutes=estimated_duration_minutes,
        )
        self.db.add(item)
        await self.db.flush()

        # Auto-generate number
        item.number = self._generate_number(item, parent_id)
        await self.db.flush()

        return item

    async def approve_suggestion(
        self,
        item: FactionAgendaItem,
        approved_by: Membership,
    ) -> FactionAgendaItem:
        """Approve an agenda item suggestion."""
        if not approved_by.can_approve_agenda_suggestions:
            raise ValueError("You don't have permission to approve suggestions")

        if item.suggestion_status != AgendaSuggestionStatus.PENDING:
            raise ValueError("Item is not pending approval")

        item.suggestion_status = AgendaSuggestionStatus.APPROVED
        item.approved_by_id = approved_by.id
        item.approved_at = datetime.now()
        await self.db.flush()

        return item

    async def reject_suggestion(
        self,
        item: FactionAgendaItem,
        rejected_by: Membership,
        reason: str | None = None,
    ) -> FactionAgendaItem:
        """Reject an agenda item suggestion."""
        if not rejected_by.can_approve_agenda_suggestions:
            raise ValueError("You don't have permission to reject suggestions")

        if item.suggestion_status != AgendaSuggestionStatus.PENDING:
            raise ValueError("Item is not pending approval")

        item.suggestion_status = AgendaSuggestionStatus.REJECTED
        item.rejection_reason = reason
        await self.db.flush()

        return item

    async def get_pending_suggestions(self, meeting: FactionMeeting) -> list[FactionAgendaItem]:
        """Get all pending suggestions for a meeting."""
        result = await self.db.execute(
            select(FactionAgendaItem)
            .where(FactionAgendaItem.meeting_id == meeting.id)
            .where(FactionAgendaItem.suggestion_status == AgendaSuggestionStatus.PENDING)
            .order_by(FactionAgendaItem.created_at)
        )
        return list(result.scalars().all())

    def _generate_number(self, item: FactionAgendaItem, parent_id: UUID | None) -> str:
        """Generate a display number for an agenda item."""
        if parent_id:
            # Sub-item: e.g., "5.1"
            return ""  # Will be computed via display_number property
        return str(item.sort_order + 1)


class MotionAgendaService:
    """Service for adding motions to meeting agenda."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_motion_to_agenda(
        self,
        meeting: FactionMeeting,
        motion: Motion,
        added_by: Membership,
        parent_id: UUID | None = None,
    ) -> FactionAgendaItem:
        """
        Add a motion as a sub-item on the agenda.
        Updates motion status to ON_AGENDA.
        """
        # Get next sort order under parent
        result = await self.db.execute(
            select(FactionAgendaItem)
            .where(FactionAgendaItem.meeting_id == meeting.id)
            .where(FactionAgendaItem.parent_id == parent_id)
            .order_by(FactionAgendaItem.sort_order.desc())
        )
        last_item = result.scalar_one_or_none()
        next_order = (last_item.sort_order + 1) if last_item else 0

        item = FactionAgendaItem(
            meeting_id=meeting.id,
            parent_id=parent_id,
            title=motion.title,
            description=motion.summary,
            visibility=AgendaItemVisibility.INTERNAL,
            sort_order=next_order,
            suggestion_status=AgendaSuggestionStatus.APPROVED,
            suggested_by_id=added_by.id,
            approved_by_id=added_by.id,
            approved_at=datetime.now(),
            related_motion_id=motion.id,
        )
        self.db.add(item)

        # Update motion status
        motion.status = MotionStatus.ON_AGENDA
        await self.db.flush()

        return item

    async def record_decision_for_motion(
        self,
        agenda_item: FactionAgendaItem,
        decision_type: DecisionType,
        decision_text: str,
        recorded_by: Membership,
        votes_for: int | None = None,
        votes_against: int | None = None,
        votes_abstain: int | None = None,
    ) -> FactionDecision:
        """
        Record a decision for a motion and update its status.
        """
        if not agenda_item.related_motion_id:
            raise ValueError("Agenda item has no related motion")

        is_unanimous = decision_type in (
            DecisionType.APPROVED_UNANIMOUS,
            DecisionType.REJECTED_UNANIMOUS,
        )

        decision = FactionDecision(
            agenda_item_id=agenda_item.id,
            decision_type=decision_type,
            decision_text=decision_text,
            votes_for=votes_for,
            votes_against=votes_against,
            votes_abstain=votes_abstain,
            is_unanimous=is_unanimous,
            motion_id=agenda_item.related_motion_id,
            created_by_id=recorded_by.id,
        )
        self.db.add(decision)

        # Update motion status based on decision
        result = await self.db.execute(
            select(Motion).where(Motion.id == agenda_item.related_motion_id)
        )
        motion = result.scalar_one_or_none()

        if motion:
            if decision_type in (DecisionType.APPROVED_UNANIMOUS, DecisionType.APPROVED_MAJORITY):
                motion.status = MotionStatus.APPROVED
            elif decision_type in (DecisionType.REJECTED_UNANIMOUS, DecisionType.REJECTED_MAJORITY):
                motion.status = MotionStatus.REJECTED
            elif decision_type == DecisionType.WITHDRAWN:
                motion.status = MotionStatus.WITHDRAWN

        await self.db.flush()
        return decision


class MeetingInvitationService:
    """Service for managing meeting invitations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def invite_all_members(
        self,
        meeting: FactionMeeting,
        invited_by: Membership,
    ) -> list[FactionMeetingInvitation]:
        """Create invitations for all active members of the organization."""
        result = await self.db.execute(
            select(Membership)
            .where(Membership.organization_id == meeting.organization_id)
            .where(Membership.is_active == True)  # noqa: E712
        )
        members = result.scalars().all()

        invitations = []
        for member in members:
            # Skip if already invited
            existing = await self.db.execute(
                select(FactionMeetingInvitation)
                .where(FactionMeetingInvitation.meeting_id == meeting.id)
                .where(FactionMeetingInvitation.membership_id == member.id)
            )
            if existing.scalar_one_or_none():
                continue

            invitation = FactionMeetingInvitation(
                meeting_id=meeting.id,
                membership_id=member.id,
                invited_by_id=invited_by.id,
            )
            self.db.add(invitation)
            invitations.append(invitation)

        await self.db.flush()
        return invitations

    async def send_invitations(
        self,
        meeting: FactionMeeting,
        invitation_ids: list[UUID] | None = None,
    ) -> int:
        """
        Send invitation emails.
        Returns number of emails sent.
        """
        query = (
            select(FactionMeetingInvitation)
            .where(FactionMeetingInvitation.meeting_id == meeting.id)
            .where(FactionMeetingInvitation.invitation_sent_at == None)  # noqa: E711
        )
        if invitation_ids:
            query = query.where(FactionMeetingInvitation.id.in_(invitation_ids))

        result = await self.db.execute(query)
        invitations = result.scalars().all()

        sent_count = 0
        for invitation in invitations:
            # TODO: Integrate with email service
            invitation.invitation_sent_at = datetime.now()
            sent_count += 1

        await self.db.flush()
        return sent_count

    async def update_rsvp(
        self,
        invitation: FactionMeetingInvitation,
        status: RSVPStatus,
        note: str | None = None,
    ) -> FactionMeetingInvitation:
        """Update RSVP status for an invitation."""
        invitation.rsvp_status = status
        invitation.rsvp_note = note
        invitation.rsvp_responded_at = datetime.now()
        await self.db.flush()
        return invitation


class AttendanceService:
    """Service for managing meeting attendance."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def record_attendance(
        self,
        meeting: FactionMeeting,
        recorded_by: Membership,
        membership_id: UUID | None = None,
        guest_name: str | None = None,
        attendance_type: AttendanceType = AttendanceType.IN_PERSON,
        status: AttendanceStatus = AttendanceStatus.PRESENT,
        note: str | None = None,
    ) -> FactionAttendance:
        """Record attendance for a member or guest."""
        is_guest = guest_name is not None

        if not is_guest and not membership_id:
            raise ValueError("Must provide either membership_id or guest_name")

        attendance = FactionAttendance(
            meeting_id=meeting.id,
            membership_id=membership_id,
            guest_name=guest_name,
            is_guest=is_guest,
            attendance_type=attendance_type,
            status=status,
            note=note,
            recorded_by_id=recorded_by.id,
        )
        self.db.add(attendance)
        await self.db.flush()

        return attendance

    async def check_in(
        self,
        attendance: FactionAttendance,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> FactionAttendance:
        """Perform check-in for attendance."""
        now = datetime.now()
        attendance.check_in_time = now
        attendance.is_checked_in = True
        attendance.last_activity_time = now
        await self.db.flush()
        return attendance

    async def check_out(
        self,
        attendance: FactionAttendance,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> FactionAttendance:
        """Perform check-out for attendance."""
        import hashlib

        now = datetime.now()
        attendance.check_out_time = now
        attendance.is_checked_in = False

        # Calculate duration
        if attendance.check_in_time:
            delta = now - attendance.check_in_time
            attendance.calculated_duration_minutes = int(delta.total_seconds() / 60)

        # Create digital signature
        signature_data = (
            f"{attendance.meeting_id}|"
            f"{attendance.membership_id or attendance.guest_name}|"
            f"{attendance.check_in_time}|{now}"
        )
        attendance.signature_hash = hashlib.sha256(signature_data.encode()).hexdigest()
        attendance.signature_timestamp = now
        if ip_address:
            attendance.signature_ip_address = ip_address

        await self.db.flush()
        return attendance

    async def update_activity(self, attendance: FactionAttendance) -> FactionAttendance:
        """Update last activity time for attendance."""
        attendance.last_activity_time = datetime.now()
        await self.db.flush()
        return attendance

    async def handle_timeout(
        self,
        meeting: FactionMeeting,
        timeout_minutes: int = 120,
    ) -> list[FactionAttendance]:
        """Handle timeout for inactive attendees."""
        now = datetime.now()
        timeout_threshold = now - timedelta(minutes=timeout_minutes)

        result = await self.db.execute(
            select(FactionAttendance)
            .where(FactionAttendance.meeting_id == meeting.id)
            .where(FactionAttendance.is_checked_in == True)  # noqa: E712
            .where(FactionAttendance.last_activity_time < timeout_threshold)
        )
        timed_out = result.scalars().all()

        for attendance in timed_out:
            attendance.check_out_time = attendance.last_activity_time
            attendance.is_checked_in = False
            if attendance.check_in_time and attendance.last_activity_time:
                delta = attendance.last_activity_time - attendance.check_in_time
                attendance.calculated_duration_minutes = int(delta.total_seconds() / 60)
            if attendance.note:
                attendance.note += "\n[Automatischer Checkout wegen Timeout]"
            else:
                attendance.note = "[Automatischer Checkout wegen Timeout]"

        await self.db.flush()
        return list(timed_out)


class ProtocolService:
    """Service for managing protocol entries."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_entry(
        self,
        agenda_item: FactionAgendaItem,
        created_by: Membership,
        entry_type: ProtocolEntryType,
        content: str,
        assigned_to_id: UUID | None = None,
        due_date: datetime | None = None,
        visibility_override: AgendaItemVisibility | None = None,
    ) -> FactionProtocolEntry:
        """Add a protocol entry to an agenda item."""
        # Get next sort order
        result = await self.db.execute(
            select(FactionProtocolEntry)
            .where(FactionProtocolEntry.agenda_item_id == agenda_item.id)
            .order_by(FactionProtocolEntry.sort_order.desc())
        )
        last_entry = result.scalar_one_or_none()
        next_order = (last_entry.sort_order + 1) if last_entry else 0

        entry = FactionProtocolEntry(
            agenda_item_id=agenda_item.id,
            entry_type=entry_type,
            content=content,
            sort_order=next_order,
            assigned_to_id=assigned_to_id,
            due_date=due_date,
            visibility_override=visibility_override,
            created_by_id=created_by.id,
        )
        self.db.add(entry)
        await self.db.flush()

        return entry

    async def add_decision(
        self,
        agenda_item: FactionAgendaItem,
        created_by: Membership,
        decision_type: DecisionType,
        decision_text: str,
        votes_for: int | None = None,
        votes_against: int | None = None,
        votes_abstain: int | None = None,
    ) -> FactionDecision:
        """Record a decision for an agenda item."""
        is_unanimous = decision_type in (
            DecisionType.APPROVED_UNANIMOUS,
            DecisionType.REJECTED_UNANIMOUS,
        )

        decision = FactionDecision(
            agenda_item_id=agenda_item.id,
            decision_type=decision_type,
            decision_text=decision_text,
            votes_for=votes_for,
            votes_against=votes_against,
            votes_abstain=votes_abstain,
            is_unanimous=is_unanimous,
            motion_id=agenda_item.related_motion_id,
            created_by_id=created_by.id,
        )
        self.db.add(decision)
        await self.db.flush()

        return decision

    async def get_protocol_for_meeting(
        self,
        meeting: FactionMeeting,
    ) -> list[FactionAgendaItem]:
        """Get full protocol with all entries for a meeting."""
        result = await self.db.execute(
            select(FactionAgendaItem)
            .where(FactionAgendaItem.meeting_id == meeting.id)
            .where(FactionAgendaItem.suggestion_status == AgendaSuggestionStatus.APPROVED)
            .options(
                selectinload(FactionAgendaItem.protocol_entries),
                selectinload(FactionAgendaItem.decisions),
                selectinload(FactionAgendaItem.sub_items).options(
                    selectinload(FactionAgendaItem.protocol_entries),
                    selectinload(FactionAgendaItem.decisions),
                ),
            )
            .order_by(FactionAgendaItem.sort_order)
        )
        return list(result.scalars().all())


class MeetingSetupService:
    """Service for setting up new meetings with standard agenda items."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_or_create_settings(
        self,
        organization_id: UUID,
    ) -> FactionMeetingSettings:
        """Get or create meeting settings for an organization."""
        result = await self.db.execute(
            select(FactionMeetingSettings)
            .where(FactionMeetingSettings.organization_id == organization_id)
        )
        settings = result.scalar_one_or_none()

        if not settings:
            settings = FactionMeetingSettings(organization_id=organization_id)
            self.db.add(settings)
            await self.db.flush()

        return settings

    async def create_meeting(
        self,
        organization_id: UUID,
        created_by: Membership,
        title: str,
        scheduled_date: datetime,
        scheduled_end: datetime | None = None,
        location: str | None = None,
        conference_link: str | None = None,
        description: str | None = None,
        previous_meeting_id: UUID | None = None,
    ) -> FactionMeeting:
        """Create a new meeting with standard setup."""
        settings = await self.get_or_create_settings(organization_id)

        # Generate unique public ID
        public_id = generate_public_id()

        # Apply defaults from settings
        if location is None:
            location = settings.default_location
        if conference_link is None:
            conference_link = settings.default_conference_link

        meeting = FactionMeeting(
            organization_id=organization_id,
            public_id=public_id,
            title=title,
            description=description,
            scheduled_date=scheduled_date,
            scheduled_end=scheduled_end,
            location=location,
            conference_link=conference_link,
            previous_meeting_id=previous_meeting_id,
            created_by_id=created_by.id,
        )
        self.db.add(meeting)
        await self.db.flush()

        # Create standard agenda items
        await self._create_standard_agenda_items(meeting, settings, created_by)

        return meeting

    async def _create_standard_agenda_items(
        self,
        meeting: FactionMeeting,
        settings: FactionMeetingSettings,
        created_by: Membership,
    ) -> None:
        """Create standard agenda items based on settings."""
        sort_order = 0

        # TOP 1: Approval of agenda and protocol
        if settings.auto_create_approval_item:
            if meeting.previous_meeting_id:
                # Get previous meeting date
                result = await self.db.execute(
                    select(FactionMeeting).where(FactionMeeting.id == meeting.previous_meeting_id)
                )
                prev_meeting = result.scalar_one_or_none()
                prev_date = prev_meeting.scheduled_date.strftime("%d.%m.%Y") if prev_meeting else "?"
                title = f"Beschließen der Tagesordnung und des Protokolls vom {prev_date}"
            else:
                title = "Beschließen der Tagesordnung"

            approval_item = FactionAgendaItem(
                meeting_id=meeting.id,
                number="1",
                title=title,
                visibility=AgendaItemVisibility.PUBLIC,
                sort_order=sort_order,
                is_public_section=True,
                is_approval_item=True,
                suggestion_status=AgendaSuggestionStatus.APPROVED,
                suggested_by_id=created_by.id,
                approved_by_id=created_by.id,
                approved_at=datetime.now(),
            )
            self.db.add(approval_item)
            sort_order += 1

        # Create "Verschiedenes" at the end if enabled
        if settings.auto_create_various_item:
            # This will be the last item, number assigned later
            various_item = FactionAgendaItem(
                meeting_id=meeting.id,
                title="Verschiedenes",
                visibility=AgendaItemVisibility.INTERNAL,
                sort_order=999,  # Will be renumbered
                is_various_item=True,
                suggestion_status=AgendaSuggestionStatus.APPROVED,
                suggested_by_id=created_by.id,
                approved_by_id=created_by.id,
                approved_at=datetime.now(),
            )
            self.db.add(various_item)

        await self.db.flush()

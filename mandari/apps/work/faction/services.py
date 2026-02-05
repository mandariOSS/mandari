# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Services for faction meeting functionality.

Includes:
- FactionMeetingEmailService: Email invitations and reminders
- AgendaProposalService: Handle agenda item proposals from Sachkundige Bürger*innen
- ProtocolApprovalService: Manage protocol approval workflow
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

logger = logging.getLogger(__name__)


class FactionMeetingEmailService:
    """Service for sending faction meeting emails."""

    def send_invitations(self, meeting) -> int:
        """
        Send invitation emails to all invited members.

        Returns the count of successfully sent emails.
        """
        attendances = meeting.attendances.filter(status="invited")
        sent_count = 0

        for attendance in attendances:
            if self._send_invitation_email(meeting, attendance):
                sent_count += 1

        return sent_count

    def _send_invitation_email(self, meeting, attendance) -> bool:
        """Send a single invitation email."""
        user = attendance.membership.user
        if not user.email:
            logger.warning(
                f"Skipping invitation for user {user.id} - no email address"
            )
            return False

        # Get agenda items based on whether the member is sworn in
        is_sworn_in = attendance.membership.is_sworn_in
        public_items = meeting.agenda_items.filter(
            visibility="public",
            proposal_status="active"
        ).order_by("order", "number")

        internal_items = []
        if is_sworn_in:
            internal_items = meeting.agenda_items.filter(
                visibility="internal",
                proposal_status="active"
            ).order_by("order", "number")

        context = {
            "meeting": meeting,
            "user": user,
            "organization": meeting.organization,
            "attendance": attendance,
            "public_agenda_items": public_items,
            "internal_agenda_items": internal_items,
            "is_sworn_in": is_sworn_in,
        }

        subject = f"Einladung: {meeting.title}"

        try:
            html_content = render_to_string(
                "work/faction/email/invitation.html", context
            )
            text_content = render_to_string(
                "work/faction/email/invitation.txt", context
            )
        except Exception as e:
            logger.error(f"Failed to render email template: {e}")
            # Fall back to simple text
            html_content = None
            text_content = self._get_simple_invitation_text(meeting, user, public_items, internal_items)

        try:
            send_mail(
                subject=subject,
                message=text_content,
                html_message=html_content,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
            logger.info(f"Invitation sent to {user.email} for meeting {meeting.id}")
            return True
        except Exception as e:
            logger.error(f"Failed to send invitation to {user.email}: {e}")
            return False

    def _get_simple_invitation_text(self, meeting, user, public_items=None, internal_items=None) -> str:
        """Generate simple text fallback for invitation email."""
        lines = [
            f"Hallo {user.first_name or user.email},",
            "",
            "du bist zur folgenden Fraktionssitzung eingeladen:",
            "",
            f"{meeting.title}",
            f"Datum: {meeting.start.strftime('%A, %d. %B %Y')}",
            f"Uhrzeit: {meeting.start.strftime('%H:%M')} Uhr",
        ]

        if meeting.location:
            lines.append(f"Ort: {meeting.location}")

        if meeting.video_link:
            lines.append(f"Video-Link: {meeting.video_link}")

        # Add agenda items
        if public_items:
            lines.extend(["", "TAGESORDNUNG", ""])
            for item in public_items:
                lines.append(f"TOP {item.number}: {item.title}")

        if internal_items:
            lines.extend(["", "NICHT-ÖFFENTLICHER TEIL", ""])
            for item in internal_items:
                lines.append(f"TOP {item.number}: {item.title}")

        lines.extend([
            "",
            "Bitte gib uns Bescheid, ob du teilnehmen kannst.",
            "",
            f"Viele Grüße,",
            f"{meeting.organization.name}",
        ])

        return "\n".join(lines)

    def send_reminder(self, meeting, hours_before: int = 24) -> int:
        """
        Send reminder emails to confirmed attendees.

        Returns the count of successfully sent emails.
        """
        attendances = meeting.attendances.filter(
            status__in=["confirmed", "tentative"]
        )
        sent_count = 0

        for attendance in attendances:
            if self._send_reminder_email(meeting, attendance, hours_before):
                sent_count += 1

        return sent_count

    def _send_reminder_email(self, meeting, attendance, hours_before: int) -> bool:
        """Send a single reminder email."""
        user = attendance.membership.user
        if not user.email:
            return False

        context = {
            "meeting": meeting,
            "user": user,
            "organization": meeting.organization,
            "attendance": attendance,
            "hours_before": hours_before,
        }

        subject = f"Erinnerung: {meeting.title} in {hours_before} Stunden"

        try:
            html_content = render_to_string(
                "work/faction/email/reminder.html", context
            )
            text_content = render_to_string(
                "work/faction/email/reminder.txt", context
            )
        except Exception:
            # Fall back to simple text
            html_content = None
            text_content = f"Erinnerung: {meeting.title} findet in {hours_before} Stunden statt."

        try:
            send_mail(
                subject=subject,
                message=text_content,
                html_message=html_content,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send reminder to {user.email}: {e}")
            return False


class AgendaProposalService:
    """
    Service for handling agenda item proposals from Sachkundige Bürger*innen.

    Allows members with 'agenda.propose' permission to suggest agenda items
    for upcoming meetings. These proposals must be reviewed and approved
    by members with 'agenda.manage' permission.
    """

    @classmethod
    def create_proposal(
        cls,
        meeting,
        title: str,
        description: str,
        proposed_by,
        visibility: str = "public",
    ):
        """
        Create a new agenda item proposal.

        Args:
            meeting: The FactionMeeting to add the proposal to
            title: Title of the proposed agenda item
            description: Description/content of the proposal
            proposed_by: Membership of the person proposing
            visibility: 'public' or 'internal'

        Returns:
            The created FactionAgendaItem in 'proposed' status
        """
        from .models import FactionAgendaItem

        item = FactionAgendaItem(
            meeting=meeting,
            title=title,
            visibility=visibility,
            proposal_status="proposed",
            proposed_by=proposed_by,
            proposed_at=timezone.now(),
            order=9999,  # Will be reordered when accepted
        )
        item.set_description_encrypted(description)
        item.save()

        logger.info(
            f"Agenda proposal created: '{title}' for meeting {meeting.id} "
            f"by {proposed_by.user.email}"
        )

        # Notify meeting managers
        cls._notify_managers(meeting, item, proposed_by)

        return item

    @classmethod
    def _notify_managers(cls, meeting, item, proposed_by):
        """Notify members with agenda.manage permission about the new proposal."""
        from apps.common.permissions import PermissionChecker
        from apps.work.notifications.services import NotificationHub
        from apps.work.notifications.models import NotificationType

        # Find all members with agenda.manage permission
        managers = []
        for membership in meeting.organization.memberships.filter(is_active=True):
            checker = PermissionChecker(membership)
            if checker.has_permission("agenda.manage"):
                managers.append(membership)

        if managers:
            NotificationHub.send_bulk(
                recipients=managers,
                notification_type=NotificationType.FACTION_MEETING_UPDATED,
                title="Neuer TOP-Vorschlag",
                message=f'{proposed_by.user.get_display_name()} hat einen TOP vorgeschlagen: "{item.title}"',
                link=f"/work/{meeting.organization.slug}/faction/{meeting.id}/",
                actor=proposed_by,
                metadata={"meeting_id": str(meeting.id), "item_id": str(item.id)},
            )

    @classmethod
    def accept_proposal(cls, item, reviewed_by, assign_number: str = None):
        """
        Accept a proposed agenda item.

        Args:
            item: The FactionAgendaItem to accept
            reviewed_by: Membership of the reviewer
            assign_number: Optional TOP number to assign

        Returns:
            True if accepted, False if already processed
        """
        if not item.accept_proposal(reviewed_by):
            return False

        if assign_number:
            item.number = assign_number
            item.save(update_fields=["number"])

        logger.info(
            f"Agenda proposal accepted: '{item.title}' by {reviewed_by.user.email}"
        )

        # Notify the proposer
        if item.proposed_by:
            from apps.work.notifications.services import NotificationHub
            from apps.work.notifications.models import NotificationType

            NotificationHub.send(
                recipient=item.proposed_by,
                notification_type=NotificationType.FACTION_MEETING_UPDATED,
                title="TOP-Vorschlag angenommen",
                message=f'Dein Vorschlag "{item.title}" wurde angenommen.',
                link=f"/work/{item.meeting.organization.slug}/faction/{item.meeting.id}/",
                actor=reviewed_by,
            )

        return True

    @classmethod
    def reject_proposal(cls, item, reviewed_by, reason: str = ""):
        """
        Reject a proposed agenda item.

        Args:
            item: The FactionAgendaItem to reject
            reviewed_by: Membership of the reviewer
            reason: Reason for rejection

        Returns:
            True if rejected, False if already processed
        """
        if not item.reject_proposal(reviewed_by, reason):
            return False

        logger.info(
            f"Agenda proposal rejected: '{item.title}' by {reviewed_by.user.email}"
        )

        # Notify the proposer
        if item.proposed_by:
            from apps.work.notifications.services import NotificationHub
            from apps.work.notifications.models import NotificationType

            message = f'Dein Vorschlag "{item.title}" wurde nicht angenommen.'
            if reason:
                message += f" Grund: {reason}"

            NotificationHub.send(
                recipient=item.proposed_by,
                notification_type=NotificationType.FACTION_MEETING_UPDATED,
                title="TOP-Vorschlag abgelehnt",
                message=message,
                link=f"/work/{item.meeting.organization.slug}/faction/{item.meeting.id}/",
                actor=reviewed_by,
            )

        return True

    @classmethod
    def get_pending_proposals(cls, meeting):
        """Get all pending proposals for a meeting."""
        return meeting.agenda_items.filter(proposal_status="proposed")

    @classmethod
    def get_proposals_by_member(cls, membership):
        """Get all proposals by a specific member across all meetings."""
        from .models import FactionAgendaItem

        return FactionAgendaItem.objects.filter(
            proposed_by=membership
        ).select_related("meeting").order_by("-proposed_at")


class ProtocolApprovalService:
    """
    Service for managing protocol approval workflow.

    The workflow:
    1. Meeting ends -> Protocol status is 'draft'
    2. Protocol is submitted for approval -> Status becomes 'pending'
    3. In the next meeting, the approval agenda item is voted on
    4. If approved -> Previous meeting's protocol status becomes 'approved'
    """

    @classmethod
    def submit_for_approval(cls, meeting):
        """
        Submit a meeting's protocol for approval in the next meeting.

        Args:
            meeting: The FactionMeeting whose protocol is ready

        Returns:
            True if submitted, False if already approved
        """
        if not meeting.submit_protocol_for_approval():
            return False

        logger.info(f"Protocol submitted for approval: meeting {meeting.id}")
        return True

    @classmethod
    def approve_protocol(cls, meeting, approved_in_meeting, approved_by):
        """
        Approve a meeting's protocol.

        Args:
            meeting: The FactionMeeting whose protocol is being approved
            approved_in_meeting: The meeting where approval is happening
            approved_by: Membership who approved

        Returns:
            True if approved, False if already approved
        """
        if meeting.protocol_approved:
            return False

        meeting.protocol_status = "approved"
        meeting.protocol_approved = True
        meeting.protocol_approved_at = timezone.now()
        meeting.protocol_approved_by = approved_by
        meeting.protocol_approved_in = approved_in_meeting
        meeting.save(update_fields=[
            "protocol_status",
            "protocol_approved",
            "protocol_approved_at",
            "protocol_approved_by",
            "protocol_approved_in",
        ])

        logger.info(
            f"Protocol approved: meeting {meeting.id} "
            f"approved in meeting {approved_in_meeting.id} "
            f"by {approved_by.user.email}"
        )

        return True

    @classmethod
    def get_pending_approvals(cls, organization):
        """
        Get all meetings with protocols pending approval.

        Args:
            organization: The Organization to filter by

        Returns:
            QuerySet of FactionMeeting with pending protocol status
        """
        from .models import FactionMeeting

        return FactionMeeting.objects.filter(
            organization=organization,
            protocol_status="pending",
        ).order_by("-start")

    @classmethod
    def auto_create_approval_item(cls, meeting):
        """
        Automatically create the protocol approval agenda item.

        Called when a new meeting is created to add the standard
        first agenda item for approving the previous meeting's protocol.

        Args:
            meeting: The new FactionMeeting

        Returns:
            The created FactionAgendaItem or None if no previous meeting
        """
        if not meeting.previous_meeting:
            # No previous meeting to approve
            return meeting.create_approval_agenda_item()

        # Ensure previous meeting's protocol is in pending status
        prev = meeting.previous_meeting
        if prev.protocol_status == "draft" and prev.status == "completed":
            prev.submit_protocol_for_approval()

        return meeting.create_approval_agenda_item()

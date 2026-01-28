# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Services for faction meeting functionality.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

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

        context = {
            "meeting": meeting,
            "user": user,
            "organization": meeting.organization,
            "attendance": attendance,
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
            text_content = self._get_simple_invitation_text(meeting, user)

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

    def _get_simple_invitation_text(self, meeting, user) -> str:
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

        lines.extend([
            "",
            "Bitte gib uns Bescheid, ob du teilnehmen kannst.",
            "",
            f"Viele Gruesse,",
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

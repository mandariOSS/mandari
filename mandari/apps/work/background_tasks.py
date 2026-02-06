# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Background tasks for the Work module.

Uses Django 6.0's native background tasks feature.
Tasks are configured via TASKS setting in settings.py.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


def send_notification_email_task(notification_id: str):
    """
    Send email for a notification asynchronously.

    This task is scheduled to run in the background after a notification is created.
    """
    from apps.work.notifications.models import Notification, NotificationPreference

    try:
        notification = Notification.objects.select_related("recipient__user", "actor__user").get(id=notification_id)
    except Notification.DoesNotExist:
        logger.error(f"Notification {notification_id} not found")
        return

    recipient_email = notification.recipient.user.email
    if not recipient_email:
        logger.warning(f"No email for notification recipient {notification_id}")
        return

    # Check user preferences
    try:
        prefs = NotificationPreference.objects.get(membership=notification.recipient)
        if not prefs.is_type_enabled(notification.notification_type, "email"):
            logger.info(f"Email disabled for notification type {notification.notification_type}")
            return
        if prefs.email_digest != "instant":
            logger.info("Email digest not instant, skipping immediate send")
            return
    except NotificationPreference.DoesNotExist:
        # Default to sending if no preferences set
        pass

    # Render email content
    context = {
        "notification": notification,
        "recipient": notification.recipient,
        "actor": notification.actor,
        "site_name": "Mandari Work",
        "base_url": getattr(settings, "SITE_URL", "http://localhost:8000"),
    }

    try:
        html_content = render_to_string("work/notifications/email/notification.html", context)
        text_content = strip_tags(html_content)
    except Exception as e:
        logger.error(f"Failed to render email template: {e}")
        return

    # Send email
    try:
        send_mail(
            subject=notification.title,
            message=text_content,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@mandari.de"),
            recipient_list=[recipient_email],
            html_message=html_content,
            fail_silently=False,
        )

        # Mark as sent
        notification.email_sent = True
        from django.utils import timezone

        notification.email_sent_at = timezone.now()
        notification.save(update_fields=["email_sent", "email_sent_at"])

        logger.info(f"Notification email sent to {recipient_email}")

    except Exception as e:
        logger.error(f"Failed to send email to {recipient_email}: {e}")


def send_meeting_invitation_task(meeting_id: str, attendance_id: str):
    """
    Send meeting invitation email asynchronously.

    Args:
        meeting_id: UUID of the FactionMeeting
        attendance_id: UUID of the FactionAttendance record
    """
    from apps.work.faction.models import FactionAttendance, FactionMeeting

    try:
        meeting = FactionMeeting.objects.select_related("organization").get(id=meeting_id)
        attendance = FactionAttendance.objects.select_related("membership__user").get(id=attendance_id)
    except (FactionMeeting.DoesNotExist, FactionAttendance.DoesNotExist) as e:
        logger.error(f"Meeting or attendance not found: {e}")
        return

    recipient_email = attendance.membership.user.email
    if not recipient_email:
        logger.warning(f"No email for meeting attendee {attendance_id}")
        return

    # Render email content
    context = {
        "meeting": meeting,
        "attendance": attendance,
        "recipient": attendance.membership,
        "organization": meeting.organization,
        "site_name": "Mandari Work",
        "base_url": getattr(settings, "SITE_URL", "http://localhost:8000"),
    }

    try:
        html_content = render_to_string("work/faction/email/invitation.html", context)
        text_content = strip_tags(html_content)
    except Exception as e:
        logger.error(f"Failed to render invitation email template: {e}")
        return

    # Send email
    try:
        send_mail(
            subject=f"Einladung: {meeting.title}",
            message=text_content,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@mandari.de"),
            recipient_list=[recipient_email],
            html_message=html_content,
            fail_silently=False,
        )

        # Mark invitation as sent
        from django.utils import timezone

        attendance.invitation_sent = True
        attendance.invitation_sent_at = timezone.now()
        attendance.save(update_fields=["invitation_sent", "invitation_sent_at"])

        logger.info(f"Meeting invitation sent to {recipient_email}")

    except Exception as e:
        logger.error(f"Failed to send invitation to {recipient_email}: {e}")


def send_meeting_reminder_task(meeting_id: str):
    """
    Send meeting reminder emails to all attendees.

    Args:
        meeting_id: UUID of the FactionMeeting
    """
    from apps.work.faction.models import FactionMeeting

    try:
        meeting = (
            FactionMeeting.objects.select_related("organization")
            .prefetch_related("attendances__membership__user")
            .get(id=meeting_id)
        )
    except FactionMeeting.DoesNotExist:
        logger.error(f"Meeting {meeting_id} not found")
        return

    # Send reminder to all confirmed attendees
    for attendance in meeting.attendances.filter(status__in=["confirmed", "pending"]):
        recipient_email = attendance.membership.user.email
        if not recipient_email:
            continue

        context = {
            "meeting": meeting,
            "attendance": attendance,
            "recipient": attendance.membership,
            "organization": meeting.organization,
            "site_name": "Mandari Work",
            "base_url": getattr(settings, "SITE_URL", "http://localhost:8000"),
        }

        try:
            html_content = render_to_string("work/faction/email/reminder.html", context)
            text_content = strip_tags(html_content)

            send_mail(
                subject=f"Erinnerung: {meeting.title}",
                message=text_content,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@mandari.de"),
                recipient_list=[recipient_email],
                html_message=html_content,
                fail_silently=True,  # Don't fail entire batch if one fails
            )

            logger.info(f"Meeting reminder sent to {recipient_email}")

        except Exception as e:
            logger.error(f"Failed to send reminder to {recipient_email}: {e}")

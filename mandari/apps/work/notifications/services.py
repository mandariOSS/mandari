# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Notification Hub/Service for the Work module.

Central service for sending notifications across the application.
Usage:
    from apps.work.notifications.services import NotificationHub

    # Send to single user
    NotificationHub.send(
        recipient=membership,
        notification_type=NotificationType.TASK_ASSIGNED,
        title="Neue Aufgabe",
        message="Dir wurde eine Aufgabe zugewiesen.",
        link="/work/org/tasks/123/",
        actor=assigner_membership,
    )

    # Send to multiple users
    NotificationHub.send_bulk(
        recipients=[member1, member2],
        notification_type=NotificationType.MEETING_REMINDER,
        title="Sitzungserinnerung",
        message="Die Sitzung beginnt in 30 Minuten.",
    )
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from django.conf import settings
from django.core.cache import cache
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

from .models import Notification, NotificationPreference, NotificationType

logger = logging.getLogger(__name__)


class NotificationHub:
    """
    Central notification service.

    Handles:
    - Creating in-app notifications
    - Sending email notifications (based on user preferences)
    - Respecting quiet hours
    - Rate limiting (optional)
    """

    @classmethod
    def send(
        cls,
        recipient,  # Membership instance
        notification_type: str,
        title: str,
        message: str,
        link: str = "",
        actor=None,  # Membership instance (optional)
        metadata: dict = None,
        send_email: bool = True,
    ) -> Optional[Notification]:
        """
        Send a notification to a single user.

        Args:
            recipient: The Membership to notify
            notification_type: Type of notification (from NotificationType)
            title: Notification title
            message: Notification message
            link: Optional link to related content
            actor: Optional Membership who triggered the notification
            metadata: Optional additional data
            send_email: Whether to send email (subject to user preferences)

        Returns:
            The created Notification instance, or None if filtered out
        """
        # Don't notify yourself
        if actor and actor.id == recipient.id:
            return None

        # Create the notification
        notification = Notification.objects.create(
            recipient=recipient,
            notification_type=notification_type,
            title=title,
            message=message,
            link=link,
            actor=actor,
            metadata=metadata or {},
        )

        # Invalidate count cache for recipient
        cls.invalidate_count_cache(recipient)

        # Handle email notification asynchronously
        if send_email:
            cls._queue_email(notification)

        logger.info(
            f"Notification sent: {notification_type} to {recipient.user.email}"
        )

        return notification

    @classmethod
    def send_bulk(
        cls,
        recipients: list,  # List of Membership instances
        notification_type: str,
        title: str,
        message: str,
        link: str = "",
        actor=None,
        metadata: dict = None,
        send_email: bool = True,
    ) -> list:
        """
        Send notifications to multiple users.

        Args:
            recipients: List of Memberships to notify
            notification_type: Type of notification
            title: Notification title
            message: Notification message
            link: Optional link
            actor: Optional Membership who triggered this
            metadata: Optional additional data
            send_email: Whether to send emails

        Returns:
            List of created Notification instances
        """
        notifications = []

        for recipient in recipients:
            notification = cls.send(
                recipient=recipient,
                notification_type=notification_type,
                title=title,
                message=message,
                link=link,
                actor=actor,
                metadata=metadata,
                send_email=send_email,
            )
            if notification:
                notifications.append(notification)

        return notifications

    @classmethod
    def send_to_organization(
        cls,
        organization,
        notification_type: str,
        title: str,
        message: str,
        link: str = "",
        actor=None,
        metadata: dict = None,
        exclude_actor: bool = True,
    ) -> list:
        """
        Send notification to all members of an organization.

        Args:
            organization: The Organization instance
            notification_type: Type of notification
            title: Notification title
            message: Notification message
            link: Optional link
            actor: Optional Membership who triggered this
            metadata: Optional additional data
            exclude_actor: Whether to exclude the actor from recipients

        Returns:
            List of created Notification instances
        """
        recipients = organization.memberships.filter(is_active=True)

        if exclude_actor and actor:
            recipients = recipients.exclude(id=actor.id)

        return cls.send_bulk(
            recipients=list(recipients),
            notification_type=notification_type,
            title=title,
            message=message,
            link=link,
            actor=actor,
            metadata=metadata,
        )

    @classmethod
    def _queue_email(cls, notification: Notification):
        """
        Queue email for a notification to be sent asynchronously.

        Uses Django 6.0's background tasks for async processing.
        """
        try:
            # Quick check if email is enabled before queueing
            prefs, _ = NotificationPreference.objects.get_or_create(
                membership=notification.recipient
            )

            # Skip if email is disabled for this type
            if not prefs.is_type_enabled(notification.notification_type, "email"):
                return

            # Skip if in quiet hours
            if cls._is_quiet_hours(prefs):
                return

            # Skip if not instant digest
            if prefs.email_digest != "instant":
                return

            # Queue the email task
            from apps.work.tasks import send_notification_email_task
            try:
                # Try using Django 6.0 background tasks
                from django.tasks import enqueue
                enqueue(send_notification_email_task, str(notification.id))
            except ImportError:
                # Fallback to synchronous execution if tasks not available
                send_notification_email_task(str(notification.id))

        except Exception as e:
            logger.error(f"Failed to queue notification email: {e}")

    @classmethod
    def _is_quiet_hours(cls, prefs: NotificationPreference) -> bool:
        """Check if we're currently in quiet hours."""
        if not prefs.quiet_hours_enabled:
            return False

        if not prefs.quiet_hours_start or not prefs.quiet_hours_end:
            return False

        now = timezone.localtime().time()
        start = prefs.quiet_hours_start
        end = prefs.quiet_hours_end

        # Handle overnight quiet hours (e.g., 22:00 - 07:00)
        if start > end:
            return now >= start or now <= end
        else:
            return start <= now <= end

    @classmethod
    def _send_notification_email(cls, notification: Notification):
        """Send the actual notification email."""
        recipient_email = notification.recipient.user.email

        if not recipient_email:
            return

        # Render email content
        context = {
            "notification": notification,
            "recipient": notification.recipient,
            "actor": notification.actor,
            "site_name": "Mandari Work",
            "base_url": getattr(settings, "SITE_URL", "http://localhost:8000"),
        }

        html_content = render_to_string(
            "work/notifications/email/notification.html",
            context
        )
        text_content = strip_tags(html_content)

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
            notification.email_sent_at = timezone.now()
            notification.save(update_fields=["email_sent", "email_sent_at"])

            logger.info(f"Notification email sent to {recipient_email}")

        except Exception as e:
            logger.error(f"Failed to send email to {recipient_email}: {e}")

    # =========================================================================
    # Convenience methods for common notification types
    # =========================================================================

    @classmethod
    def notify_task_assigned(
        cls,
        task,
        assignee,  # Membership
        assigner,  # Membership
    ):
        """Notify user when a task is assigned to them."""
        return cls.send(
            recipient=assignee,
            notification_type=NotificationType.TASK_ASSIGNED,
            title="Neue Aufgabe zugewiesen",
            message=f'Dir wurde die Aufgabe "{task.title}" zugewiesen.',
            link=f"/work/{task.organization.slug}/tasks/{task.id}/",
            actor=assigner,
            metadata={"task_id": str(task.id)},
        )

    @classmethod
    def notify_task_comment(
        cls,
        task,
        comment,
        commenter,  # Membership
    ):
        """Notify task assignee/creator about a new comment."""
        recipients = set()

        if task.assigned_to and task.assigned_to.id != commenter.id:
            recipients.add(task.assigned_to)
        if task.created_by and task.created_by.id != commenter.id:
            recipients.add(task.created_by)

        return cls.send_bulk(
            recipients=list(recipients),
            notification_type=NotificationType.TASK_COMMENT,
            title="Neuer Kommentar zur Aufgabe",
            message=f'Neuer Kommentar zu "{task.title}": {comment.content[:100]}...',
            link=f"/work/{task.organization.slug}/tasks/{task.id}/",
            actor=commenter,
            metadata={"task_id": str(task.id), "comment_id": str(comment.id)},
        )

    @classmethod
    def notify_meeting_reminder(
        cls,
        meeting,  # OParlMeeting
        recipient,  # Membership
        minutes_before: int = 30,
    ):
        """Send meeting reminder notification."""
        return cls.send(
            recipient=recipient,
            notification_type=NotificationType.MEETING_REMINDER,
            title="Sitzungserinnerung",
            message=f'Die Sitzung "{meeting.get_display_name()}" beginnt in {minutes_before} Minuten.',
            link=f"/insight/termine/{meeting.id}/",
            metadata={
                "meeting_id": str(meeting.id),
                "minutes_before": minutes_before,
            },
        )

    @classmethod
    def notify_motion_shared(
        cls,
        motion,
        share,  # MotionShare instance
        sharer,  # Membership
    ):
        """Notify user when a motion is shared with them."""
        return cls.send(
            recipient=share.shared_with,
            notification_type=NotificationType.MOTION_SHARED,
            title="Antrag mit dir geteilt",
            message=f'Der Antrag "{motion.title}" wurde mit dir geteilt.',
            link=f"/work/{motion.organization.slug}/motions/{motion.id}/",
            actor=sharer,
            metadata={"motion_id": str(motion.id)},
        )

    @classmethod
    def notify_member_joined(
        cls,
        organization,
        new_member,  # Membership
        inviter=None,  # Membership (optional)
    ):
        """Notify organization admins when a new member joins."""
        # Get admin memberships
        admins = organization.memberships.filter(
            is_active=True,
            role__in=["owner", "admin"],
        ).exclude(id=new_member.id)

        if inviter:
            admins = admins.exclude(id=inviter.id)

        return cls.send_bulk(
            recipients=list(admins),
            notification_type=NotificationType.MEMBER_JOINED,
            title="Neues Mitglied",
            message=f'{new_member.user.get_full_name() or new_member.user.email} ist der Organisation beigetreten.',
            link=f"/work/{organization.slug}/organization/members/",
            actor=inviter,
            metadata={"member_id": str(new_member.id)},
        )

    # =========================================================================
    # Support Ticket notifications
    # =========================================================================

    @classmethod
    def notify_support_ticket_created(
        cls,
        ticket,
        creator,  # Membership
    ):
        """
        Notify support staff when a new ticket is created.
        This sends to all users with support.manage permission.
        """
        # For now, just log - staff notifications would go through admin
        logger.info(f"Support ticket created: {ticket.id} by {creator.user.email}")
        return None

    @classmethod
    def notify_support_ticket_reply(
        cls,
        ticket,
        message,
        replier=None,  # Membership or None for staff
        is_staff_reply: bool = False,
    ):
        """
        Notify the ticket creator when staff replies.
        """
        if is_staff_reply and ticket.created_by:
            return cls.send(
                recipient=ticket.created_by,
                notification_type=NotificationType.SUPPORT_TICKET_REPLY,
                title="Antwort auf Ihr Support-Ticket",
                message=f'Ihr Ticket "{ticket.subject}" hat eine neue Antwort erhalten.',
                link=f"/work/{ticket.organization.slug}/support/{ticket.id}/",
                metadata={"ticket_id": str(ticket.id), "message_id": str(message.id)},
            )
        return None

    @classmethod
    def notify_support_ticket_status_change(
        cls,
        ticket,
        old_status: str,
        new_status: str,
    ):
        """
        Notify the ticket creator when status changes.
        """
        if not ticket.created_by:
            return None

        status_labels = dict(ticket.STATUS_CHOICES)
        new_status_label = status_labels.get(new_status, new_status)

        notification_type = NotificationType.SUPPORT_TICKET_STATUS
        title = f"Ticketstatus geändert: {new_status_label}"
        message = f'Ihr Ticket "{ticket.subject}" hat jetzt den Status: {new_status_label}'

        # Special cases for resolved/escalated
        if new_status == "resolved":
            notification_type = NotificationType.SUPPORT_TICKET_RESOLVED
            title = "Ihr Support-Ticket wurde gelöst"
            message = f'Ihr Ticket "{ticket.subject}" wurde als gelöst markiert. Falls das Problem weiterhin besteht, können Sie das Ticket wieder öffnen.'
        elif new_status == "escalated":
            notification_type = NotificationType.SUPPORT_TICKET_ESCALATED
            title = "Ihr Ticket wurde eskaliert"
            message = f'Ihr Ticket "{ticket.subject}" wurde an ein höheres Support-Level weitergeleitet.'

        return cls.send(
            recipient=ticket.created_by,
            notification_type=notification_type,
            title=title,
            message=message,
            link=f"/work/{ticket.organization.slug}/support/{ticket.id}/",
            metadata={
                "ticket_id": str(ticket.id),
                "old_status": old_status,
                "new_status": new_status,
            },
        )

    # =========================================================================
    # Utility methods
    # =========================================================================

    @classmethod
    def _get_count_cache_key(cls, membership) -> str:
        """Generate cache key for notification count."""
        return f"notif_count_{membership.id}"

    @classmethod
    def get_unread_count(cls, membership) -> int:
        """Get count of unread notifications for a user (cached for 30 seconds)."""
        cache_key = cls._get_count_cache_key(membership)
        count = cache.get(cache_key)
        if count is None:
            count = Notification.objects.filter(
                recipient=membership,
                is_read=False,
            ).count()
            cache.set(cache_key, count, 30)  # 30 seconds TTL
        return count

    @classmethod
    def invalidate_count_cache(cls, membership):
        """Invalidate the notification count cache for a user."""
        cache_key = cls._get_count_cache_key(membership)
        cache.delete(cache_key)

    @classmethod
    def mark_all_as_read(cls, membership) -> int:
        """Mark all notifications as read for a user."""
        count = Notification.objects.filter(
            recipient=membership,
            is_read=False,
        ).update(is_read=True, read_at=timezone.now())
        # Invalidate cache after marking as read
        cls.invalidate_count_cache(membership)
        return count

    @classmethod
    def cleanup_old_notifications(cls, days: int = 90) -> int:
        """Delete notifications older than specified days."""
        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = Notification.objects.filter(
            created_at__lt=cutoff,
            is_read=True,
        ).delete()
        return deleted

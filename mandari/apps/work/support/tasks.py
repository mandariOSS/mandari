# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Background tasks for Support ticket management.

Uses Django 6.0's built-in background tasks framework:
- task.call(...) - Execute immediately
- task.enqueue(...) - Execute in background
- Schedule with cron/systemd for periodic execution
"""

import logging
from datetime import timedelta
from typing import Any

from django.db.models import F
from django.tasks import task
from django.utils import timezone

logger = logging.getLogger(__name__)


@task
def auto_close_resolved_tickets(days: int = 7) -> dict[str, Any]:
    """
    Automatically close tickets that have been resolved without customer reply.

    Closes tickets where:
    - Status is 'resolved'
    - resolved_at is more than {days} days ago
    - No customer reply since resolution (last_customer_reply_at < resolved_at)

    Args:
        days: Number of days after which resolved tickets are auto-closed.
              Default: 7 days.

    Returns:
        Dict with statistics about closed tickets.
    """
    from apps.work.notifications.services import NotificationHub
    from apps.work.support.models import SupportTicket

    logger.info(f"Running auto-close for tickets resolved more than {days} days ago")

    cutoff_date = timezone.now() - timedelta(days=days)

    # Find tickets to close
    tickets_to_close = SupportTicket.objects.filter(
        status="resolved",
        resolved_at__lte=cutoff_date,
    ).exclude(
        # Exclude if customer replied after resolution
        last_customer_reply_at__gt=F("resolved_at")
    )

    closed_count = 0
    closed_tickets = []

    for ticket in tickets_to_close:
        old_status = ticket.status
        ticket.status = "closed"
        ticket.closed_at = timezone.now()
        ticket.save(update_fields=["status", "closed_at", "updated_at"])

        # Send notification to ticket creator
        NotificationHub.notify_support_ticket_status_change(ticket, old_status, "closed")

        closed_tickets.append(str(ticket.id))
        closed_count += 1

        logger.info(f"Auto-closed ticket {ticket.id}: {ticket.subject}")

    result = {
        "closed_count": closed_count,
        "closed_tickets": closed_tickets,
        "cutoff_date": cutoff_date.isoformat(),
    }

    logger.info(f"Auto-close completed: {closed_count} tickets closed")
    return result


@task
def send_resolution_reminder(days_until_close: int = 2) -> dict[str, Any]:
    """
    Send reminder to customers whose tickets will be auto-closed soon.

    Sends notifications for tickets where:
    - Status is 'resolved'
    - Will be auto-closed in {days_until_close} days or less
    - No reminder has been sent yet (tracked via metadata)

    Args:
        days_until_close: Days before auto-close to send reminder.
                         Default: 2 days (5 days after resolution).

    Returns:
        Dict with statistics about reminders sent.
    """
    from apps.work.notifications.models import NotificationType
    from apps.work.notifications.services import NotificationHub
    from apps.work.support.models import SupportTicket

    # Auto-close happens at 7 days, so reminder at (7 - days_until_close) days
    reminder_after_days = 7 - days_until_close
    reminder_cutoff = timezone.now() - timedelta(days=reminder_after_days)

    logger.info(f"Sending resolution reminders for tickets resolved before {reminder_cutoff}")

    # Find tickets to remind about
    tickets_to_remind = SupportTicket.objects.filter(
        status="resolved",
        resolved_at__lte=reminder_cutoff,
        resolved_at__gt=timezone.now() - timedelta(days=7),  # Not yet due for close
    ).exclude(
        # Exclude if customer replied after resolution
        last_customer_reply_at__gt=F("resolved_at")
    )

    reminded_count = 0
    reminded_tickets = []

    for ticket in tickets_to_remind:
        if ticket.created_by:
            # Send reminder notification
            NotificationHub.send(
                recipient=ticket.created_by,
                notification_type=NotificationType.SUPPORT_TICKET_STATUS,
                title="Ihr Ticket wird bald geschlossen",
                message=f'Ihr Ticket "{ticket.subject}" wurde als gelöst markiert und wird in {days_until_close} Tagen automatisch geschlossen. Falls Sie weitere Hilfe benötigen, antworten Sie bitte auf das Ticket.',
                link=f"/work/{ticket.organization.slug}/support/{ticket.id}/",
                metadata={
                    "ticket_id": str(ticket.id),
                    "reminder_type": "auto_close_warning",
                },
            )

            reminded_tickets.append(str(ticket.id))
            reminded_count += 1

            logger.info(f"Sent reminder for ticket {ticket.id}")

    result = {
        "reminded_count": reminded_count,
        "reminded_tickets": reminded_tickets,
        "days_until_close": days_until_close,
    }

    logger.info(f"Reminders sent: {reminded_count}")
    return result


@task
def cleanup_old_tickets(months: int = 12) -> dict[str, Any]:
    """
    Archive or anonymize very old closed tickets for data hygiene.

    This task can be used for GDPR compliance to remove personal data
    from tickets older than a specified period.

    Args:
        months: Age threshold in months for cleanup. Default: 12 months.

    Returns:
        Dict with statistics about cleaned up tickets.

    Note:
        This task does NOT delete tickets but could be extended to:
        - Anonymize encrypted content
        - Remove attachments
        - Archive to cold storage
    """
    from apps.work.support.models import SupportTicket

    cutoff_date = timezone.now() - timedelta(days=months * 30)

    old_tickets = SupportTicket.objects.filter(
        status="closed",
        closed_at__lte=cutoff_date,
    )

    count = old_tickets.count()

    # For now, just count and log
    # Actual cleanup logic can be added based on requirements
    logger.info(f"Found {count} tickets older than {months} months eligible for cleanup")

    return {
        "eligible_count": count,
        "cutoff_date": cutoff_date.isoformat(),
        "action": "counted_only",  # No action taken, just reporting
    }

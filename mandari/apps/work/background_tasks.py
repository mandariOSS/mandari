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

try:
    from django.tasks import task
except ImportError:  # Django < 6.0: Tasks laufen synchron

    def task(func):
        return func


from apps.common.email import render_email

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
        html_content, text_content = render_email("work/notifications/email/notification.html", context)
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
        )

        # Mark as sent
        notification.email_sent = True
        from django.utils import timezone

        notification.email_sent_at = timezone.now()
        notification.save(update_fields=["email_sent", "email_sent_at"])

        logger.info(f"Notification email sent to {recipient_email}")

    except Exception as e:
        logger.error(f"Failed to send email to {recipient_email}: {e}")


@task
def generate_dsgvo_export_task(export_id: str):
    """
    Generate a DSGVO data export in the background.

    Collects user data, generates JSON or PDF, and writes to disk.
    """
    import json as json_mod
    from pathlib import Path

    from django.utils import timezone

    from apps.work.organization.models import DataExport

    try:
        export = DataExport.objects.select_related("membership__user", "organization").get(id=export_id)
    except DataExport.DoesNotExist:
        logger.error(f"DataExport {export_id} not found")
        return

    if export.status != "pending":
        logger.info(f"DataExport {export_id} already {export.status}, skipping")
        return

    export.status = "processing"
    export.started_at = timezone.now()
    export.save(update_fields=["status", "started_at"])

    try:
        from apps.work.organization.export_service import dsgvo_export_service

        data = dsgvo_export_service.collect_user_data(
            user=export.membership.user,
            membership=export.membership,
            organization=export.organization,
        )

        if export.export_format == "pdf":
            html_content = render_to_string(
                "work/profile/export/dsgvo_export.html",
                {
                    "data": data,
                    "user": export.membership.user,
                    "organization": export.organization,
                    "export_date": timezone.now(),
                },
            )
            file_bytes = dsgvo_export_service._html_to_pdf(html_content)
            ext = "pdf"
        else:
            content = json_mod.dumps(data, indent=2, ensure_ascii=False, default=str)
            file_bytes = content.encode("utf-8")
            ext = "json"

        # Write file to MEDIA_ROOT/exports/<org_id>/<membership_id>/
        rel_dir = Path("exports") / str(export.organization_id) / str(export.membership_id)
        abs_dir = settings.MEDIA_ROOT / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)

        filename = f"dsgvo-export-{export.id}.{ext}"
        rel_path = rel_dir / filename
        abs_path = abs_dir / filename

        abs_path.write_bytes(file_bytes)

        export.status = "completed"
        export.file_path = str(rel_path)
        export.file_size = len(file_bytes)
        export.completed_at = timezone.now()
        export.save(update_fields=["status", "file_path", "file_size", "completed_at"])

        logger.info(f"DSGVO export {export_id} completed ({export.file_size_human})")

    except Exception as e:
        logger.error(f"DSGVO export {export_id} failed: {e}")
        export.status = "failed"
        # Feste Meldung für die Anzeige; Details stehen im Protokoll
        export.error_message = "Der Export konnte nicht erstellt werden. Bitte später erneut anfordern."
        export.completed_at = timezone.now()
        export.save(update_fields=["status", "error_message", "completed_at"])

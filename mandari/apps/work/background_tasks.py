# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Background tasks for the Work module.

Uses Django 6.0's native background tasks feature.
Tasks are configured via TASKS setting in settings.py.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.tasks import TaskContext, task
from django.template.loader import render_to_string

from apps.common import mail
from apps.common.email import render_email

logger = logging.getLogger(__name__)


@task(queue_name="mail")
def send_notification_email_task(notification_id: str):
    """
    Send email for a notification asynchronously.

    This task is scheduled to run in the background after a notification is created.
    Idempotent: a notification whose email was already sent is skipped (at-least-once delivery).
    Versand über den Mail-Dienst auf dem Weg der Organisation der Empfängerin (eigenes SMTP oder
    mandari-Standard, Issue #528); der Auftrag versendet selbst, statt einen zweiten einzureihen.
    """
    from apps.work.notifications.models import Notification, NotificationPreference

    try:
        notification = Notification.objects.select_related(
            "recipient__user", "recipient__organization", "actor__user"
        ).get(id=notification_id)
    except Notification.DoesNotExist:
        logger.error(f"Notification {notification_id} not found")
        return

    if notification.email_sent:
        logger.info("Notification email %s already sent, skipping", notification_id)
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
        mail.send(
            kind="work.benachrichtigung",
            subject=notification.title,
            body=text_content,
            html_body=html_content,
            to=[recipient_email],
            organization=notification.recipient.organization,
            sofort=True,
        )

        # Mark as sent
        notification.email_sent = True
        from django.utils import timezone

        notification.email_sent_at = timezone.now()
        notification.save(update_fields=["email_sent", "email_sent_at"])

        logger.info("Notification email %s sent", notification_id)

    except Exception:
        # Weiterreichen: Mit TASKS_BACKEND=journal wiederholt der Runner den Versand mit wachsender
        # Wartezeit; das sofort ausführende Backend fängt den Fehler selbst ab (wie bisher kein Abbruch).
        logger.exception("Failed to send notification email %s", notification_id)
        raise


class ExportBusyError(Exception):
    """Ein anderer Versuch hat den Export gerade begonnen; der Runner versucht es später erneut."""


@task(takes_context=True)
def generate_dsgvo_export_task(context: TaskContext[Any, Any], export_id: str):
    """
    Generate a DSGVO data export in the background.

    Collects user data, generates JSON or PDF, and writes to disk.

    Wiederaufnehmbar: Bricht ein Versuch ab (Zeitgrenze, Absturz, Speichergrenze), steht der Export noch
    auf „processing“. Einen weiteren Versuch gibt der Runner erst aus, wenn der vorige beendet ist; ab dem
    zweiten Versuch wird der Export deshalb übernommen, ebenso ein abgebrochener (``DATA_EXPORT_STALE_AFTER``).
    Die Datei wird dabei neu geschrieben.
    """
    import json as json_mod
    from pathlib import Path

    from django.db.models import Q
    from django.utils import timezone

    from apps.work.organization.models import DATA_EXPORT_FAILED_MESSAGE, DATA_EXPORT_STALE_AFTER, DataExport

    try:
        export = DataExport.objects.select_related("membership__user", "organization").get(id=export_id)
    except DataExport.DoesNotExist:
        logger.error(f"DataExport {export_id} not found")
        return

    if export.status not in ("pending", "processing"):
        logger.info(f"DataExport {export_id} already {export.status}, skipping")
        return

    jetzt = timezone.now()
    uebernehmbar = Q(status="pending") | Q(status="processing", started_at__lt=jetzt - DATA_EXPORT_STALE_AFTER)
    uebernehmbar |= Q(status="processing", started_at__isnull=True)
    if context.attempt > 1:
        uebernehmbar |= Q(status="processing")
    # Bedingt und in einem Schritt: Von zwei gleichzeitigen Aufträgen übernimmt nur einer den Export
    if not DataExport.objects.filter(pk=export.pk).filter(uebernehmbar).update(status="processing", started_at=jetzt):
        export.refresh_from_db(fields=["status"])
        if export.status == "processing":
            raise ExportBusyError(export_id)
        logger.info(f"DataExport {export_id} already {export.status}, skipping")
        return
    export.status = "processing"
    export.started_at = jetzt

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

        from apps.work.files import DATA_EXPORTS

        # MEDIA_ROOT/exports/<org_id>/<membership_id>/ – geschütztes Präfix, /media/ liefert es nie aus;
        # heruntergeladen wird nur über work:export_download (Organisation und Mitgliedschaft geprüft)
        rel_dir = Path(DATA_EXPORTS) / str(export.organization_id) / str(export.membership_id)
        abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)

        filename = f"dsgvo-export-{export.id}.{ext}"
        rel_path = rel_dir / filename
        abs_path = abs_dir / filename

        abs_path.write_bytes(file_bytes)

        export.status = "completed"
        export.file_path = rel_path.as_posix()
        export.file_size = len(file_bytes)
        export.completed_at = timezone.now()
        export.save(update_fields=["status", "file_path", "file_size", "completed_at"])

        logger.info(f"DSGVO export {export_id} completed ({export.file_size_human})")

    except Exception as e:
        logger.error(f"DSGVO export {export_id} failed: {e}")
        export.status = "failed"
        # Feste Meldung für die Anzeige; Details stehen im Protokoll
        export.error_message = DATA_EXPORT_FAILED_MESSAGE
        export.completed_at = timezone.now()
        export.save(update_fields=["status", "error_message", "completed_at"])

"""
Management Command: Send Digest Emails

Sends digest emails to subscribers who have unsent alerts.

Run as cronjob: weekly (e.g. Monday 8:00).
Usage: python manage.py send_digest
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Versendet Digest-E-Mails an Insight-Abonnenten mit neuen Benachrichtigungen"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur anzeigen, nicht senden",
        )
        parser.add_argument(
            "--email",
            type=str,
            help="Nur an diese E-Mail-Adresse senden (zum Testen)",
        )

    def handle(self, *args, **options):
        from insight_core.models import DigestLog, InsightSubscriber, SubscriptionAlert

        dry_run = options["dry_run"]
        filter_email = options.get("email")

        if not getattr(settings, "INSIGHT_DIGEST_ENABLED", True):
            self.stdout.write("Digest-Versand ist deaktiviert (INSIGHT_DIGEST_ENABLED=False)")
            return

        max_alerts = getattr(settings, "INSIGHT_DIGEST_MAX_ALERTS_PER_MAIL", 20)
        site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
        from_email = (
            getattr(settings, "INSIGHT_DIGEST_FROM_EMAIL", "")
            or getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@mandari.de")
        )

        # Find subscribers with unsent alerts
        subscribers = InsightSubscriber.objects.filter(
            confirmed=True,
            unsubscribed_at__isnull=True,
        )

        if filter_email:
            subscribers = subscribers.filter(email=filter_email)

        sent_count = 0
        error_count = 0

        for subscriber in subscribers:
            unsent_alerts = (
                SubscriptionAlert.objects.filter(
                    subscriber=subscriber,
                    sent_in_digest__isnull=True,
                )
                .order_by("alert_type", "-created_at")[:max_alerts]
            )

            if not unsent_alerts.exists():
                continue

            alerts_list = list(unsent_alerts)
            alert_count = len(alerts_list)

            # Group alerts by type
            neighborhood_alerts = [a for a in alerts_list if a.alert_type == "neighborhood"]
            keyword_alerts = [a for a in alerts_list if a.alert_type == "keyword"]
            bookmark_alerts = [a for a in alerts_list if a.alert_type == "bookmark"]

            if dry_run:
                self.stdout.write(
                    f"  [DRY RUN] {subscriber.email}: {alert_count} Alerts "
                    f"(N={len(neighborhood_alerts)}, K={len(keyword_alerts)}, B={len(bookmark_alerts)})"
                )
                sent_count += 1
                continue

            # Render email
            html_message = render_to_string("emails/insight_digest.html", {
                "subscriber": subscriber,
                "alert_count": alert_count,
                "neighborhood_alerts": neighborhood_alerts,
                "keyword_alerts": keyword_alerts,
                "bookmark_alerts": bookmark_alerts,
                "site_url": site_url,
            })

            subject = f"Dein Mandari-Digest: {alert_count} neue Treffer ({subscriber.body.get_display_name()})"

            # Send
            try:
                send_mail(
                    subject=subject,
                    message=f"{alert_count} neue Treffer in {subscriber.body.get_display_name()}. "
                            f"Ansehen: {site_url}/insight/",
                    from_email=from_email,
                    recipient_list=[subscriber.email],
                    html_message=html_message,
                    fail_silently=False,
                )

                # Log digest
                digest_log = DigestLog.objects.create(
                    subscriber=subscriber,
                    alert_count=alert_count,
                    success=True,
                )

                # Mark alerts as sent
                SubscriptionAlert.objects.filter(
                    id__in=[a.id for a in alerts_list]
                ).update(sent_in_digest=digest_log)

                sent_count += 1
                self.stdout.write(f"  {subscriber.email}: {alert_count} Alerts gesendet")

            except Exception as e:
                logger.exception(f"Digest send failed for {subscriber.email}: {e}")

                DigestLog.objects.create(
                    subscriber=subscriber,
                    alert_count=alert_count,
                    success=False,
                    error=str(e)[:500],
                )

                error_count += 1

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}{sent_count} Digests versendet, {error_count} Fehler."
        ))

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Send Digest Emails

Sends digest emails to subscribers who have unsent alerts.

Läuft montags 08:00 als Zeitplan befehl:send_digest im Worker, wenn die Abos eingeschaltet sind
(apps/common/schedules.py, Issue #516).
Usage: python manage.py send_digest
"""

import logging
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.common import mail
from apps.common.einmalig import EinmaligMixin
from apps.common.email import render_email

logger = logging.getLogger(__name__)


def _pick_alerts(unsent: Any, limit: int) -> tuple[list[Any], set[Any]]:
    """
    Die ersten ``limit`` Hinweise aus ``unsent`` (sortiert), die hinausgehen dürfen, und die Kennungen derer, die zu
    verwerfen sind.

    Ein Hinweis hält Titel und Adresse des Vorgangs vom Tag seiner Entstehung fest. Geprüft wird gegen den Bestand
    unmittelbar vor dem Versand, gleich auf welchem Weg markiert wurde:

    - Den Vorgang gibt es nicht mehr, oder er ist zurückgenommen: Der Hinweis wird verworfen, sein Titel geht nicht
      mehr hinaus.
    - Der Vorgang ist nur als in der Quelle gelöscht markiert: Der Hinweis wird übersprungen, nicht verworfen. Eine
      solche Markierung kann vorübergehend sein (der Abgleich hebt sie auf, sobald die Quelle den Vorgang wieder
      liefert); ein verworfener Hinweis entstünde dann nicht neu.

    Begrenzt wird die Zahl der gültigen Hinweise, nicht die der geprüften: Stehen vorn lauter verworfene oder
    übersprungene, rücken die dahinter nach, statt bis zum nächsten Lauf zu warten. Je Abschnitt zwei Abfragen.
    """
    from insight_core.models import OParlPaper

    sendable: list[Any] = []
    discard: set[Any] = set()
    offset = 0
    while len(sendable) < limit:
        batch = list(unsent[offset : offset + limit])
        if not batch:
            break
        offset += len(batch)
        paper_ids = {a.entity_id for a in batch if a.entity_type == "paper"}
        papers = (
            {
                p.pk: p
                for p in OParlPaper.objects.filter(pk__in=paper_ids).only("external_id", "deleted", "deletion_reason")
            }
            if paper_ids
            else {}
        )
        for alert in batch:
            if alert.entity_type == "paper":
                paper = papers.get(alert.entity_id)
                if paper is None or paper.withdrawn_by_publisher:
                    discard.add(alert.pk)
                    continue
                if paper.deleted:
                    continue
            if len(sendable) < limit:
                sendable.append(alert)
    return sendable, discard


class Command(EinmaligMixin, BaseCommand):
    sperre = "send_digest"  # Singleton je Cache/Redis, #55
    sperre_ttl = 3600
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

        if not getattr(settings, "INSIGHT_SUBSCRIPTIONS_ENABLED", False):
            self.stdout.write(
                self.style.WARNING(
                    "Insight-Abos sind abgeschaltet (INSIGHT_SUBSCRIPTIONS_ENABLED=False) – keine Digest-Mails versendet."
                )
            )
            return

        if not getattr(settings, "INSIGHT_DIGEST_ENABLED", True):
            self.stdout.write("Digest-Versand ist deaktiviert (INSIGHT_DIGEST_ENABLED=False)")
            return

        max_alerts = getattr(settings, "INSIGHT_DIGEST_MAX_ALERTS_PER_MAIL", 20)
        site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
        from_email = getattr(settings, "INSIGHT_DIGEST_FROM_EMAIL", "") or None

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
            # „id“ als letzter Schlüssel: Die Auswahl liest in Abschnitten und braucht eine feste Reihenfolge
            unsent_alerts = SubscriptionAlert.objects.filter(
                subscriber=subscriber,
                sent_in_digest__isnull=True,
            ).order_by("alert_type", "-created_at", "id")

            alerts_list, veraltet = _pick_alerts(unsent_alerts, max_alerts)
            if veraltet and not dry_run:
                SubscriptionAlert.objects.filter(pk__in=veraltet).delete()
            if not alerts_list:
                continue
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

            # Render email (Inliner + Text-Alternative aus dem HTML)
            html_message, text_message = render_email(
                "emails/insight_digest.html",
                {
                    "subscriber": subscriber,
                    "alert_count": alert_count,
                    "neighborhood_alerts": neighborhood_alerts,
                    "keyword_alerts": keyword_alerts,
                    "bookmark_alerts": bookmark_alerts,
                    "site_url": site_url,
                },
            )

            subject = f"Ihr mandari-Digest: {alert_count} neue Treffer ({subscriber.body.get_display_name()})"

            # Send
            try:
                mail.send(
                    kind="insight.digest",
                    subject=subject,
                    body=text_message,
                    html_body=html_message,
                    from_email=from_email,
                    to=[subscriber.email],
                )

                # Log digest
                digest_log = DigestLog.objects.create(
                    subscriber=subscriber,
                    alert_count=alert_count,
                    success=True,
                )

                # Mark alerts as sent
                SubscriptionAlert.objects.filter(id__in=[a.id for a in alerts_list]).update(sent_in_digest=digest_log)

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
        self.stdout.write(self.style.SUCCESS(f"{prefix}{sent_count} Digests versendet, {error_count} Fehler."))

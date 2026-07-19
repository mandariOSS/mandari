# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Generate Subscription Alerts

Generates SubscriptionAlert entries for active subscribers based on:
1. Neighborhood subscriptions (Haversine proximity query)
2. Keyword subscriptions (Elasticsearch or Django ORM fallback)

Run as cronjob: daily or before digest sending.
Usage: python manage.py generate_alerts
"""

import logging

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Generiert Benachrichtigungen für aktive Insight-Abonnenten"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help="Zeitraum in Tagen für neue Vorgänge (Standard: 7)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur anzeigen, nicht speichern",
        )

    def handle(self, *args, **options):
        from insight_core.models import InsightSubscriber

        days = options["days"]
        dry_run = options["dry_run"]
        cutoff = timezone.now() - timezone.timedelta(days=days)

        subscribers = InsightSubscriber.objects.filter(
            confirmed=True,
            unsubscribed_at__isnull=True,
        )

        total_alerts = 0
        self.stdout.write(f"Prüfe {subscribers.count()} aktive Abonnenten (Zeitraum: {days} Tage)...")

        for subscriber in subscribers:
            count = 0

            if subscriber.neighborhood_active and subscriber.neighborhood_lat and subscriber.neighborhood_lon:
                count += self._generate_neighborhood_alerts(subscriber, cutoff, dry_run)

            if subscriber.keyword_active and subscriber.keyword:
                count += self._generate_keyword_alerts(subscriber, cutoff, dry_run)

            if count > 0:
                self.stdout.write(f"  {subscriber.email}: {count} neue Alerts")
                total_alerts += count

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{prefix}{total_alerts} Alerts generiert."))

    def _generate_neighborhood_alerts(self, subscriber, cutoff, dry_run):
        """Generates alerts for papers near the subscriber's location."""
        from insight_core.models import SubscriptionAlert

        lat = float(subscriber.neighborhood_lat)
        lon = float(subscriber.neighborhood_lon)
        radius = subscriber.neighborhood_radius

        sql = """
            SELECT DISTINCT ON (p.id) p.id, p.name, p.reference,
                   d.dist AS distance
            FROM oparl_papers p,
                 LATERAL jsonb_array_elements(p.locations) AS loc,
                 LATERAL (
                     SELECT 6371000 * acos(
                         LEAST(1.0, GREATEST(-1.0,
                             cos(radians(%s)) * cos(radians((loc->>'lat')::float))
                             * cos(radians((loc->>'lon')::float) - radians(%s))
                             + sin(radians(%s)) * sin(radians((loc->>'lat')::float))
                         ))
                     ) AS dist
                 ) d
            WHERE p.body_id = %s
              AND p.deleted = FALSE
              AND p.locations IS NOT NULL
              AND jsonb_array_length(p.locations) > 0
              AND p.created_at >= %s
              AND d.dist <= %s
            ORDER BY p.id, d.dist
            LIMIT 50
        """

        count = 0
        with connection.cursor() as cursor:
            cursor.execute(sql, [lat, lon, lat, str(subscriber.body_id), cutoff, radius])
            for row in cursor.fetchall():
                paper_id, name, reference, distance = row

                if dry_run:
                    count += 1
                    continue

                _, created = SubscriptionAlert.objects.get_or_create(
                    subscriber=subscriber,
                    entity_type="paper",
                    entity_id=paper_id,
                    defaults={
                        "alert_type": "neighborhood",
                        "entity_title": name or reference or "Vorgang",
                        "entity_url": f"/insight/vorgaenge/{paper_id}/",
                        "context": {"distance": int(distance)},
                    },
                )
                if created:
                    count += 1

        return count

    def _generate_keyword_alerts(self, subscriber, cutoff, dry_run):
        """Generates alerts for papers matching the subscriber's keyword."""
        from insight_core.models import OParlPaper, SubscriptionAlert

        keyword = subscriber.keyword
        count = 0

        # Try Elasticsearch first, fall back to Django ORM
        try:
            from insight_core.services.search_service import INDEX_PAPERS, get_search_service

            search_service = get_search_service()
            result = search_service.search_all(
                query=keyword,
                body_id=str(subscriber.body_id),
                page=1,
                page_size=20,
                index_names=[INDEX_PAPERS],
            )

            for hit in result.get("results", []):
                paper_id = hit.get("id")
                if not paper_id:
                    continue

                if dry_run:
                    count += 1
                    continue

                _, created = SubscriptionAlert.objects.get_or_create(
                    subscriber=subscriber,
                    entity_type="paper",
                    entity_id=paper_id,
                    defaults={
                        "alert_type": "keyword",
                        "entity_title": hit.get("title", "Vorgang"),
                        "entity_url": f"/insight/vorgaenge/{paper_id}/",
                        "context": {"keyword": keyword},
                    },
                )
                if created:
                    count += 1

        except Exception as e:
            logger.warning(f"Elasticsearch unavailable for keyword alerts, falling back to ORM: {e}")

            from django.db.models import Q

            papers = OParlPaper.objects.filter(
                body=subscriber.body,
                created_at__gte=cutoff,
                deleted=False,
            ).filter(Q(name__icontains=keyword) | Q(reference__icontains=keyword))[:20]

            for paper in papers:
                if dry_run:
                    count += 1
                    continue

                _, created = SubscriptionAlert.objects.get_or_create(
                    subscriber=subscriber,
                    entity_type="paper",
                    entity_id=paper.id,
                    defaults={
                        "alert_type": "keyword",
                        "entity_title": paper.name or paper.reference or "Vorgang",
                        "entity_url": f"/insight/vorgaenge/{paper.id}/",
                        "context": {"keyword": keyword},
                    },
                )
                if created:
                    count += 1

        return count

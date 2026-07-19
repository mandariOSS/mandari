# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Offizielle OParl-Locations in paper.locations übernehmen.

Der Ingestor verknüpft das OParl-Feld `paper.location` als M2M
(OParlPaper.oparl_locations). Dieses Command übernimmt daraus Koordinaten
in das `locations`-JSON (source="oparl", höchste Priorität) — idempotent,
manuelle und extrahierte Einträge bleiben erhalten.

Verwendung:
    python manage.py backfill_oparl_locations
    python manage.py backfill_oparl_locations --body muenster --limit 500
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from insight_core.models import OParlBody, OParlPaper
from insight_core.services.oparl_locations import apply_oparl_locations


class Command(BaseCommand):
    help = "Übernimmt offizielle OParl-Locations (paper.location) in paper.locations."

    def add_arguments(self, parser):
        parser.add_argument("--body", type=str, default=None, help="UUID oder Slug der Kommune")
        parser.add_argument("--limit", type=int, default=0, help="Max. Anzahl Papers (0 = alle)")
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen, nichts speichern")

    def handle(self, *args, **options):
        queryset = OParlPaper.objects.filter(oparl_locations__isnull=False).distinct()

        if options["body"]:
            body_id = options["body"]
            try:
                body = OParlBody.objects.get(Q(id=body_id) | Q(slug=body_id))
            except (OParlBody.DoesNotExist, ValueError, ValidationError):
                body = OParlBody.objects.filter(slug=body_id).first()
                if not body:
                    raise CommandError(f"Kommune mit ID/Slug '{body_id}' nicht gefunden.")
            queryset = queryset.filter(body=body)
            self.stdout.write(f"Kommune: {body.name}")

        queryset = queryset.prefetch_related("oparl_locations").order_by("-date")
        if options["limit"] > 0:
            queryset = queryset[: options["limit"]]

        total = queryset.count()
        self.stdout.write(f"Papers mit OParl-Locations: {total}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry-Run: nichts gespeichert."))
            return

        changed = 0
        for paper in queryset.iterator(chunk_size=200):
            if apply_oparl_locations(paper):
                changed += 1

        self.stdout.write(self.style.SUCCESS(f"Aktualisiert: {changed} von {total} Papers."))

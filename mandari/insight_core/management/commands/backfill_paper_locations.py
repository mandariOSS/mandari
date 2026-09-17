# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Tabelle ``PaperLocation`` aus ``paper.locations`` (JSON) aufbauen.

Einmalig nach der Migration 0033 bzw. zur Reparatur: Für jeden Vorgang mit
Verortungen wird das JSON in die indexierbare Tabelle gespiegelt (idempotent).
Bestätigte und entfernte Zeilen bleiben erhalten; entfernte Punkte fallen dabei
auch aus dem JSON heraus (Issue #54).

Verwendung:
    python manage.py backfill_paper_locations
    python manage.py backfill_paper_locations --body muenster --limit 500
    python manage.py backfill_paper_locations --dry-run
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Q, QuerySet

from insight_core.models import OParlBody, OParlPaper
from insight_core.services.paper_locations import sync_paper_locations


class Command(BaseCommand):
    help = "Spiegelt paper.locations (JSON) in die Tabelle PaperLocation (Umkreissuche, Admin-Korrektur)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--body", type=str, default=None, help="UUID oder Slug der Kommune")
        parser.add_argument("--limit", type=int, default=0, help="Max. Anzahl Vorgänge (0 = alle)")
        parser.add_argument("--dry-run", action="store_true", help="Nur zählen, nichts speichern")

    def handle(self, *args: Any, **options: Any) -> None:
        queryset: QuerySet[OParlPaper] = OParlPaper.objects.filter(locations__isnull=False).order_by("-date")

        if options["body"]:
            body = self._resolve_body(options["body"])
            queryset = queryset.filter(body=body)
            self.stdout.write(f"Kommune: {body.name}")

        if options["limit"] > 0:
            queryset = queryset[: options["limit"]]

        total = queryset.count()
        self.stdout.write(f"Vorgänge mit Verortungen: {total}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry-Run: nichts gespeichert."))
            return

        created = updated = deleted = suppressed = 0
        changed_papers = 0
        for paper in queryset.iterator(chunk_size=200):
            result = sync_paper_locations(paper)
            created += result.created
            updated += result.updated
            deleted += result.deleted
            suppressed += result.suppressed
            if result.changed:
                changed_papers += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Fertig: {changed_papers} von {total} Vorgängen geändert — "
                f"{created} Verortungen neu, {updated} aktualisiert, {deleted} gelöscht, {suppressed} gesperrt."
            )
        )

    @staticmethod
    def _resolve_body(identifier: str) -> OParlBody:
        try:
            body: OParlBody = OParlBody.objects.get(Q(id=identifier) | Q(slug=identifier))
            return body
        except (OParlBody.DoesNotExist, ValueError, ValidationError):
            fallback: OParlBody | None = OParlBody.objects.filter(slug=identifier).first()
            if fallback is None:
                raise CommandError(f"Kommune mit ID/Slug '{identifier}' nicht gefunden.") from None
            return fallback

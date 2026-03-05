"""
Management Command: Ortsreferenzen aus Papers extrahieren und geocodieren.

Befüllt das `locations` JSONField auf OParlPaper mit Koordinaten,
die aus dem Text der zugehörigen Dateien extrahiert werden.

Verwendung:
    python manage.py extract_locations                       # Regex-Pass (default)
    python manage.py extract_locations --mode ai             # KI-Pass
    python manage.py extract_locations --mode all            # Beide Passes
    python manage.py extract_locations --body <uuid>         # Nur eine Kommune
    python manage.py extract_locations --limit 100           # Max 100 Papers
    python manage.py extract_locations --verbose             # Detaillierte Ausgabe
    python manage.py extract_locations --reprocess           # Auch bereits verarbeitete
    python manage.py extract_locations --dry-run             # Nur zählen
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef, Q

from insight_core.models import OParlBody, OParlFile, OParlPaper
from insight_core.services.georeferencing import (
    process_paper_georef,
    update_paper_georef,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Extrahiert Ortsreferenzen aus Papers und geocodiert sie."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            type=str,
            default="regex",
            choices=["regex", "ai", "all"],
            help="Extraktionsmodus: regex (schnell), ai (LLM), all (beide). Standard: regex",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Maximale Anzahl zu verarbeitender Papers (0 = unbegrenzt)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=20,
            help="Anzahl Papers pro Batch (Standard: 20)",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=2,
            help="Anzahl paralleler Worker (Standard: 2, niedrig wegen Geocoding-API)",
        )
        parser.add_argument(
            "--body",
            type=str,
            default=None,
            help="UUID der Kommune (nur Papers dieser Kommune verarbeiten)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Detaillierte Ausgabe",
        )
        parser.add_argument(
            "--reprocess",
            action="store_true",
            help="Auch bereits verarbeitete Papers neu extrahieren",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur zählen, keine Extraktion durchführen",
        )

    def handle(self, *args, **options):
        mode = options["mode"]
        limit = options["limit"]
        batch_size = options["batch_size"]
        workers = options["workers"]
        body_id = options["body"]
        verbose = options["verbose"]
        reprocess = options["reprocess"]
        dry_run = options["dry_run"]

        self.stdout.write(f"Modus: {mode}")

        # Build queryset
        queryset = OParlPaper.objects.select_related("body")

        # Filter: Only papers with at least one file that has extracted text
        has_text = OParlFile.objects.filter(
            paper=OuterRef("pk"),
            text_extraction_status="completed",
            text_content__isnull=False,
        ).exclude(text_content="")
        queryset = queryset.filter(Exists(has_text))

        # Filter by georef_status based on mode
        if not reprocess:
            if mode == "regex":
                queryset = queryset.filter(georef_status="pending")
            elif mode == "ai":
                queryset = queryset.filter(georef_status="ai_needed")
            elif mode == "all":
                queryset = queryset.filter(georef_status__in=["pending", "ai_needed"])

        # Filter: Specific body
        if body_id:
            try:
                body = OParlBody.objects.get(Q(id=body_id) | Q(slug=body_id))
                queryset = queryset.filter(body=body)
                self.stdout.write(f"Kommune: {body.name}")
            except OParlBody.DoesNotExist:
                raise CommandError(f"Kommune mit ID/Slug '{body_id}' nicht gefunden.")

        # Order by date (newest first)
        queryset = queryset.order_by("-date", "-oparl_created")

        # Apply limit
        if limit > 0:
            queryset = queryset[:limit]

        total = queryset.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS("Keine Papers zu verarbeiten."))
            return

        self.stdout.write(f"Gefunden: {total} Papers zur Verarbeitung")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry-Run: Keine Extraktion durchgeführt."))
            if verbose:
                for paper in queryset[:20]:
                    self.stdout.write(
                        f"  {paper.reference or paper.id}: {paper.name or '(ohne Name)'} [{paper.georef_status}]"
                    )
                if total > 20:
                    self.stdout.write(f"  ... und {total - 20} weitere")
            return

        # Statistics
        stats = {
            "completed": 0,
            "ai_needed": 0,
            "no_locations": 0,
            "skipped": 0,
            "failed": 0,
            "total_locations": 0,
        }

        # Batch processing
        papers = list(queryset)
        for batch_start in range(0, len(papers), batch_size):
            batch = papers[batch_start : batch_start + batch_size]
            batch_num = (batch_start // batch_size) + 1
            total_batches = (len(papers) + batch_size - 1) // batch_size

            self.stdout.write(f"\nBatch {batch_num}/{total_batches} ({len(batch)} Papers)...")

            # Parallel processing (limited workers for API rate limiting)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(self._process_paper, p, mode, verbose): p for p in batch}

                for future in as_completed(futures):
                    paper = futures[future]
                    try:
                        result = future.result()
                        status = result.get("status", "failed")

                        if status == "completed":
                            stats["completed"] += 1
                            locs = result.get("locations", [])
                            stats["total_locations"] += len(locs)
                        elif status == "ai_needed":
                            stats["ai_needed"] += 1
                        elif status == "no_locations":
                            stats["no_locations"] += 1
                        elif status == "skipped":
                            stats["skipped"] += 1
                        else:
                            stats["failed"] += 1

                    except Exception as exc:
                        stats["failed"] += 1
                        if verbose:
                            self.stdout.write(self.style.ERROR(f"  {paper.id}: Fehler - {exc}"))

        # Summary
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.SUCCESS(f"Georeferenziert: {stats['completed']} Papers ({stats['total_locations']} Orte)")
        )
        if stats["ai_needed"]:
            self.stdout.write(self.style.WARNING(f"KI-Extraktion benötigt: {stats['ai_needed']} Papers"))
        if stats["no_locations"]:
            self.stdout.write(f"Keine Ortsbezüge: {stats['no_locations']}")
        if stats["skipped"]:
            self.stdout.write(self.style.WARNING(f"Übersprungen: {stats['skipped']}"))
        if stats["failed"]:
            self.stdout.write(self.style.ERROR(f"Fehlgeschlagen: {stats['failed']}"))

    def _process_paper(self, paper, mode: str, verbose: bool) -> dict:
        """Process a single paper for georeferencing."""
        try:
            # Mark as processing
            paper.georef_status = "processing"
            paper.save(update_fields=["georef_status", "updated_at"])

            # Run pipeline
            result = process_paper_georef(paper, mode=mode)

            # Update paper
            update_paper_georef(paper, result)

            if verbose:
                status = result["status"]
                locs = result.get("locations", [])
                method = result.get("method", "")
                ref = paper.reference or str(paper.id)[:8]

                if status == "completed":
                    loc_names = [loc.get("name", "?") for loc in locs[:3]]
                    self.stdout.write(
                        self.style.SUCCESS(f"  {ref}: {len(locs)} Orte ({method}) [{', '.join(loc_names)}]")
                    )
                elif status == "ai_needed":
                    self.stdout.write(f"  {ref}: KI-Extraktion benötigt")
                elif status == "no_locations":
                    self.stdout.write(f"  {ref}: Keine Ortsbezüge")
                elif status == "skipped":
                    self.stdout.write(self.style.WARNING(f"  {ref}: Übersprungen ({result.get('reason', '')})"))

            return result

        except Exception as exc:
            # Mark as failed
            paper.georef_status = "failed"
            paper.georef_error = str(exc)[:500]
            paper.save(update_fields=["georef_status", "georef_error", "updated_at"])

            if verbose:
                self.stdout.write(self.style.ERROR(f"  {paper.id}: {exc}"))
            return {"status": "failed", "error": str(exc)}

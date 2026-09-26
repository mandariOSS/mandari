# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management Command: Text aus OParl-Dateien extrahieren.

Extrahiert Text aus PDFs und anderen Dokumenten und speichert
ihn im text_content Feld der OParlFile-Objekte.

Verwendung:
    python manage.py extract_texts                    # Alle ohne text_content
    python manage.py extract_texts --limit 100       # Max 100 Dateien
    python manage.py extract_texts --batch-size 10   # 10 pro Batch
    python manage.py extract_texts --body <uuid>     # Nur für eine Kommune
    python manage.py extract_texts --verbose         # Detaillierte Ausgabe
    python manage.py extract_texts --reprocess       # Auch bereits verarbeitete
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from apps.common.db_connections import releases_db_connections
from insight_core.management.arguments import add_extraction_arguments
from insight_core.models import OParlBody, OParlFile
from insight_core.services.document_extraction import (
    DocumentDownloadError,
    download_and_extract,
)
from insight_core.services.file_cache import download_headers

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Extrahiert Text aus OParl-Dateien (PDFs) mittels pypdf und OCR."

    def add_arguments(self, parser):
        add_extraction_arguments(parser, noun="Dateien", batch_size=50, workers=4)
        parser.add_argument(
            "--pdf-only",
            action="store_true",
            help="Nur PDF-Dateien verarbeiten",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        batch_size = options["batch_size"]
        workers = options["workers"]
        body_id = options["body"]
        verbose = options["verbose"]
        reprocess = options["reprocess"]
        dry_run = options["dry_run"]
        pdf_only = options["pdf_only"]

        # Query aufbauen (Tombstones: keine Extraktion für gelöschte Dateien)
        queryset = OParlFile.objects.filter(deleted=False).select_related("paper", "paper__body")

        # Filter: Nur Dateien mit Download-URL
        queryset = queryset.filter(Q(download_url__isnull=False) | Q(access_url__isnull=False))

        # Filter: Nur pending/unverarbeitete Dateien (außer bei --reprocess)
        if not reprocess:
            queryset = queryset.filter(Q(text_extraction_status="pending") | Q(text_extraction_status__isnull=True))

        # Filter: Nur bestimmte Kommune
        if body_id:
            try:
                body = OParlBody.objects.get(id=body_id)
                queryset = queryset.filter(paper__body=body)
                self.stdout.write(f"Verarbeite nur Dateien für: {body.name}")
            except OParlBody.DoesNotExist:
                raise CommandError(f"Kommune mit ID {body_id} nicht gefunden.") from None

        # Filter: Nur PDFs
        if pdf_only:
            queryset = queryset.filter(Q(mime_type__icontains="pdf") | Q(file_name__iendswith=".pdf"))

        # Limit anwenden
        if limit > 0:
            queryset = queryset[:limit]

        total = queryset.count()

        if total == 0:
            self.stdout.write(self.style.SUCCESS("Keine Dateien zu verarbeiten."))
            return

        self.stdout.write(f"Gefunden: {total} Dateien zur Verarbeitung")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry-Run: Keine Extraktion durchgeführt."))
            return

        # Statistiken
        stats = {
            "success": 0,
            "failed": 0,
            "ocr": 0,
            "skipped": 0,
            "total_chars": 0,
        }

        # Batch-Verarbeitung: vorab nur die IDs, die Dateien je Stapel (Speicher, siehe extract_locations)
        file_ids = list(queryset.values_list("id", flat=True))
        for batch_start in range(0, len(file_ids), batch_size):
            batch = list(
                OParlFile.objects.select_related("paper", "paper__body").filter(
                    id__in=file_ids[batch_start : batch_start + batch_size]
                )
            )
            batch_num = (batch_start // batch_size) + 1
            total_batches = (len(file_ids) + batch_size - 1) // batch_size

            self.stdout.write(f"\nBatch {batch_num}/{total_batches} ({len(batch)} Dateien)...")

            # Parallele Verarbeitung
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(self._process_file, f, verbose): f for f in batch}

                for future in as_completed(futures):
                    file = futures[future]
                    try:
                        result = future.result()
                        if result["success"]:
                            stats["success"] += 1
                            stats["total_chars"] += result.get("chars", 0)
                            if result.get("ocr"):
                                stats["ocr"] += 1
                        elif result.get("skipped"):
                            stats["skipped"] += 1
                        else:
                            stats["failed"] += 1
                    except Exception as exc:
                        stats["failed"] += 1
                        if verbose:
                            self.stdout.write(self.style.ERROR(f"Fehler bei {file.id}: {exc}"))

        # Zusammenfassung
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(self.style.SUCCESS(f"Erfolgreich: {stats['success']}"))
        self.stdout.write(f"  davon OCR: {stats['ocr']}")
        self.stdout.write(f"  Zeichen gesamt: {stats['total_chars']:,}")
        if stats["skipped"]:
            self.stdout.write(self.style.WARNING(f"Übersprungen: {stats['skipped']}"))
        if stats["failed"]:
            self.stdout.write(self.style.ERROR(f"Fehlgeschlagen: {stats['failed']}"))

    # Läuft in Worker-Threads, die je Stapel neu entstehen – ohne Rückgabe leert sich der Pool
    # nach wenigen Stapeln (siehe extract_locations, Issue #54).
    @releases_db_connections
    def _process_file(self, file: OParlFile, verbose: bool) -> dict:
        """
        Verarbeitet eine einzelne Datei.

        Returns:
            Dict mit Ergebnis-Informationen
        """
        url = file.download_url or file.access_url
        if not url:
            return {"success": False, "skipped": True, "reason": "Keine URL"}

        try:
            result = download_and_extract(
                url=url,
                mime_type=file.mime_type,
                original_name=file.file_name or file.name or "",
                timeout=120.0,
                extra_headers=download_headers(file.body),
            )

            # Text speichern
            if result.text:
                file.text_content = result.text
                file.text_extraction_status = "completed"
                file.text_extraction_method = "ocr" if result.ocr_performed else "pypdf"
                file.save(
                    update_fields=["text_content", "text_extraction_status", "text_extraction_method", "updated_at"]
                )

                if verbose:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  {file.id}: {len(result.text)} Zeichen{' (OCR)' if result.ocr_performed else ''}"
                        )
                    )

                return {
                    "success": True,
                    "chars": len(result.text),
                    "ocr": result.ocr_performed,
                    "pages": result.page_count,
                }
            file.text_extraction_status = "ocr_needed"
            file.text_extraction_error = "Download ok, aber kein Text extrahierbar (KI-OCR benötigt)"
            file.save(update_fields=["text_extraction_status", "text_extraction_error", "updated_at"])
            if verbose:
                self.stdout.write(self.style.WARNING(f"  {file.id}: KI-OCR benötigt (kein Text via pypdf/Tesseract)"))
            return {"success": False, "reason": "ocr_needed"}

        except DocumentDownloadError as exc:
            file.text_extraction_status = "failed"
            file.text_extraction_error = str(exc)[:500]
            file.save(update_fields=["text_extraction_status", "text_extraction_error", "updated_at"])
            if verbose:
                self.stdout.write(self.style.ERROR(f"  {file.id}: Download-Fehler - {exc}"))
            return {"success": False, "reason": str(exc)}

        except Exception as exc:
            file.text_extraction_status = "failed"
            file.text_extraction_error = str(exc)[:500]
            file.save(update_fields=["text_extraction_status", "text_extraction_error", "updated_at"])
            if verbose:
                self.stdout.write(self.style.ERROR(f"  {file.id}: Fehler - {exc}"))
            return {"success": False, "reason": str(exc)}

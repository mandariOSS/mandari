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
from django.utils import timezone

from insight_core.models import OParlFile, OParlBody
from insight_core.services.document_extraction import (
    download_and_extract,
    DocumentDownloadError,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Extrahiert Text aus OParl-Dateien (PDFs) mittels PyPDF2 und OCR."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Maximale Anzahl zu verarbeitender Dateien (0 = unbegrenzt)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="Anzahl Dateien pro Batch (Standard: 50)",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=4,
            help="Anzahl paralleler Worker (Standard: 4)",
        )
        parser.add_argument(
            "--body",
            type=str,
            default=None,
            help="UUID der Kommune (nur Dateien dieser Kommune verarbeiten)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Detaillierte Ausgabe",
        )
        parser.add_argument(
            "--reprocess",
            action="store_true",
            help="Auch bereits verarbeitete Dateien neu extrahieren",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Nur zählen, keine Extraktion durchführen",
        )
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

        # Query aufbauen
        queryset = OParlFile.objects.select_related("paper", "paper__body")

        # Filter: Nur Dateien mit Download-URL
        queryset = queryset.filter(
            Q(download_url__isnull=False) | Q(access_url__isnull=False)
        )

        # Filter: Ohne text_content (außer bei --reprocess)
        if not reprocess:
            queryset = queryset.filter(
                Q(text_content__isnull=True) | Q(text_content="")
            )

        # Filter: Nur bestimmte Kommune
        if body_id:
            try:
                body = OParlBody.objects.get(id=body_id)
                queryset = queryset.filter(paper__body=body)
                self.stdout.write(f"Verarbeite nur Dateien für: {body.name}")
            except OParlBody.DoesNotExist:
                raise CommandError(f"Kommune mit ID {body_id} nicht gefunden.")

        # Filter: Nur PDFs
        if pdf_only:
            queryset = queryset.filter(
                Q(mime_type__icontains="pdf") | Q(file_name__iendswith=".pdf")
            )

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

        # Batch-Verarbeitung
        files = list(queryset)
        for batch_start in range(0, len(files), batch_size):
            batch = files[batch_start:batch_start + batch_size]
            batch_num = (batch_start // batch_size) + 1
            total_batches = (len(files) + batch_size - 1) // batch_size

            self.stdout.write(
                f"\nBatch {batch_num}/{total_batches} ({len(batch)} Dateien)..."
            )

            # Parallele Verarbeitung
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(self._process_file, f, verbose): f
                    for f in batch
                }

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
                            self.stdout.write(
                                self.style.ERROR(f"Fehler bei {file.id}: {exc}")
                            )

        # Zusammenfassung
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(self.style.SUCCESS(f"Erfolgreich: {stats['success']}"))
        self.stdout.write(f"  davon OCR: {stats['ocr']}")
        self.stdout.write(f"  Zeichen gesamt: {stats['total_chars']:,}")
        if stats["skipped"]:
            self.stdout.write(self.style.WARNING(f"Übersprungen: {stats['skipped']}"))
        if stats["failed"]:
            self.stdout.write(self.style.ERROR(f"Fehlgeschlagen: {stats['failed']}"))

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
            )

            # Text speichern
            if result.text:
                file.text_content = result.text
                file.save(update_fields=["text_content", "updated_at"])

                if verbose:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  {file.id}: {len(result.text)} Zeichen"
                            f"{' (OCR)' if result.ocr_performed else ''}"
                        )
                    )

                return {
                    "success": True,
                    "chars": len(result.text),
                    "ocr": result.ocr_performed,
                    "pages": result.page_count,
                }
            else:
                if verbose:
                    self.stdout.write(
                        self.style.WARNING(f"  {file.id}: Kein Text extrahiert")
                    )
                return {"success": False, "reason": "Kein Text extrahiert"}

        except DocumentDownloadError as exc:
            if verbose:
                self.stdout.write(
                    self.style.ERROR(f"  {file.id}: Download-Fehler - {exc}")
                )
            return {"success": False, "reason": str(exc)}

        except Exception as exc:
            if verbose:
                self.stdout.write(
                    self.style.ERROR(f"  {file.id}: Fehler - {exc}")
                )
            return {"success": False, "reason": str(exc)}

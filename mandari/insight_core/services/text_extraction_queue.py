"""
Text Extraction Queue Service.

Verwaltet die asynchrone Textextraktion für OParlFile-Objekte.
Kann sowohl synchron als auch via Django Tasks ausgeführt werden.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from ..models import OParlFile

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Ergebnis einer Textextraktion."""

    file_id: UUID
    success: bool
    method: str
    text_length: int = 0
    page_count: int | None = None
    error: str | None = None
    duration_ms: int = 0


class TextExtractionQueue:
    """
    Queue-basierte Textextraktion für OParlFile-Objekte.

    Verarbeitet Dateien in der Reihenfolge ihrer Erstellung und
    speichert Ergebnisse direkt im OParlFile-Modell.
    """

    # Maximale Dateigröße für Extraktion (in Bytes)
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

    # Unterstützte MIME-Types
    SUPPORTED_MIME_TYPES = {
        "application/pdf",
        "application/x-pdf",
        "text/plain",
        "text/html",
    }

    def __init__(self):
        """Initialisiert den Queue-Service."""
        self.enabled = getattr(settings, "TEXT_EXTRACTION_ENABLED", True)
        self.async_mode = getattr(settings, "TEXT_EXTRACTION_ASYNC", True)
        self.max_size = getattr(settings, "TEXT_EXTRACTION_MAX_SIZE_MB", 50) * 1024 * 1024

    def queue_extraction(self, file: OParlFile) -> bool:
        """
        Markiert eine Datei für Extraktion.

        Args:
            file: OParlFile Objekt

        Returns:
            True wenn Datei für Extraktion vorgemerkt wurde
        """
        if not self.enabled:
            logger.debug("Textextraktion deaktiviert")
            return False

        # Prüfen ob Extraktion sinnvoll ist
        if not self._should_extract(file):
            file.text_extraction_status = "skipped"
            file.save(update_fields=["text_extraction_status"])
            return False

        # Status auf pending setzen
        file.text_extraction_status = "pending"
        file.text_extraction_error = None
        file.save(update_fields=["text_extraction_status", "text_extraction_error"])

        if self.async_mode:
            # Task für späteren Prozess einreihen
            # Der Task-Worker wird die Datei später verarbeiten
            logger.info(f"Datei {file.id} für Textextraktion eingereiht")
        else:
            # Synchrone Extraktion
            self.process_file(file)

        return True

    def _should_extract(self, file: OParlFile) -> bool:
        """
        Prüft ob eine Datei extrahiert werden sollte.

        Args:
            file: OParlFile Objekt

        Returns:
            True wenn Extraktion sinnvoll
        """
        # Bereits extrahiert?
        if file.text_content and file.text_extraction_status == "completed":
            return False

        # MIME-Type unterstützt?
        mime = file.mime_type or ""
        if not any(mime.startswith(t.split("/")[0]) for t in self.SUPPORTED_MIME_TYPES):
            if mime not in self.SUPPORTED_MIME_TYPES:
                logger.debug(f"MIME-Type nicht unterstützt: {mime}")
                return False

        # Dateigröße prüfen
        if file.size and file.size > self.max_size:
            logger.debug(f"Datei zu groß: {file.size} > {self.max_size}")
            return False

        # Download-URL vorhanden?
        if not (file.download_url or file.access_url):
            logger.debug("Keine Download-URL vorhanden")
            return False

        return True

    def process_file(self, file: OParlFile) -> ExtractionResult:
        """
        Verarbeitet eine einzelne Datei.

        Args:
            file: OParlFile Objekt

        Returns:
            ExtractionResult mit Ergebnis
        """
        import time

        from .document_extraction import download_and_extract

        start_time = time.time()
        result = ExtractionResult(
            file_id=file.id,
            success=False,
            method="none",
        )

        try:
            # Status aktualisieren
            file.text_extraction_status = "processing"
            file.save(update_fields=["text_extraction_status"])

            # Download und Extraktion
            url = file.download_url or file.access_url
            doc = download_and_extract(
                url=url,
                mime_type=file.mime_type,
                original_name=file.file_name or "",
            )

            # Ergebnis speichern
            file.text_content = doc.text
            file.sha256_hash = doc.checksum
            file.page_count = doc.page_count
            file.text_extracted_at = timezone.now()

            # Methode aus ExtractedDocument übernehmen
            file.text_extraction_method = doc.extraction_method

            file.text_extraction_status = "completed" if doc.text else "skipped"
            file.text_extraction_error = None

            file.save(
                update_fields=[
                    "text_content",
                    "sha256_hash",
                    "page_count",
                    "text_extracted_at",
                    "text_extraction_method",
                    "text_extraction_status",
                    "text_extraction_error",
                ]
            )

            result.success = True
            result.method = file.text_extraction_method
            result.text_length = len(doc.text)
            result.page_count = doc.page_count

            logger.info(f"Textextraktion erfolgreich: {file.id} ({result.text_length} Zeichen via {result.method})")

        except Exception as e:
            logger.exception(f"Textextraktion fehlgeschlagen für {file.id}: {e}")

            file.text_extraction_status = "failed"
            file.text_extraction_error = str(e)[:1000]  # Kürzen
            file.save(
                update_fields=[
                    "text_extraction_status",
                    "text_extraction_error",
                ]
            )

            result.error = str(e)

        result.duration_ms = int((time.time() - start_time) * 1000)
        return result

    def process_pending(
        self,
        batch_size: int = 50,
        body_id: UUID | None = None,
    ) -> list[ExtractionResult]:
        """
        Verarbeitet ausstehende Dateien.

        Args:
            batch_size: Maximale Anzahl zu verarbeitender Dateien
            body_id: Optional: Nur Dateien dieser Kommune

        Returns:
            Liste mit Ergebnissen
        """
        # Ausstehende Dateien laden
        qs = OParlFile.objects.filter(
            text_extraction_status="pending",
        ).exclude(Q(download_url__isnull=True) & Q(access_url__isnull=True))

        if body_id:
            qs = qs.filter(body_id=body_id)

        # Älteste zuerst
        files = list(qs.order_by("created_at")[:batch_size])

        if not files:
            logger.info("Keine ausstehenden Dateien für Extraktion")
            return []

        logger.info(f"Verarbeite {len(files)} Dateien für Textextraktion")

        results = []
        for file in files:
            result = self.process_file(file)
            results.append(result)

        # Statistik
        successful = sum(1 for r in results if r.success)
        failed = len(results) - successful
        total_chars = sum(r.text_length for r in results if r.success)

        logger.info(
            f"Textextraktion Batch abgeschlossen: "
            f"{successful} erfolgreich, {failed} fehlgeschlagen, "
            f"{total_chars} Zeichen extrahiert"
        )

        return results

    def get_stats(self, body_id: UUID | None = None) -> dict:
        """
        Gibt Statistiken zur Textextraktion zurück.

        Args:
            body_id: Optional: Nur für diese Kommune

        Returns:
            Dict mit Statistiken
        """
        qs = OParlFile.objects.all()
        if body_id:
            qs = qs.filter(body_id=body_id)

        return {
            "total": qs.count(),
            "pending": qs.filter(text_extraction_status="pending").count(),
            "processing": qs.filter(text_extraction_status="processing").count(),
            "completed": qs.filter(text_extraction_status="completed").count(),
            "failed": qs.filter(text_extraction_status="failed").count(),
            "skipped": qs.filter(text_extraction_status="skipped").count(),
            "with_text": qs.exclude(text_content__isnull=True).exclude(text_content="").count(),
        }

    def reset_failed(self, body_id: UUID | None = None) -> int:
        """
        Setzt fehlgeschlagene Extraktionen zurück auf pending.

        Args:
            body_id: Optional: Nur für diese Kommune

        Returns:
            Anzahl zurückgesetzter Dateien
        """
        qs = OParlFile.objects.filter(text_extraction_status="failed")
        if body_id:
            qs = qs.filter(body_id=body_id)

        count = qs.update(
            text_extraction_status="pending",
            text_extraction_error=None,
        )

        logger.info(f"{count} fehlgeschlagene Extraktionen zurückgesetzt")
        return count


# Singleton-Instanz
_extraction_queue: TextExtractionQueue | None = None


def get_extraction_queue() -> TextExtractionQueue:
    """Gibt die Singleton-Instanz der Extraction Queue zurück."""
    global _extraction_queue
    if _extraction_queue is None:
        _extraction_queue = TextExtractionQueue()
    return _extraction_queue

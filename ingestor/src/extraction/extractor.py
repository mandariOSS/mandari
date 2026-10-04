"""
Text Extraction Pipeline for the Ingestor.

Downloads PDF files and extracts text using a fallback chain:
1. pypdf (fast, text-based PDFs)
2. Mistral OCR (optional, mit API-Schlüssel)
3. Tesseract OCR (lokal, Seite für Seite mit Speicher- und Zeitgrenzen, ``src.extraction.ocr``)

Async-capable: PDF downloads via httpx, sync extraction via asyncio.to_thread().

Downloads laufen gestreamt in eine temporäre Datei, die beim Schreiben gehasht wird; die Größengrenze
greift schon während des Downloads, nie liegt eine ganze Datei im Arbeitsspeicher (Issue #788). Ist die
Dokumentablage eingerichtet (``OPARL_FILES_ROOT``), legt der Ingestor die Datei danach gleich unter
ihrem SHA-256 ab – ein Abruf je Datei für Text und Ablage.

Abbrüche (Issue #817): Jede Datei zählt ihre begonnenen Bearbeitungen. Stirbt der Worker mitten in einer
Datei, stellt die nächste Runde sie nach ``TEXT_EXTRACTION_STALE_MINUTES`` zurück; Dateien mit einem
Abbruch laufen danach einzeln, nach ``TEXT_EXTRACTION_MAX_ATTEMPTS`` Abbrüchen gelten sie als gescheitert
(„Speichergrenze“) statt den Worker immer wieder zu beenden.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from mandari_oparl.robots import KIND_FILES, RETRY_UNREACHABLE_SECONDS

from src.client.host_pacing import host_pacer
from src.client.robots import robots_gate
from src.client.source_options import SourceFetchOptions
from src.config import settings
from src.extraction.ocr import MEMORY_LIMIT_REASON, OcrLimits, OcrMemoryLimitError, ocr_pdf

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

#: So lange gelten Einstellungen einer Quelle (Download-Header, robots-Ausnahme) im Zwischenspeicher je Body.
#: Der Extraktions-Worker lebt lange; eine neue Ausnahme oder ein neuer Header wirkt spätestens danach.
SOURCE_OPTIONS_TTL_SECONDS = 300.0

PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
TEXT_MIME_TYPES = {"text/plain", "text/html"}
SUPPORTED_MIME_TYPES = PDF_MIME_TYPES | TEXT_MIME_TYPES
#: Textdateien werden höchstens bis zu dieser Größe gelesen
MAX_TEXT_BYTES = 20 * 1024 * 1024
#: Abgebrochene Bearbeitungen höchstens so oft je Minute auflösen (eine Abfrage über alle Kommunen)
RELEASE_STALE_EVERY_SECONDS = 60.0


class FileTooLargeError(Exception):
    """Die Datei überschreitet ``TEXT_EXTRACTION_MAX_SIZE_MB`` (Abbruch während des Downloads)."""


@dataclass
class DownloadedFile:
    """Gestreamt geladene Datei in einer temporären Datei."""

    path: Path
    size: int
    sha256: str
    head: bytes

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


def blob_root() -> Path | None:
    """Wurzel der Ablage nach SHA-256 oder ``None``, wenn der Ingestor nichts ablegt."""
    root = (settings.oparl_files_root or "").strip()
    if not root or (settings.file_store_layout or "sha256").strip().lower() != "sha256":
        return None
    path = Path(root)
    return path / "sha256" if path.is_dir() else None


def looks_like_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head[:200]


class TextExtractor:
    """
    Async text extraction pipeline for OParl files.

    Downloads files and extracts text content, then updates the database
    with the results.
    """

    def __init__(self, storage, clock: Callable[[], float] = time.monotonic) -> None:
        """
        Args:
            storage: DatabaseStorage instance for querying/updating files.
            clock: Uhr für Zwischenspeicher und Zurückstellen (Tests)
        """
        self.storage = storage
        self.max_size_bytes = settings.text_extraction_max_size_mb * 1024 * 1024
        self.concurrency = settings.text_extraction_concurrency
        self.timeout = settings.text_extraction_timeout
        self.batch_size = settings.text_extraction_batch_size
        self._clock = clock
        # Je Body: (Wert, geladen um); gültig SOURCE_OPTIONS_TTL_SECONDS
        self._header_cache: dict[Any, tuple[dict[str, str], float]] = {}
        self._options_cache: dict[Any, tuple[SourceFetchOptions, float]] = {}
        # Bodies, deren robots.txt nicht erreichbar war: bis zu diesem Zeitpunkt nichts beanspruchen
        self._deferred_until: dict[Any, float] = {}
        # Letztes Auflösen abgebrochener Bearbeitungen (Issue #817)
        self._released_at: float | None = None

    async def release_stale(self) -> tuple[int, int]:
        """
        Abgebrochene Bearbeitungen (Worker beendet) zurückstellen bzw. nach zu vielen Abbrüchen aufgeben,
        höchstens alle ``RELEASE_STALE_EVERY_SECONDS``. Rückgabe: (zurückgestellt, aufgegeben).
        """
        release = getattr(self.storage, "release_stale_extractions", None)
        now = self._clock()
        if release is None or (self._released_at is not None and now - self._released_at < RELEASE_STALE_EVERY_SECONDS):
            return 0, 0
        self._released_at = now
        try:
            zurueck, aufgegeben = await release(
                stale_after=timedelta(minutes=settings.text_extraction_stale_minutes),
                max_attempts=settings.text_extraction_max_attempts,
            )
        except Exception as e:  # noqa: BLE001 - die Extraktion läuft auch ohne Auflösen weiter
            logger.warning("Abgebrochene Textextraktionen nicht auflösbar: %s", e)
            return 0, 0
        if zurueck:
            logger.warning("Textextraktion: %d abgebrochene Dateien zurückgestellt (Worker beendet)", zurueck)
        if aufgegeben:
            logger.error(
                "Textextraktion: %d Dateien nach %d Abbrüchen aufgegeben (%s)",
                aufgegeben,
                settings.text_extraction_max_attempts,
                MEMORY_LIMIT_REASON,
            )
        return zurueck, aufgegeben

    async def extract_pending_files(self, body_id: UUID) -> int:
        """
        Download and extract text from all pending files for a body.

        Args:
            body_id: The body UUID to process files for.

        Returns:
            Number of files successfully extracted.
        """
        # Abgebrochene Bearbeitungen aller Kommunen auflösen (höchstens einmal je Minute, Issue #817)
        await self.release_stale()
        # Quelle liefert Dokumente nur hinter einer Zugangsprüfung für Menschen (sync_config["file_downloads"]):
        # nichts beanspruchen, die Dateien bleiben "pending" und werden nachgeholt, sobald der Schalter fällt
        if not await self._file_downloads_enabled(body_id):
            logger.debug("Dateiabruf für Body %s abgeschaltet (sync_config der Quelle)", body_id)
            return 0
        # Die robots.txt der Quelle war eben nicht erreichbar: Dateien bleiben "pending", bis der neue Versuch
        # fällig ist, statt sie in jeder Runde zu beanspruchen und wieder zurückzustellen
        deferred = self._deferred_until.get(body_id)
        if deferred is not None:
            if self._clock() < deferred:
                logger.debug("Body %s zurückgestellt (robots.txt nicht erreichbar)", body_id)
                return 0
            del self._deferred_until[body_id]

        files = await self.storage.get_pending_files(
            body_id=body_id,
            batch_size=self.batch_size,
            max_size_bytes=self.max_size_bytes,
        )

        if not files:
            logger.debug("No pending files for body %s", body_id)
            return 0

        logger.info("Extracting text from %d pending files", len(files))

        semaphore = asyncio.Semaphore(self.concurrency)
        extracted = 0

        async def process_one(file_row):
            nonlocal extracted
            async with semaphore:
                success = await self._process_file(file_row)
                if success:
                    extracted += 1

        # Dateien, deren Bearbeitung schon einmal abbrach, laufen zuletzt und einzeln: Stirbt der Worker
        # wieder, zählt der Abbruch nur bei der Datei, die ihn auslöst, nicht bei einer zufällig parallelen.
        suspects = [f for f in files if (getattr(f, "text_extraction_attempts", 0) or 0) > 0]
        regular = [f for f in files if (getattr(f, "text_extraction_attempts", 0) or 0) <= 0]
        await asyncio.gather(
            *(process_one(f) for f in regular),
            return_exceptions=True,
        )
        for file_row in suspects:
            try:
                if await self._process_file(file_row):
                    extracted += 1
            except Exception as e:  # noqa: BLE001 - wie gather(return_exceptions=True)
                logger.warning("Textextraktion für Datei %s fehlgeschlagen: %s", file_row.id, e)

        logger.info("Extracted text from %d/%d files", extracted, len(files))
        return extracted

    async def _process_file(self, file_row) -> bool:
        """Download a single file and extract its text."""
        file_id = file_row.id
        download_url = file_row.download_url or file_row.access_url
        mime_type = file_row.mime_type or ""
        file_name = file_row.file_name or ""

        if not download_url:
            await self.storage.update_file_text(
                file_id=file_id,
                status="skipped",
                error="No download URL",
            )
            return False

        # Check MIME type support
        # Allow unknown MIME types (try anyway), but skip known unsupported
        if mime_type and mime_type not in SUPPORTED_MIME_TYPES and mime_type.startswith(("image/", "video/", "audio/")):
            await self.storage.update_file_text(
                file_id=file_id,
                status="skipped",
                error=f"Unsupported MIME type: {mime_type}",
            )
            return False

        # robots.txt (RFC 9309) gilt auch für Dateien; viele Systeme sperren nur Dokumente (Disallow: /*.pdf$).
        # Gesperrte Dateien werden übersprungen; nach einer Freigabe holt sie robots_override (Django) zurück.
        # Ist die robots.txt nicht erreichbar, bleibt die Datei "pending" und kommt später wieder dran.
        # Geprüft wird mit dem User-Agent, mit dem die Datei auch geladen wird (Download-Header der Quelle).
        headers = await self._download_headers(file_row.body_id)
        options = await self._fetch_options(file_row.body_id)
        interval = settings.request_interval if options.request_interval is None else options.request_interval

        async def pace(url: str) -> None:
            await host_pacer.wait(url, interval)

        decision = await robots_gate.decide(
            None,
            download_url,
            user_agent=_user_agent_of(headers),
            kind=KIND_FILES,
            override=options.robots_override,
            pace=pace,
        )
        if decision.unreachable:
            logger.info("Datei %s zurückgestellt: %s", file_id, decision.reason)
            if file_row.body_id is not None:
                self._deferred_until[file_row.body_id] = self._clock() + RETRY_UNREACHABLE_SECONDS
            await self.storage.update_file_text(file_id=file_id, status="pending")
            return False
        if not decision.allowed:
            logger.info("Datei %s nicht abgerufen: %s", file_id, decision.reason)
            await self.storage.update_file_text(file_id=file_id, status="skipped", error=decision.reason)
            return False

        # Ab hier kann die Bearbeitung den Worker beenden (Speicher): Versuch zählen (Issue #817)
        if not await self._mark_started(file_id):
            return False

        try:
            # Drossel je Host: Dateien zählen wie jede andere Anfrage an die Quelle (mit Grenze je Host)
            async with host_pacer.limit(download_url, interval):
                await pace(download_url)
                downloaded = await self._download_to_file(download_url, headers)
        except FileTooLargeError:
            await self.storage.update_file_text(
                file_id=file_id,
                status="skipped",
                error=f"File too large: > {self.max_size_bytes} bytes",
            )
            return False
        except Exception as e:
            logger.warning("Download failed for %s: %s", download_url, e)
            await self.storage.update_file_text(
                file_id=file_id,
                status="failed",
                error=f"Download failed: {e}",
            )
            return False

        try:
            return await self._extract_and_store(file_row, downloaded, mime_type, file_name)
        finally:
            downloaded.discard()

    async def _extract_and_store(
        self, file_row: Any, downloaded: DownloadedFile, mime_type: str, file_name: str
    ) -> bool:
        """Text aus der geladenen Datei erkennen, Ergebnis speichern und die Datei in der Ablage ablegen."""
        file_id = file_row.id
        sha256_hash = downloaded.sha256

        # Detect MIME type from content if not set
        if not mime_type and downloaded.head[:5] == b"%PDF-":
            mime_type = "application/pdf"

        # Extract text
        try:
            text, page_count, method = await asyncio.to_thread(
                self._extract_text, downloaded.path, mime_type, file_name
            )
        except OcrMemoryLimitError as e:
            logger.warning("Texterkennung an der Speichergrenze für %s: %s", file_name or file_id, e)
            await self.storage.update_file_text(
                file_id=file_id,
                status="failed",
                error=str(e),
                sha256_hash=sha256_hash,
            )
            await self._store(file_row, downloaded, mime_type)
            return False
        except Exception as e:
            logger.warning("Extraction failed for %s: %s", file_name or file_id, e)
            await self.storage.update_file_text(
                file_id=file_id,
                status="failed",
                error=f"Extraction failed: {e}",
                sha256_hash=sha256_hash,
            )
            await self._store(file_row, downloaded, mime_type)
            return False
        await self._store(file_row, downloaded, mime_type)

        if text.strip():
            await self.storage.update_file_text(
                file_id=file_id,
                text_content=text.strip(),
                method=method,
                status="completed",
                page_count=page_count,
                sha256_hash=sha256_hash,
            )
            return True
        await self.storage.update_file_text(
            file_id=file_id,
            method=method or "none",
            status="completed",
            page_count=page_count,
            sha256_hash=sha256_hash,
        )
        return False

    async def _mark_started(self, file_id: Any) -> bool:
        """Begonnene Bearbeitung zählen; ``False``, wenn die Datei nicht mehr zu bearbeiten ist."""
        mark = getattr(self.storage, "mark_extraction_started", None)
        if mark is None:
            return True
        try:
            attempts = await mark(file_id)
        except Exception as e:  # noqa: BLE001 - ohne Zähler läuft die Bearbeitung wie bisher
            logger.warning("Bearbeitungsbeginn für Datei %s nicht gespeichert: %s", file_id, e)
            return True
        if attempts is None:
            logger.info("Datei %s nicht mehr in Bearbeitung, übersprungen", file_id)
            return False
        if attempts > 1:
            logger.warning("Datei %s: Versuch %d nach Abbruch, läuft einzeln", file_id, attempts)
        return True

    async def _file_downloads_enabled(self, body_id: Any) -> bool:
        """Schalter ``sync_config["file_downloads"]`` der Quelle; im Zweifel (Fehler, alter Storage) an."""
        lookup = getattr(self.storage, "file_downloads_enabled_for_body", None)
        if lookup is None or body_id is None:
            return True
        try:
            return bool(await lookup(body_id))
        except Exception as e:  # noqa: BLE001 - der Schalter ist optional, Extraktion läuft wie bisher weiter
            logger.warning("Dateiabruf-Schalter für Body %s nicht ladbar: %s", body_id, e)
            return True

    async def _download_headers(self, body_id: Any) -> dict[str, str]:
        """
        Zusätzliche Download-Header je Quelle (``sync_config["download_headers"]``, Issue #116):
        manche RIS liefern Anlagen nur mit Referer oder Sitzungs-Cookie aus. Ergebnis je Body
        ``SOURCE_OPTIONS_TTL_SECONDS`` zwischengespeichert; ohne Body oder Konfiguration leer.
        """
        if body_id is None:
            return {}
        cached = self._header_cache.get(body_id)
        now = self._clock()
        if cached is not None and now - cached[1] < SOURCE_OPTIONS_TTL_SECONDS:
            return cached[0]
        headers: dict[str, str]
        try:
            headers = await self.storage.get_download_headers_for_body(body_id)
        except Exception as e:  # noqa: BLE001 - Header sind optional, Download läuft ohne weiter
            logger.warning("Download-Header für Body %s nicht ladbar: %s", body_id, e)
            headers = {}
        self._header_cache[body_id] = (headers, now)
        return headers

    async def _fetch_options(self, body_id: Any) -> SourceFetchOptions:
        """
        Abrufoptionen der Quelle eines Bodies (Abstand, robots-Ausnahme), je Body ``SOURCE_OPTIONS_TTL_SECONDS``
        zwischengespeichert: Eine neu gesetzte Ausnahme wirkt im laufenden Extraktions-Worker spätestens danach,
        ohne Neustart.
        """
        if body_id is None:
            return SourceFetchOptions()
        cached = self._options_cache.get(body_id)
        now = self._clock()
        if cached is not None and now - cached[1] < SOURCE_OPTIONS_TTL_SECONDS:
            return cached[0]
        lookup = getattr(self.storage, "get_fetch_options_for_body", None)
        options: SourceFetchOptions
        try:
            options = await lookup(body_id) if lookup is not None else SourceFetchOptions()
        except Exception as e:  # noqa: BLE001 - ohne lesbare Optionen gelten die Standards
            logger.warning("Abrufoptionen für Body %s nicht ladbar: %s", body_id, e)
            options = SourceFetchOptions()
        self._options_cache[body_id] = (options, now)
        return options

    async def _download_to_file(self, url: str, extra_headers: dict[str, str] | None = None) -> DownloadedFile:
        """
        Datei gestreamt in eine temporäre Datei laden und dabei hashen.

        Bricht ab, sobald die Größengrenze überschritten ist (auch bei falscher ``Content-Length``). Mit
        eingerichteter Ablage liegt die temporäre Datei im selben Dateisystem wie die Ablage.
        """
        headers = {
            **{key: value for key, value in (extra_headers or {}).items() if key.lower() != "user-agent"},
            "User-Agent": _user_agent_of(extra_headers),
        }
        root = blob_root()
        directory: Path | None = None
        if root is not None:
            directory = root / "tmp"
            directory.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(suffix=".part", dir=directory)
        path = Path(name)
        digest = hashlib.sha256()
        size = 0
        head = b""
        try:
            with os.fdopen(handle, "wb") as target:
                async with (
                    httpx.AsyncClient(timeout=self.timeout) as client,
                    client.stream("GET", url, headers=headers, follow_redirects=True) as response,
                ):
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self.max_size_bytes:
                        raise FileTooLargeError
                    async for chunk in response.aiter_bytes(256 * 1024):
                        size += len(chunk)
                        if size > self.max_size_bytes:
                            raise FileTooLargeError
                        digest.update(chunk)
                        target.write(chunk)
                        if len(head) < 512:
                            head += chunk[: 512 - len(head)]
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return DownloadedFile(path=path, size=size, sha256=digest.hexdigest(), head=head)

    async def _store(self, file_row: Any, downloaded: DownloadedFile, mime_type: str) -> bool:
        """
        Geladene Datei in der Ablage nach SHA-256 ablegen (nur gelistete Kommunen, genug Platz).

        Hinweis- oder Prüfseiten statt der Datei (HTML bei erwarteter PDF) und leere Antworten werden nicht
        abgelegt. Fehler beim Ablegen verhindern die Texterkennung nie.
        """
        root = blob_root()
        attach = getattr(self.storage, "attach_file_blob", None)
        if root is None or attach is None or not downloaded.size or file_row.body_id is None:
            return False
        if looks_like_html(downloaded.head) and "html" not in (mime_type or "").lower():
            return False
        try:
            free = shutil.disk_usage(root.parent).free
            if free - downloaded.size < settings.file_cache_min_free_gb * 1024**3:
                logger.info("Ablage: zu wenig freier Speicher, Datei %s nicht abgelegt", file_row.id)
                return False
            if not await self.storage.body_stores_files(file_row.body_id):
                return False
            target = root / downloaded.sha256[:2] / downloaded.sha256
            if downloaded.path.parent != root / "tmp":
                return False
            stored = bool(await attach(file_row.id, downloaded.sha256, downloaded.size, downloaded.path, target))
        except Exception as e:  # noqa: BLE001 - Ablage ist optional, der Text zählt
            logger.warning("Datei %s nicht abgelegt: %s", file_row.id, e)
            return False
        return stored

    @staticmethod
    def _extract_text(source: Path, mime_type: str, file_name: str) -> tuple[str, int | None, str]:
        """
        Extract text from a downloaded file. Runs in a thread (sync).

        Returns:
            (text, page_count, extraction_method)
        """
        resolved_mime = mime_type or ""

        # PDF extraction
        if resolved_mime in PDF_MIME_TYPES or file_name.lower().endswith(".pdf"):
            return _extract_text_from_pdf(source, file_name)

        with open(source, "rb") as handle:
            data = handle.read(MAX_TEXT_BYTES)

        # Plain text / HTML
        if resolved_mime.startswith("text/"):
            text = _extract_text_from_plain(data)
            if resolved_mime == "text/html":
                text = _strip_html_tags(text)
            return text, None, "text"

        # Try as text fallback
        text = _extract_text_from_plain(data)
        if text.strip():
            return text, None, "text"

        return "", None, "none"


def _user_agent_of(headers: dict[str, str] | None) -> str:
    """User-Agent für Datei-Abrufe: aus den Download-Headern der Quelle (gleich welche Schreibweise), sonst Standard."""
    for key, value in (headers or {}).items():
        if key.lower() == "user-agent" and str(value).strip():
            return str(value).strip()
    return settings.user_agent


def _extract_text_from_pdf(source: Path, file_name: str = "") -> tuple[str, int | None, str]:
    """
    Extract text from a PDF file using the fallback chain: pypdf -> Mistral -> Tesseract.

    Liest aus der Datei; pypdf und pdftoppm laden nur, was sie brauchen. pypdf liefert dabei die Seitengrößen,
    aus denen die Texterkennung die Auflösung je Seite bestimmt (Issue #817).

    Returns:
        (text, page_count, extraction_method)
    """
    page_count = None
    page_sizes: list[tuple[float, float] | None] | None = None

    # 1. Try pypdf (fast, for text-based PDFs)
    if PdfReader is not None:
        try:
            reader = PdfReader(source)
            page_count = len(reader.pages)

            text_fragments: list[str] = []
            page_sizes = []
            for page in reader.pages:
                try:
                    page_text = page.extract_text() or ""
                except Exception:
                    page_text = ""
                text_fragments.append(page_text.strip())
                if len(page_sizes) < settings.ocr_max_pages:
                    page_sizes.append(_page_size(page))

            text = "\n\n".join(f for f in text_fragments if f)
            del reader, text_fragments

            if text.strip():
                logger.debug("pypdf extraction ok: %d chars", len(text))
                return text, page_count, "pypdf"

        except Exception as exc:
            logger.warning("pypdf extraction failed: %s", exc)

    # 2. Mistral OCR (API, optional) — schneller als lokales Tesseract
    if settings.mistral_api_key:
        try:
            text = _extract_text_with_mistral(source.read_bytes(), file_name)
            if text.strip():
                logger.debug("Mistral OCR ok: %d chars", len(text))
                return text, page_count, "mistral"
        except Exception as exc:
            logger.warning("Mistral OCR failed for %s, falling back to Tesseract: %s", file_name, exc)

    # 3. Tesseract OCR (lokal, Seite für Seite mit Grenzen); OcrMemoryLimitError geht an den Aufrufer
    result = ocr_pdf(source, page_count=page_count, page_sizes=page_sizes, limits=ocr_limits())
    if result.notes:
        logger.warning("OCR %s: %s", file_name or source.name, "; ".join(result.notes))
    if result.text.strip():
        logger.debug("Tesseract OCR ok: %d chars", len(result.text))
        return result.text, page_count, "tesseract"

    logger.warning("No text extracted from PDF")
    return "", page_count, "none"


def _page_size(page: Any) -> tuple[float, float] | None:
    """Breite und Höhe einer Seite in PDF-Punkten (MediaBox wie pdftoppm, mit UserUnit); ``None`` bei Fehlern."""
    try:
        box = page.mediabox
        unit = float(getattr(page, "user_unit", 1) or 1)
        return abs(float(box.width)) * unit, abs(float(box.height)) * unit
    except Exception:  # noqa: BLE001 - ohne Größe skaliert die Texterkennung über die lange Seite
        return None


def ocr_limits() -> OcrLimits:
    """Grenzen der Texterkennung aus den Einstellungen (``OCR_*``)."""
    return OcrLimits(
        dpi=settings.ocr_dpi,
        max_pixels=int(settings.ocr_max_megapixels * 1_000_000),
        memory_limit_mb=settings.ocr_memory_limit_mb,
        page_timeout=settings.ocr_page_timeout,
        file_budget=settings.ocr_file_budget_seconds,
        max_pages=settings.ocr_max_pages,
    )


def _extract_text_with_mistral(data: bytes, file_name: str = "") -> str:
    """
    OCR über die Mistral-API (synchron, läuft im Extraktions-Thread).

    Nutzt dasselbe Request-Format wie die Django-Seite
    (insight_core/services/mistral_ocr.py), damit sich beide Pfade
    identisch verhalten.
    """
    import base64

    import httpx

    from src.config import settings

    pdf_base64 = base64.b64encode(data).decode("utf-8")
    payload = {
        "model": settings.mistral_ocr_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extrahiere den vollständigen Text aus diesem PDF-Dokument. "
                            "Gib nur den extrahierten Text zurück, ohne Kommentare oder Formatierung. "
                            "Behalte Absätze und Strukturierung bei. "
                            "Falls das Dokument auf Deutsch ist, behalte die deutsche Sprache bei."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:application/pdf;base64,{pdf_base64}"},
                    },
                ],
            }
        ],
        "max_tokens": 32000,
    }

    response = httpx.post(
        "https://api.mistral.ai/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
        timeout=settings.text_extraction_timeout,
    )
    response.raise_for_status()
    result = response.json()
    return (result.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()


def _extract_text_from_plain(data: bytes) -> str:
    """Decode text files as UTF-8 with latin-1 fallback."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="ignore")


def _strip_html_tags(html: str) -> str:
    """Minimal HTML tag stripping without external dependencies."""
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.fragments: list[str] = []

        def handle_data(self, data: str) -> None:
            cleaned = data.strip()
            if cleaned:
                self.fragments.append(cleaned)

    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.fragments)

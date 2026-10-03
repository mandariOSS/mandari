"""
Text Extraction Pipeline for the Ingestor.

Downloads PDF files and extracts text using a fallback chain:
1. pypdf (fast, text-based PDFs)
2. Tesseract OCR (local, scanned PDFs)
3. AI OCR (placeholder for future Mistral integration)

Async-capable: PDF downloads via httpx, sync extraction via asyncio.to_thread().
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from io import BytesIO
from typing import Any
from uuid import UUID

import httpx
from mandari_oparl.robots import KIND_FILES, RETRY_UNREACHABLE_SECONDS

from src.client.host_pacing import host_pacer
from src.client.robots import robots_gate
from src.client.source_options import SourceFetchOptions
from src.config import settings

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore[assignment, misc]

try:
    from pdf2image import convert_from_bytes
    from pdf2image.exceptions import PDFInfoNotInstalledError
except ImportError:
    convert_from_bytes = None  # type: ignore[assignment, misc]
    PDFInfoNotInstalledError = None  # type: ignore[assignment, misc]

try:
    import pytesseract
except ImportError:
    pytesseract = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

#: So lange gelten Einstellungen einer Quelle (Download-Header, robots-Ausnahme) im Zwischenspeicher je Body.
#: Der Extraktions-Worker lebt lange; eine neue Ausnahme oder ein neuer Header wirkt spätestens danach.
SOURCE_OPTIONS_TTL_SECONDS = 300.0

PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
TEXT_MIME_TYPES = {"text/plain", "text/html"}
SUPPORTED_MIME_TYPES = PDF_MIME_TYPES | TEXT_MIME_TYPES


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

    async def extract_pending_files(self, body_id: UUID) -> int:
        """
        Download and extract text from all pending files for a body.

        Args:
            body_id: The body UUID to process files for.

        Returns:
            Number of files successfully extracted.
        """
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

        await asyncio.gather(
            *(process_one(f) for f in files),
            return_exceptions=True,
        )

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

        try:
            # Drossel je Host: Dateien zählen wie jede andere Anfrage an die Quelle
            await pace(download_url)
            data = await self._download(download_url, headers)
        except Exception as e:
            logger.warning("Download failed for %s: %s", download_url, e)
            await self.storage.update_file_text(
                file_id=file_id,
                status="failed",
                error=f"Download failed: {e}",
            )
            return False

        # Size check after download
        if len(data) > self.max_size_bytes:
            await self.storage.update_file_text(
                file_id=file_id,
                status="skipped",
                error=f"File too large: {len(data)} bytes",
            )
            return False

        # Calculate hash
        sha256_hash = hashlib.sha256(data).hexdigest()

        # Detect MIME type from content if not set
        if not mime_type and data[:5] == b"%PDF-":
            mime_type = "application/pdf"

        # Extract text
        try:
            text, page_count, method = await asyncio.to_thread(self._extract_text, data, mime_type, file_name)
        except Exception as e:
            logger.warning("Extraction failed for %s: %s", file_name or file_id, e)
            await self.storage.update_file_text(
                file_id=file_id,
                status="failed",
                error=f"Extraction failed: {e}",
                sha256_hash=sha256_hash,
            )
            return False

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

    async def _download(self, url: str, extra_headers: dict[str, str] | None = None) -> bytes:
        """Download a file via httpx async (User-Agent aus den Download-Headern der Quelle, sonst Standard)."""
        headers = {
            **{key: value for key, value in (extra_headers or {}).items() if key.lower() != "user-agent"},
            "User-Agent": _user_agent_of(extra_headers),
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            return response.content

    @staticmethod
    def _extract_text(data: bytes, mime_type: str, file_name: str) -> tuple[str, int | None, str]:
        """
        Extract text from file data. Runs in a thread (sync).

        Returns:
            (text, page_count, extraction_method)
        """
        resolved_mime = mime_type or ""

        # PDF extraction
        if resolved_mime in PDF_MIME_TYPES or file_name.lower().endswith(".pdf"):
            return _extract_text_from_pdf(data, file_name)

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


def _extract_text_from_pdf(data: bytes, file_name: str = "") -> tuple[str, int | None, str]:
    """
    Extract text from a PDF using the fallback chain: pypdf -> Tesseract.

    Returns:
        (text, page_count, extraction_method)
    """
    page_count = None

    # 1. Try pypdf (fast, for text-based PDFs)
    if PdfReader is not None:
        try:
            reader = PdfReader(BytesIO(data))
            page_count = len(reader.pages)

            text_fragments: list[str] = []
            for page in reader.pages:
                try:
                    page_text = page.extract_text() or ""
                except Exception:
                    page_text = ""
                text_fragments.append(page_text.strip())

            text = "\n\n".join(f for f in text_fragments if f)

            if text.strip():
                logger.debug("pypdf extraction ok: %d chars", len(text))
                return text, page_count, "pypdf"

        except Exception as exc:
            logger.warning("pypdf extraction failed: %s", exc)

    # 2. Mistral OCR (API, optional) — schneller als lokales Tesseract
    from src.config import settings as _settings

    if _settings.mistral_api_key:
        try:
            text = _extract_text_with_mistral(data, file_name)
            if text.strip():
                logger.debug("Mistral OCR ok: %d chars", len(text))
                return text, page_count, "mistral"
        except Exception as exc:
            logger.warning("Mistral OCR failed for %s, falling back to Tesseract: %s", file_name, exc)

    # 3. Tesseract OCR (local)
    text, success = _extract_text_with_ocr(data, page_count=page_count)
    if success and text.strip():
        logger.debug("Tesseract OCR ok: %d chars", len(text))
        return text, page_count, "tesseract"

    logger.warning("No text extracted from PDF")
    return "", page_count, "none"


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


OCR_MAX_PAGES = 100  # Schutz vor Extremfaellen (Anlagenbaende etc.)
OCR_DPI = 200  # 200 dpi reicht Tesseract; 300 dpi verdoppelt den Speicher


def _extract_text_with_ocr(data: bytes, page_count: int | None = None) -> tuple[str, bool]:
    """
    Extract text via Tesseract OCR — seitenweise.

    convert_from_bytes ohne first_page/last_page rendert ALLE Seiten
    gleichzeitig in den RAM (~26 MB pro A4-Seite bei 300 dpi) — ein
    50-Seiten-Scan sprengt damit jedes Container-Limit (OOM-Kill).
    Deshalb: eine Seite rendern, OCRen, freigeben, naechste Seite.
    """
    if convert_from_bytes is None or pytesseract is None:
        logger.warning("OCR not available (pdf2image or pytesseract missing)")
        return "", False

    max_pages = min(page_count, OCR_MAX_PAGES) if page_count else OCR_MAX_PAGES
    ocr_fragments: list[str] = []
    rendered_any = False

    import tempfile

    for page_no in range(1, max_pages + 1):
        try:
            # Eigenes Temp-Verzeichnis pro Seite: pdf2image raeumt seine
            # Zwischendateien sonst bei Abbruechen nicht auf (57GB-Vorfall)
            with tempfile.TemporaryDirectory(prefix="ocr-page-") as tmpdir:
                images = convert_from_bytes(
                    data, dpi=OCR_DPI, first_page=page_no, last_page=page_no, output_folder=tmpdir
                )
                if images:
                    images[0].load()
        except Exception as exc:
            if PDFInfoNotInstalledError and isinstance(exc, PDFInfoNotInstalledError):
                logger.warning("Poppler not installed, skipping OCR")
                return "", False
            # Hinter der letzten Seite / defekte Seite: abbrechen
            if rendered_any:
                break
            logger.warning("Error converting PDF for OCR: %s", exc)
            return "", False

        if not images:
            break
        rendered_any = True

        try:
            ocr_text = pytesseract.image_to_string(images[0], lang="deu")
        except Exception as exc:
            logger.warning("OCR error for page %d: %s", page_no, exc)
            ocr_text = ""
        ocr_fragments.append(ocr_text.strip())
        images[0].close()
        del images

    text = "\n\n".join(f for f in ocr_fragments if f)
    return text, rendered_any


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

"""
Text Extraction Pipeline for the Ingestor.

Downloads PDF files and extracts text using a fallback chain:
1. pypdf (fast, text-based PDFs)
2. Tesseract OCR (local, scanned PDFs)
3. AI OCR (placeholder for future Mistral integration)

Async-capable: PDF downloads via httpx, sync extraction via asyncio.to_thread().

Die Textebene (pypdf) eines PDFs liest ein eigener Prozess mit Speicher- und Zeitgrenze (``textebene.py``,
``TEXT_LAYER_*``), nie der Ingestor selbst, und höchstens ``TEXT_LAYER_CONCURRENCY`` davon zugleich.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import httpx
from mandari_oparl.ziel import BlockedDestinationError, PinnedAsyncTransport, UnresolvableHostError

from src.config import settings
from src.extraction.textebene import EXIT_MEMORY, MAX_TEXT_CHARS

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

PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
TEXT_MIME_TYPES = {"text/plain", "text/html"}
SUPPORTED_MIME_TYPES = PDF_MIME_TYPES | TEXT_MIME_TYPES


def _download_transport() -> httpx.AsyncHTTPTransport:
    """Transport für Datei-Downloads: verbindet nur mit geprüften, öffentlichen Adressen (``mandari_oparl.ziel``)."""
    return PinnedAsyncTransport(
        allow_private=settings.fetch_allow_private_networks,
        allowed_hosts=[host for host in settings.fetch_internal_hosts.split(",") if host.strip()],
    )


def _content_encodings(response: httpx.Response) -> list[str]:
    """Kompressionsverfahren der Antwort laut ``Content-Encoding``, ohne ``identity``."""
    return [
        teil.strip().lower()
        for teil in response.headers.get("content-encoding", "").split(",")
        if teil.strip() and teil.strip().lower() != "identity"
    ]


class FileTooLargeError(Exception):
    """Die Datei überschreitet ``TEXT_EXTRACTION_MAX_SIZE_MB`` (Abbruch während des Downloads)."""


class TextExtractor:
    """
    Async text extraction pipeline for OParl files.

    Downloads files and extracts text content, then updates the database
    with the results.
    """

    def __init__(self, storage) -> None:
        """
        Args:
            storage: DatabaseStorage instance for querying/updating files.
        """
        self.storage = storage
        self.max_size_bytes = settings.text_extraction_max_size_mb * 1024 * 1024
        self.concurrency = settings.text_extraction_concurrency
        self.timeout = settings.text_extraction_timeout
        self.batch_size = settings.text_extraction_batch_size
        self._header_cache: dict[Any, dict[str, str]] = {}

    async def extract_pending_files(self, body_id: UUID) -> int:
        """
        Download and extract text from all pending files for a body.

        Args:
            body_id: The body UUID to process files for.

        Returns:
            Number of files successfully extracted.
        """
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

        try:
            data = await self._download(download_url, await self._download_headers(file_row.body_id))
        except FileTooLargeError:
            await self.storage.update_file_text(
                file_id=file_id,
                status="skipped",
                error=f"File too large: more than {self.max_size_bytes} bytes",
            )
            return False
        except UnresolvableHostError as e:
            # Name gerade nicht auflösbar: wie jeder andere Fehlschlag des Downloads
            logger.warning("Download failed for %s: %s", download_url, e)
            await self.storage.update_file_text(file_id=file_id, status="failed", error=f"Download failed: {e}")
            return False
        except BlockedDestinationError:
            # Ziel außerhalb des öffentlichen Netzes: kein Abruf, kein erneuter Versuch
            logger.warning("Download blocked for %s: destination is not a public address", download_url)
            await self.storage.update_file_text(
                file_id=file_id,
                status="skipped",
                error="Download blocked: destination is not a public address",
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

    async def _download_headers(self, body_id: Any) -> dict[str, str]:
        """
        Zusätzliche Download-Header je Quelle (``sync_config["download_headers"]``, Issue #116):
        manche RIS liefern Anlagen nur mit Referer oder Sitzungs-Cookie aus. Ergebnis je Body
        zwischengespeichert; ohne Body oder Konfiguration leer.
        """
        if body_id is None:
            return {}
        cache = self._header_cache
        if body_id not in cache:
            try:
                cache[body_id] = await self.storage.get_download_headers_for_body(body_id)
            except Exception as e:  # noqa: BLE001 - Header sind optional, Download läuft ohne weiter
                logger.warning("Download-Header für Body %s nicht ladbar: %s", body_id, e)
                cache[body_id] = {}
        return cache[body_id]

    async def _download(self, url: str, extra_headers: dict[str, str] | None = None) -> bytes:
        """
        Datei gestreamt laden: nur von geprüften, öffentlichen Adressen (auch nach Weiterleitungen) und höchstens
        ``max_size_bytes`` (``FileTooLargeError``).
        """
        headers = {
            "User-Agent": "Mandari/2.0 (+https://mandari.de; support@mandari.de)",
            **(extra_headers or {}),
            # Dateien unkomprimiert anfordern: Die Größengrenze unten zählt entpackte Bytes, und eine komprimierte
            # Antwort belegt beim Entpacken ein Vielfaches, bevor das erste Stück gezählt ist
            "Accept-Encoding": "identity",
        }
        chunks: list[bytes] = []
        size = 0
        async with (
            httpx.AsyncClient(timeout=self.timeout, transport=_download_transport()) as client,
            client.stream("GET", url, headers=headers, follow_redirects=True) as response,
        ):
            response.raise_for_status()
            # Mehrere Kompressionslagen übereinander vervielfachen, was ein gelesenes Stück entpackt belegt; so
            # etwas liefert kein Dateiserver
            if len(_content_encodings(response)) > 1:
                raise FileTooLargeError
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self.max_size_bytes:
                raise FileTooLargeError
            async for chunk in response.aiter_bytes(256 * 1024):
                size += len(chunk)
                if size > self.max_size_bytes:
                    raise FileTooLargeError
                chunks.append(chunk)
        return b"".join(chunks)

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


#: Skript des eigenen Prozesses, der die Textebene liest (nur Standardbibliothek und pypdf)
_TEXT_LAYER_SCRIPT = Path(__file__).with_name("textebene.py")
#: Höchstens so viel Ausgabe (JSON, Byte) liest der Ingestor von diesem Prozess; mehr gilt als Fehler. Reicht für
#: ``MAX_TEXT_CHARS`` Zeichen mit maskierten Zeichen (bis sechs Byte je Zeichen).
_MAX_LAYER_OUTPUT_BYTES: Final = 6 * MAX_TEXT_CHARS + 64 * 1024
#: Von stderr genügt der Anfang (für die Meldung)
_MAX_STDERR_BYTES: Final = 4096
#: Größte plausible Seitenzahl; darüber gilt sie als unbekannt
_MAX_PAGE_COUNT: Final = 100_000

#: Plätze für Prozesse der Textebene in diesem Ingestor-Prozess, über alle Quellen und Extraktoren
#: (``TEXT_LAYER_CONCURRENCY``); beim ersten Gebrauch angelegt
_text_layer_slots: threading.BoundedSemaphore | None = None
_text_layer_slots_lock = threading.Lock()


def _text_layer_slot() -> threading.BoundedSemaphore:
    global _text_layer_slots
    with _text_layer_slots_lock:
        if _text_layer_slots is None:
            _text_layer_slots = threading.BoundedSemaphore(max(1, settings.text_layer_concurrency))
        return _text_layer_slots


def _read_text_layer_isolated(data: bytes, file_name: str = "") -> tuple[str, int | None] | None:
    """
    Text und Seitenzahl der Textebene, gelesen von einem eigenen Prozess (``textebene.py``) mit Grenze des Adressraums
    (``TEXT_LAYER_MEMORY_LIMIT_MB``) und Zeitgrenze (``TEXT_LAYER_TIMEOUT``); höchstens ``TEXT_LAYER_CONCURRENCY``
    solcher Prozesse laufen zugleich, ihr Speicher bleibt so zusammen mit dem Grundbedarf des Ingestors begrenzt.

    ``None``, wenn der Prozess an einer Grenze oder mit einem Fehler endet: Die Datei geht dann an die Erkennung aus
    dem Bild, ohne dass dieser Prozess die Textebene liest. Die Ausgabe landet in temporären Dateien und wird nur bis
    ``_MAX_LAYER_OUTPUT_BYTES`` gelesen.
    """
    command = [sys.executable, "-I", str(_TEXT_LAYER_SCRIPT), str(settings.text_layer_memory_limit_mb)]
    with tempfile.TemporaryFile() as ausgabe, tempfile.TemporaryFile() as fehler:
        try:
            with _text_layer_slot():
                lauf = subprocess.run(
                    command,
                    input=data,
                    stdout=ausgabe,
                    stderr=fehler,
                    timeout=settings.text_layer_timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            logger.warning(
                "Textebene %s: Zeitgrenze (%.0f s), weiter mit der Texterkennung",
                file_name,
                settings.text_layer_timeout,
            )
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("Textebene %s: Prozess nicht startbar (%s)", file_name, type(exc).__name__)
            return None
        groesse = ausgabe.seek(0, os.SEEK_END)
        fehler.seek(0)
        meldung = fehler.read(_MAX_STDERR_BYTES).decode("utf-8", "replace").strip()
        if lauf.returncode != 0:
            speicher = lauf.returncode in (EXIT_MEMORY, -9) or "MemoryError" in meldung
            logger.warning(
                "Textebene %s: %s (Rückgabe %s), weiter mit der Texterkennung",
                file_name,
                "Speichergrenze" if speicher else "Fehler",
                lauf.returncode,
            )
            return None
        if groesse > _MAX_LAYER_OUTPUT_BYTES:
            logger.warning("Textebene %s: Ausgabe zu groß (%d Byte), weiter mit der Texterkennung", file_name, groesse)
            return None
        ausgabe.seek(0)
        try:
            daten = json.loads(ausgabe.read())
        except ValueError:
            daten = None
    if not isinstance(daten, dict) or not isinstance(daten.get("text"), str):
        logger.warning("Textebene %s: Ausgabe nicht lesbar, weiter mit der Texterkennung", file_name)
        return None
    seiten = daten.get("page_count")
    plausibel = isinstance(seiten, int) and not isinstance(seiten, bool) and 0 <= seiten <= _MAX_PAGE_COUNT
    return daten["text"][:MAX_TEXT_CHARS], seiten if plausibel else None


def _extract_text_from_pdf(data: bytes, file_name: str = "") -> tuple[str, int | None, str]:
    """
    Extract text from a PDF using the fallback chain: pypdf -> Tesseract.

    Die Textebene (pypdf) liest ein eigener Prozess mit Speicher- und Zeitgrenze (``_read_text_layer_isolated``),
    nie dieser Prozess, der alle Abgleiche trägt; pypdf hält die entpackten Inhalte aller gelesenen Seiten. Endet
    er an einer Grenze, geht die Datei ohne Seitenzahl an die Erkennung aus dem Bild.

    Returns:
        (text, page_count, extraction_method)
    """
    page_count = None

    # 1. Try pypdf (fast, for text-based PDFs), in einem eigenen Prozess
    layer = _read_text_layer_isolated(data, file_name)
    if layer is not None:
        text, page_count = layer
        if text.strip():
            logger.debug("pypdf extraction ok: %d chars", len(text))
            return text, page_count, "pypdf"

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

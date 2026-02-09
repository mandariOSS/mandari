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
from io import BytesIO
from uuid import UUID

import httpx

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

PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
TEXT_MIME_TYPES = {"text/plain", "text/html"}
SUPPORTED_MIME_TYPES = PDF_MIME_TYPES | TEXT_MIME_TYPES


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
        if mime_type and mime_type not in SUPPORTED_MIME_TYPES:
            # Allow unknown MIME types (try anyway), but skip known unsupported
            if mime_type.startswith(("image/", "video/", "audio/")):
                await self.storage.update_file_text(
                    file_id=file_id,
                    status="skipped",
                    error=f"Unsupported MIME type: {mime_type}",
                )
                return False

        try:
            data = await self._download(download_url)
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
        if not mime_type:
            if data[:5] == b"%PDF-":
                mime_type = "application/pdf"

        # Extract text
        try:
            text, page_count, method = await asyncio.to_thread(
                self._extract_text, data, mime_type, file_name
            )
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
        else:
            await self.storage.update_file_text(
                file_id=file_id,
                method=method or "none",
                status="completed",
                page_count=page_count,
                sha256_hash=sha256_hash,
            )
            return False

    async def _download(self, url: str) -> bytes:
        """Download a file via httpx async."""
        headers = {
            "User-Agent": "Mandari/2.0 (https://mandari.dev; contact@mandari.dev)",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            return response.content

    @staticmethod
    def _extract_text(
        data: bytes, mime_type: str, file_name: str
    ) -> tuple[str, int | None, str]:
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

    # 2. Tesseract OCR (local)
    text, success = _extract_text_with_ocr(data)
    if success and text.strip():
        logger.debug("Tesseract OCR ok: %d chars", len(text))
        return text, page_count, "tesseract"

    logger.warning("No text extracted from PDF")
    return "", page_count, "none"


def _extract_text_with_ocr(data: bytes) -> tuple[str, bool]:
    """Extract text via Tesseract OCR."""
    if convert_from_bytes is None or pytesseract is None:
        logger.warning("OCR not available (pdf2image or pytesseract missing)")
        return "", False

    try:
        images = convert_from_bytes(data, dpi=300)
    except Exception as exc:
        if PDFInfoNotInstalledError and isinstance(exc, PDFInfoNotInstalledError):
            logger.warning("Poppler not installed, skipping OCR")
        else:
            logger.warning("Error converting PDF for OCR: %s", exc)
        return "", False

    ocr_fragments: list[str] = []
    for image in images:
        try:
            ocr_text = pytesseract.image_to_string(image, lang="deu")
        except Exception as exc:
            logger.warning("OCR error for page: %s", exc)
            ocr_text = ""
        ocr_fragments.append(ocr_text.strip())

    text = "\n\n".join(f for f in ocr_fragments if f)
    return text, True


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

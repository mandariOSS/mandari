"""
Dokument-Extraktion Service.

Lädt Dokumente herunter und extrahiert Text aus PDFs (mit OCR-Fallback).
Portiert von _old/insight_ai/services/document_extraction.py.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import httpx
from django.utils.encoding import force_str

try:
    from PyPDF2 import PdfReader
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

PDF_MIME_TYPES = {
    "application/pdf",
    "application/x-pdf",
}

WORD_MIME_TYPES = {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass(slots=True)
class ExtractedDocument:
    """Ergebnis einer Dokumenten-Extraktion."""

    binary: bytes
    text: str
    checksum: str
    mime_type: str
    original_name: str
    source_url: str
    ocr_performed: bool = False
    page_count: Optional[int] = None


class DocumentDownloadError(RuntimeError):
    """Wird geworfen, wenn ein Dokument nicht heruntergeladen werden kann."""


class DocumentExtractionError(RuntimeError):
    """Wird geworfen, wenn Text nicht extrahiert werden kann."""


def _http_get(url: str, timeout: float = 60.0) -> httpx.Response:
    """Führt einen HTTP-GET Request aus."""
    headers = {
        "User-Agent": "Mandari/2.0 (https://mandari.dev; contact@mandari.dev)",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DocumentDownloadError(f"Download fehlgeschlagen: {url}") from exc
    return response


def _extract_text_from_pdf(data: bytes) -> tuple[str, Optional[int], bool]:
    """
    Extrahiert Text aus einer PDF-Datei.

    Versucht zuerst textbasierte Extraktion mit PyPDF2,
    fällt bei Bedarf auf OCR zurück.

    Returns:
        Tuple mit (text, page_count, ocr_used)
    """
    if PdfReader is None:
        logger.warning("PyPDF2 nicht installiert, PDF-Extraktion nicht möglich.")
        return "", None, False

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        logger.warning("PDF konnte nicht gelesen werden: %s", exc)
        return "", None, False

    text_fragments: list[str] = []
    ocr_used = False
    page_count = len(reader.pages)

    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        text_fragments.append(page_text.strip())

    text = "\n\n".join(fragment for fragment in text_fragments if fragment)

    # Falls kein Text extrahiert werden konnte, OCR versuchen
    if not text.strip():
        text, ocr_used = _extract_text_with_ocr(data)

    return text, page_count, ocr_used


def _extract_text_with_ocr(data: bytes) -> tuple[str, bool]:
    """
    Extrahiert Text aus einem Dokument mittels OCR.

    Returns:
        Tuple mit (text, success)
    """
    if convert_from_bytes is None or pytesseract is None:
        logger.warning("OCR nicht verfügbar (pdf2image oder pytesseract fehlt).")
        return "", False

    try:
        images = convert_from_bytes(data, dpi=300)
    except Exception as exc:
        if PDFInfoNotInstalledError and isinstance(exc, PDFInfoNotInstalledError):
            logger.warning("Poppler nicht installiert, OCR wird übersprungen.")
        else:
            logger.warning("Fehler beim Konvertieren für OCR: %s", exc)
        return "", False

    ocr_fragments: list[str] = []
    for image in images:
        try:
            # Deutsche Sprache für bessere Erkennung von Umlauten
            ocr_text = pytesseract.image_to_string(image, lang="deu")
        except Exception as exc:
            logger.warning("OCR-Fehler für Seite: %s", exc)
            ocr_text = ""
        ocr_fragments.append(ocr_text.strip())

    text = "\n\n".join(fragment for fragment in ocr_fragments if fragment)
    return text, True


def _extract_text_from_plain(data: bytes) -> str:
    """Dekodiert Textdateien in UTF-8 (Fallback latin-1)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="ignore")


def _strip_html_tags(html: str) -> str:
    """Minimale HTML-Bereinigung ohne externe Abhängigkeiten."""
    from html.parser import HTMLParser

    class TextExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.fragments: list[str] = []

        def handle_data(self, data: str) -> None:
            cleaned = data.strip()
            if cleaned:
                self.fragments.append(cleaned)

    parser = TextExtractor()
    parser.feed(html)
    return "\n".join(parser.fragments)


def extract_text_from_file(
    data: bytes,
    mime_type: str | None = None,
    file_name: str = "",
) -> tuple[str, bool, Optional[int]]:
    """
    Extrahiert Text aus Binärdaten basierend auf MIME-Typ.

    Args:
        data: Binärdaten der Datei
        mime_type: MIME-Typ der Datei
        file_name: Optionaler Dateiname für Fallback-Erkennung

    Returns:
        Tuple mit (text, ocr_performed, page_count)
    """
    text = ""
    ocr_used = False
    page_count: Optional[int] = None

    resolved_mime = mime_type or ""

    # PDF-Erkennung
    if resolved_mime in PDF_MIME_TYPES or file_name.lower().endswith(".pdf"):
        text, page_count, ocr_used = _extract_text_from_pdf(data)

    # Textdateien
    elif resolved_mime.startswith("text/"):
        text = _extract_text_from_plain(data)
        if resolved_mime == "text/html":
            text = _strip_html_tags(text)

    # Word-Dokumente (OCR-Fallback)
    elif resolved_mime in WORD_MIME_TYPES:
        logger.info("Word-Datei erkannt, versuche OCR-Fallback.")
        text, ocr_used = _extract_text_with_ocr(data)

    # Generischer Fallback
    else:
        text = _extract_text_from_plain(data)

    text = force_str(text or "").strip()
    return text, ocr_used, page_count


def download_and_extract(
    *,
    url: str,
    mime_type: str | None = None,
    original_name: str = "",
    timeout: float = 60.0,
) -> ExtractedDocument:
    """
    Lädt ein Dokument herunter und extrahiert Text.

    Args:
        url: Download-URL
        mime_type: MIME-Typ (optional, wird aus Response ermittelt)
        original_name: Originaler Dateiname
        timeout: HTTP-Timeout in Sekunden

    Returns:
        ExtractedDocument mit Binärdaten, Text und Metadaten
    """
    response = _http_get(url, timeout=timeout)
    binary = response.content
    resolved_mime = mime_type or response.headers.get("Content-Type", "").split(";")[0]
    checksum = hashlib.sha256(binary).hexdigest()

    text, ocr_used, page_count = extract_text_from_file(
        binary,
        mime_type=resolved_mime,
        file_name=original_name or url.split("/")[-1],
    )

    return ExtractedDocument(
        binary=binary,
        text=text,
        checksum=checksum,
        mime_type=resolved_mime,
        original_name=original_name or url.split("/")[-1],
        source_url=url,
        ocr_performed=ocr_used,
        page_count=page_count,
    )


async def download_and_extract_async(
    *,
    url: str,
    mime_type: str | None = None,
    original_name: str = "",
    timeout: float = 60.0,
) -> ExtractedDocument:
    """
    Asynchrone Version von download_and_extract.

    Args:
        url: Download-URL
        mime_type: MIME-Typ (optional)
        original_name: Originaler Dateiname
        timeout: HTTP-Timeout in Sekunden

    Returns:
        ExtractedDocument mit Binärdaten, Text und Metadaten
    """
    headers = {
        "User-Agent": "Mandari/2.0 (https://mandari.dev; contact@mandari.dev)",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DocumentDownloadError(f"Download fehlgeschlagen: {url}") from exc

    binary = response.content
    resolved_mime = mime_type or response.headers.get("Content-Type", "").split(";")[0]
    checksum = hashlib.sha256(binary).hexdigest()

    text, ocr_used, page_count = extract_text_from_file(
        binary,
        mime_type=resolved_mime,
        file_name=original_name or url.split("/")[-1],
    )

    return ExtractedDocument(
        binary=binary,
        text=text,
        checksum=checksum,
        mime_type=resolved_mime,
        original_name=original_name or url.split("/")[-1],
        source_url=url,
        ocr_performed=ocr_used,
        page_count=page_count,
    )

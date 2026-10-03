# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokument-Extraktion Service.

Lädt Dokumente herunter und extrahiert Text aus PDFs.

Fallback-Kette:
1. pypdf (schnell, nur für Text-PDFs)
2. Mistral OCR (API, hochwertig, wenn konfiguriert)
3. Tesseract OCR (lokal, als letzter Fallback)

Portiert von _old/insight_ai/services/document_extraction.py.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import httpx
from django.conf import settings
from django.utils.encoding import force_str

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None  # type: ignore[assignment, misc]

try:
    from pdf2image import convert_from_bytes, pdfinfo_from_bytes
    from pdf2image.exceptions import PDFInfoNotInstalledError
except ImportError:
    convert_from_bytes = None  # type: ignore[assignment, misc]
    pdfinfo_from_bytes = None  # type: ignore[assignment, misc]
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
    page_count: int | None = None
    extraction_method: str = "none"  # pypdf, mistral, tesseract, none


class DocumentDownloadError(RuntimeError):
    """Wird geworfen, wenn ein Dokument nicht heruntergeladen werden kann."""


class RobotsBlockedError(DocumentDownloadError):
    """Die robots.txt der Quelle sperrt das Dokument; ``reason`` beginnt mit ``robots.txt``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RobotsUnreachableError(DocumentDownloadError):
    """
    Die robots.txt der Quelle war nicht erreichbar (5xx, 408, 429, Netzfehler): Abruf zurückgestellt, keine
    Sperre. Aufrufer lassen die Datei in der Warteschlange und versuchen es später erneut.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _http_get(
    url: str,
    timeout: float = 60.0,
    extra_headers: dict[str, str] | None = None,
    sync_config: Any = None,
) -> httpx.Response:
    """
    Führt einen HTTP-GET Request aus (``extra_headers``: Download-Header je Quelle, Issue #116).

    Vorher gilt die robots.txt des Hosts (``sync_config`` der Quelle für eine Ausnahme mit Vermerk), geprüft
    mit dem User-Agent des Abrufs (``User-Agent`` in ``extra_headers``, sonst unser Standard). Ist das Dokument
    gesperrt, folgt ``RobotsBlockedError``, ist die robots.txt nicht erreichbar, ``RobotsUnreachableError`` –
    jeweils ohne Anfrage an die Quelle.
    """
    from . import host_pacing, robots

    agent = next(
        (v for k, v in (extra_headers or {}).items() if k.lower() == "user-agent" and v.strip()), robots.USER_AGENT
    )
    decision = robots.check(url, robots.KIND_FILES, sync_config=sync_config, agent=agent)
    if decision.unreachable:
        raise RobotsUnreachableError(decision.reason)
    if not decision.allowed:
        raise RobotsBlockedError(decision.reason)
    host_pacing.wait(url, sync_config=sync_config)  # Drossel je Host über alle Prozesse
    headers = {
        **{k: v for k, v in (extra_headers or {}).items() if k.lower() != "user-agent"},
        "User-Agent": agent,
    }
    from .safe_fetch import guarded_client

    try:
        # Nur öffentliche Ziele, auch nach Weiterleitungen (Adressen stammen aus der Quelle)
        with guarded_client(timeout=timeout) as client:
            response = client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DocumentDownloadError(f"Download fehlgeschlagen: {url}") from exc
    return response


def _extract_text_from_pdf(
    data: bytes, file_name: str = "", ocr_max_pages: int | None = None
) -> tuple[str, int | None, str]:
    """
    Extrahiert Text aus einer PDF-Datei.

    Fallback-Kette:
    1. pypdf (schnell, nur Text-PDFs)
    2. Mistral OCR (API, wenn konfiguriert)
    3. Tesseract OCR (lokal; ``ocr_max_pages`` begrenzt die erkannten Seiten)

    Returns:
        Tuple mit (text, page_count, extraction_method)
    """
    page_count = None

    # 1. Versuche pypdf (schnell, für Text-PDFs)
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

            text = "\n\n".join(fragment for fragment in text_fragments if fragment)

            if text.strip():
                logger.debug(f"pypdf Extraktion erfolgreich: {len(text)} Zeichen")
                return text, page_count, "pypdf"

        except Exception as exc:
            logger.warning("pypdf Extraktion fehlgeschlagen: %s", exc)

    # 2. Versuche Mistral OCR (wenn konfiguriert)
    mistral_api_key = getattr(settings, "MISTRAL_API_KEY", "")
    if mistral_api_key:
        try:
            from .mistral_ocr import extract_text_with_mistral

            text = extract_text_with_mistral(data, file_name or "document.pdf")
            if text.strip():
                logger.debug(f"Mistral OCR erfolgreich: {len(text)} Zeichen")
                return text, page_count, "mistral"

        except Exception as exc:
            logger.warning("Mistral OCR fehlgeschlagen: %s", exc)

    # 3. Fallback auf Tesseract OCR (lokal)
    text, success = _extract_text_with_ocr(data, max_pages=ocr_max_pages, page_count=page_count)
    if success and text.strip():
        logger.debug(f"Tesseract OCR erfolgreich: {len(text)} Zeichen")
        return text, page_count, "tesseract"

    # Kein Text extrahiert
    logger.warning("Keine Textextraktion möglich für PDF")
    return "", page_count, "none"


#: Auflösung für die Texterkennung; Tesseract erkennt Fließtext ab etwa 300 dpi zuverlässig
OCR_DPI = 300


def _extract_text_with_ocr(
    data: bytes, max_pages: int | None = None, page_count: int | None = None
) -> tuple[str, bool]:
    """
    Extrahiert Text aus einem Dokument mittels OCR.

    Seite für Seite und in Graustufen: Früher wurden alle Seiten auf einmal in Farbe gerastert
    (rund 26 MB je A4-Seite bei 300 dpi, dazu der Rohdatenstrom) – ein gescannter Antrag mit
    zwanzig Seiten brauchte über 1 GB Arbeitsspeicher und damit mehr, als der Web-Container hat.
    Jetzt liegt immer nur eine Seite (rund 9 MB) im Speicher. ``max_pages`` begrenzt die Zahl der
    erkannten Seiten, etwa für den Import im laufenden Seitenaufruf.

    Returns:
        Tuple mit (text, success)
    """
    if convert_from_bytes is None or pytesseract is None:
        logger.warning("OCR nicht verfügbar (pdf2image oder pytesseract fehlt).")
        return "", False

    total = page_count
    if total is None:
        try:
            total = int(pdfinfo_from_bytes(data)["Pages"]) if pdfinfo_from_bytes is not None else None
        except Exception as exc:
            if PDFInfoNotInstalledError and isinstance(exc, PDFInfoNotInstalledError):
                logger.warning("Poppler nicht installiert, OCR wird übersprungen.")
            else:
                logger.warning("Fehler beim Lesen der Seitenzahl für OCR: %s", type(exc).__name__)
            return "", False
    if not total:
        return "", False
    limit = total if max_pages is None else max(0, min(total, max_pages))

    ocr_fragments: list[str] = []
    for number in range(1, limit + 1):
        try:
            images = convert_from_bytes(data, dpi=OCR_DPI, first_page=number, last_page=number, grayscale=True)
        except Exception as exc:
            if PDFInfoNotInstalledError and isinstance(exc, PDFInfoNotInstalledError):
                logger.warning("Poppler nicht installiert, OCR wird übersprungen.")
                return "", False
            logger.warning("Fehler beim Konvertieren von Seite %s für OCR: %s", number, type(exc).__name__)
            continue
        for image in images:
            try:
                # Deutsche Sprache für bessere Erkennung von Umlauten
                ocr_text = pytesseract.image_to_string(image, lang="deu")
            except Exception as exc:
                logger.warning("OCR-Fehler für Seite %s: %s", number, type(exc).__name__)
                ocr_text = ""
            finally:
                image.close()
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
    ocr_max_pages: int | None = None,
) -> tuple[str, bool, int | None, str]:
    """
    Extrahiert Text aus Binärdaten basierend auf MIME-Typ.

    Args:
        data: Binärdaten der Datei
        mime_type: MIME-Typ der Datei
        file_name: Optionaler Dateiname für Fallback-Erkennung
        ocr_max_pages: Höchstzahl der Seiten für die lokale Texterkennung (``None`` = alle)

    Returns:
        Tuple mit (text, ocr_performed, page_count, extraction_method)
    """
    text = ""
    ocr_used = False
    page_count: int | None = None
    extraction_method = "none"

    resolved_mime = mime_type or ""

    # PDF-Erkennung
    if resolved_mime in PDF_MIME_TYPES or file_name.lower().endswith(".pdf"):
        text, page_count, extraction_method = _extract_text_from_pdf(data, file_name, ocr_max_pages)
        ocr_used = extraction_method in ("mistral", "tesseract")

    # Textdateien
    elif resolved_mime.startswith("text/"):
        text = _extract_text_from_plain(data)
        if resolved_mime == "text/html":
            text = _strip_html_tags(text)
        extraction_method = "text"

    # Word-Dokumente (OCR-Fallback)
    elif resolved_mime in WORD_MIME_TYPES:
        logger.info("Word-Datei erkannt, versuche OCR-Fallback.")
        text, ocr_used = _extract_text_with_ocr(data)
        extraction_method = "tesseract" if ocr_used else "none"

    # Generischer Fallback
    else:
        text = _extract_text_from_plain(data)
        extraction_method = "text" if text.strip() else "none"

    # PostgreSQL speichert keine Null-Bytes in Textfeldern; manche PDFs enthalten sie im Textstrom
    text = force_str(text or "").replace("\x00", "").strip()
    return text, ocr_used, page_count, extraction_method


def download_and_extract(
    *,
    url: str,
    mime_type: str | None = None,
    original_name: str = "",
    timeout: float = 60.0,
    extra_headers: dict[str, str] | None = None,
    sync_config: Any = None,
) -> ExtractedDocument:
    """
    Lädt ein Dokument herunter und extrahiert Text.

    Args:
        url: Download-URL
        mime_type: MIME-Typ (optional, wird aus Response ermittelt)
        original_name: Originaler Dateiname
        timeout: HTTP-Timeout in Sekunden
        sync_config: ``sync_config`` der Quelle (Ausnahme von der robots.txt)

    Returns:
        ExtractedDocument mit Binärdaten, Text und Metadaten

    Raises:
        RobotsBlockedError: die robots.txt sperrt das Dokument
        RobotsUnreachableError: die robots.txt ist nicht erreichbar (später erneut versuchen)
        DocumentDownloadError: Abruf fehlgeschlagen
    """
    response = _http_get(url, timeout=timeout, extra_headers=extra_headers, sync_config=sync_config)
    binary = response.content
    resolved_mime = mime_type or response.headers.get("Content-Type", "").split(";")[0]
    checksum = hashlib.sha256(binary).hexdigest()

    text, ocr_used, page_count, extraction_method = extract_text_from_file(
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
        extraction_method=extraction_method,
    )

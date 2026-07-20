# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datei-Service für Session-Anlagen (Issue #25).

Bietet:
- Validierung von Dateityp und Dateigröße
- Virenscan-Hook (per Setting konfigurierbar, standardmäßig No-Op)
- Text-Extraktion für die Session-Suche (best effort)
"""

import logging
import mimetypes

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

# Erlaubte Dateiendungen für Anlagen (Verwaltungs-Alltag)
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".txt",
    ".csv",
    ".rtf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
}

# Maximale Dateigröße in MB (per Setting überschreibbar)
MAX_FILE_SIZE_MB = getattr(settings, "SESSION_FILE_MAX_SIZE_MB", 50)

# Größenlimit für die Text-Extraktion (große Dateien überspringen)
TEXT_EXTRACTION_MAX_SIZE_MB = 25


class FileValidationError(Exception):
    """Validierungsfehler beim Datei-Upload (nutzerfreundliche Meldung)."""


def validate_upload(uploaded_file) -> None:
    """
    Validiert eine hochgeladene Datei (Typ + Größe).

    Raises:
        FileValidationError: bei unerlaubtem Typ oder Überschreitung der Größe
    """
    name = uploaded_file.name or ""
    extension = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if extension not in ALLOWED_EXTENSIONS:
        raise FileValidationError(
            f"Dateityp '{extension or 'unbekannt'}' ist nicht erlaubt. Erlaubt: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )

    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise FileValidationError(
            f"Datei '{name}' ist zu groß ({uploaded_file.size / (1024 * 1024):.1f} MB). "
            f"Maximal erlaubt: {MAX_FILE_SIZE_MB} MB."
        )

    scan_upload(uploaded_file)


def scan_upload(uploaded_file) -> None:
    """
    Virenscan-Hook.

    Ist ``SESSION_FILE_SCAN_HOOK`` (dotted path auf ein Callable) gesetzt,
    wird die Datei damit geprüft. Das Callable erhält das UploadedFile und
    muss bei Befund eine Exception mit nutzerfreundlicher Meldung werfen.
    Ohne Konfiguration: No-Op (Hook vorbereitet für z. B. ClamAV).
    """
    hook_path = getattr(settings, "SESSION_FILE_SCAN_HOOK", None)
    if not hook_path:
        return
    try:
        hook = import_string(hook_path)
    except ImportError:
        logger.error("SESSION_FILE_SCAN_HOOK '%s' konnte nicht geladen werden.", hook_path)
        return
    hook(uploaded_file)


def guess_mime_type(name: str) -> str:
    """MIME-Typ aus dem Dateinamen ableiten."""
    mime_type, _ = mimetypes.guess_type(name)
    return mime_type or "application/octet-stream"


def extract_text(data: bytes, mime_type: str, file_name: str) -> str:
    """
    Text aus einer Anlage extrahieren (Grundlage für die Session-Suche).

    Best effort: Fehler werden geloggt, der Upload schlägt dadurch nie fehl.
    """
    if len(data) > TEXT_EXTRACTION_MAX_SIZE_MB * 1024 * 1024:
        return ""
    try:
        from insight_core.services.document_extraction import extract_text_from_file

        text, _ocr_used, _pages, _method = extract_text_from_file(data, mime_type, file_name)
        return text or ""
    except Exception:
        logger.exception("Text-Extraktion für '%s' fehlgeschlagen.", file_name)
        return ""

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
from collections.abc import Set
from pathlib import PurePosixPath
from typing import Any

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


def file_parent(session_file: Any) -> Any:
    """Elternobjekt einer Anlage (Vorlage, TOP oder Sitzung); None ohne Zuordnung."""
    if session_file.paper_id:
        return session_file.paper
    if session_file.agenda_item_id:
        return session_file.agenda_item
    if session_file.meeting_id:
        return session_file.meeting
    return None


def is_non_public(session_file: Any) -> bool:
    """Ist die Anlage selbst oder ihr Elternobjekt nichtöffentlich? (Kennzeichen im Protokoll, Issue #221)."""
    if not session_file.is_public:
        return True
    parent = file_parent(session_file)
    if parent is None:
        return False
    if session_file.agenda_item_id:
        return not (parent.is_public and parent.meeting.is_public)
    return not parent.is_public


def file_visible(permissions: Set[str], session_file: Any) -> bool:
    """
    Darf, wer genau diese Berechtigungen hat, die Anlage sehen/herunterladen?

    Die eine Regel für Anlagen – Download-View und Sitzungsmappe (Issue #218) prüfen beide hiermit:
    - Basis-Sichtberechtigung des Elternobjekts (view_papers/view_meetings)
    - NÖ-Anlage oder NÖ-Elternobjekt (beim TOP auch die NÖ-Sitzung): zusätzlich die NÖ-Berechtigung
    """
    parent = file_parent(session_file)
    if session_file.paper_id:
        base_perm, np_perm = "view_papers", "view_non_public_papers"
        parent_public = parent.is_public if parent else True
    elif session_file.agenda_item_id:
        base_perm, np_perm = "view_meetings", "view_non_public_meetings"
        parent_public = (parent.is_public and parent.meeting.is_public) if parent else True
    elif session_file.meeting_id:
        base_perm, np_perm = "view_meetings", "view_non_public_meetings"
        parent_public = parent.is_public if parent else True
    else:
        # Anlage ohne Elternobjekt: restriktiv behandeln
        base_perm, np_perm = "view_papers", "view_non_public_papers"
        parent_public = True

    if base_perm not in permissions:
        return False
    if not session_file.is_public or not parent_public:
        return np_perm in permissions
    return True


def download_name(session_file: Any) -> str:
    """
    Dateiname für Downloads und das OParl-Feld ``fileName``: der Anzeigename der Anlage.

    Nie der Speichername – seit der Deduplizierung (Issue #226) teilen sich Anlagen mit
    gleichem Inhalt eine Datei, deren Name aus einem anderen (womöglich nichtöffentlichen)
    Upload stammen kann. Fehlt dem Anzeigenamen die Endung, kommt sie vom Speichernamen.
    """
    name = str(session_file.name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    stored = PurePosixPath(str(session_file.file.name or "")) if session_file.file else None
    if not name:
        name = "Anlage"
    if not PurePosixPath(name).suffix and stored is not None and stored.suffix:
        name += stored.suffix
    return name[:255]


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

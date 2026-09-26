# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datei-Service für Session-Anlagen (Issue #25).

Bietet:
- Validierung von Dateityp und Dateigröße
- Virenscan-Hook (per Setting konfigurierbar, standardmäßig No-Op)
- Text-Extraktion für die Session-Suche (best effort)
- sichere Auslieferung: MIME-Typ aus der Endung (Positivliste), im Browser angezeigt nur PDF und
  Rasterbilder, alles andere als Download; immer ``nosniff``, außer bei PDF eine Sandbox-CSP
"""

import logging
from collections.abc import Set
from pathlib import PurePosixPath
from typing import Any

from django.conf import settings
from django.http import FileResponse
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

#: Endung → MIME-Typ. Den Typ bestimmt der Server, nie die Angabe des hochladenden Browsers.
MIME_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".rtf": "application/rtf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}

#: Nur diese Typen zeigt der Browser an. Alles andere – HTML, SVG, Text, Office – kommt als Download.
INLINE_MIME_TYPES = frozenset({"application/pdf", "image/png", "image/jpeg", "image/gif"})

#: Für alle Antworten außer angezeigten PDFs: kein Skript, kein Formular, eigener (leerer) Ursprung.
#: Angezeigte PDFs erhalten sie nicht, weil Browser PDFs in einer Sandbox nicht darstellen.
SANDBOX_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"

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


def non_public_permission(session_file: Any) -> str:
    """NÖ-Sichtrecht, das eine nichtöffentliche Anlage bzw. eine Anlage an einem NÖ-Objekt verlangt."""
    if session_file.paper_id or not (session_file.agenda_item_id or session_file.meeting_id):
        return "view_non_public_papers"
    return "view_non_public_meetings"


def non_public_allowed(permissions: Set[str], session_file: Any) -> bool:
    """
    Darf, wer diese Rechte hat, die Anlage im Rahmen seines Fachrechts anfassen?

    Nichtöffentliche Anlagen und Anlagen an nichtöffentlichen Vorlagen, Sitzungen oder TOPs nur mit
    dem passenden NÖ-Sichtrecht – das Bearbeitungsrecht allein genügt nicht.
    """
    return not is_non_public(session_file) or non_public_permission(session_file) in permissions


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
    stored = str(session_file.file.name or "") if session_file.file else ""
    return display_file_name(session_file.name, stored)


def display_file_name(name: Any, stored_name: str = "") -> str:
    """Anzeigename als Dateiname: ohne Pfad und Steuerzeichen, fehlende Endung vom Speichernamen."""
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    stored = PurePosixPath(stored_name) if stored_name else None
    if not name:
        name = "Anlage"
    if not PurePosixPath(name).suffix and stored is not None and stored.suffix:
        name += stored.suffix
    return name[:255]


def blob_download_name(name: Any, blob: Any) -> str:
    """Dateiname einer Fassung (Anlage oder Vorlagenfassung) mit Endung vom gespeicherten Inhalt."""
    stored = str(blob.file.name or "") if blob is not None and blob.file else ""
    return display_file_name(name, stored)


def mime_type_for_name(name: str) -> str:
    """MIME-Typ aus der Dateiendung (Positivliste ``MIME_TYPES``), sonst ``application/octet-stream``."""
    suffix = PurePosixPath(str(name or "").replace("\\", "/")).suffix.lower()
    return MIME_TYPES.get(suffix, "application/octet-stream")


def file_response(handle: Any, filename: str, *, inline: bool = False) -> FileResponse:
    """
    Anlage ausliefern – die eine Stelle für OParl, geschützten Download und Fassungen.

    Der Typ kommt aus der Endung des Dateinamens, nie aus dem gespeicherten Wert des hochladenden
    Browsers. Im Browser angezeigt (``inline``) werden nur PDF und Rasterbilder; alles andere ist ein
    Download. ``nosniff`` verhindert, dass der Browser den Typ selbst errät.
    """
    mime_type = mime_type_for_name(filename)
    show = inline and mime_type in INLINE_MIME_TYPES
    response = FileResponse(handle, as_attachment=not show, filename=filename, content_type=mime_type)
    response["X-Content-Type-Options"] = "nosniff"
    if not (show and mime_type == "application/pdf"):
        response["Content-Security-Policy"] = SANDBOX_CSP
    return response


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

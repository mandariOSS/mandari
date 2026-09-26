# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anhänge im Work-Portal: Speicherpfade und geschützte Auslieferung.

Anhänge von Aufgaben, Fraktionssitzungen, der Sitzungsvorbereitung, Support-Tickets und
Briefköpfe gehören einer Organisation und oft nur einem Teil ihrer Mitglieder. Sie gehen
deshalb nie über den allgemeinen ``/media/``-Weg hinaus (``PROTECTED_PREFIXES`` stehen in
``PROTECTED_MEDIA_PREFIXES`` von ``mandari/urls.py``), sondern nur über Download-Views, die
Organisation, Sichtbarkeit und Rechte prüfen. Neue Dateien erhalten zufällige Namen; der
Originalname steht im Modell und wird beim Download gesetzt.
"""

from __future__ import annotations

import mimetypes
import uuid
from pathlib import PurePosixPath
from typing import Any

from django.http import FileResponse, Http404
from django.utils import timezone

from apps.common.uploads import is_embeddable

TASK_ATTACHMENTS = "tasks/attachments/"
FACTION_ATTACHMENTS = "faction/attachments/"
MEETING_DOCUMENTS = "meetings/documents/"
SUPPORT_ATTACHMENTS = "support/attachments/"
LETTERHEADS = "motions/letterheads/"

#: Upload-Präfixe, die nur über zugriffsgeprüfte Views ausgeliefert werden.
PROTECTED_PREFIXES = (
    TASK_ATTACHMENTS,
    FACTION_ATTACHMENTS,
    MEETING_DOCUMENTS,
    SUPPORT_ATTACHMENTS,
    LETTERHEADS,
)

#: Längste übernommene Dateiendung (".docx", ".jpeg" …); alles darüber fällt weg.
_MAX_SUFFIX = 10


def _random_name(prefix: str, filename: str) -> str:
    """``<prefix>JJJJ/MM/<zufall><endung>`` – der Originalname taucht im Pfad nicht auf."""
    suffix = PurePosixPath((filename or "").replace("\\", "/")).suffix.lower()
    if len(suffix) > _MAX_SUFFIX or not suffix[1:].isalnum():
        suffix = ""
    return f"{prefix}{timezone.now():%Y/%m}/{uuid.uuid4().hex}{suffix}"


def task_attachment_path(instance: Any, filename: str) -> str:
    return _random_name(TASK_ATTACHMENTS, filename)


def faction_attachment_path(instance: Any, filename: str) -> str:
    return _random_name(FACTION_ATTACHMENTS, filename)


def meeting_document_path(instance: Any, filename: str) -> str:
    return _random_name(MEETING_DOCUMENTS, filename)


def support_attachment_path(instance: Any, filename: str) -> str:
    return _random_name(SUPPORT_ATTACHMENTS, filename)


def letterhead_path(instance: Any, filename: str) -> str:
    return _random_name(LETTERHEADS, filename)


def attachment_response(fieldfile: Any, filename: str = "", *, allow_pdf_inline: bool = False) -> FileResponse:
    """
    Datei eines Anhangs nach bestandener Zugriffsprüfung ausliefern.

    Eingebettet (``inline``) gehen nur Bilder hinaus und – für die Vorschau im Browser – PDF,
    wenn ``allow_pdf_inline`` gesetzt ist. Alles andere kommt als Download. Der Inhaltstyp
    folgt der Dateiendung, nicht der beim Hochladen gemeldeten Angabe.
    """
    if not fieldfile:
        raise Http404("Datei nicht gefunden.")
    name = filename or PurePosixPath(fieldfile.name).name
    try:
        handle = fieldfile.open("rb")
    except (FileNotFoundError, ValueError, OSError):
        raise Http404("Datei nicht gefunden.") from None
    suffix = PurePosixPath(name).suffix.lower()
    inline = is_embeddable(name) or (allow_pdf_inline and suffix == ".pdf")
    content_type = mimetypes.guess_type(name)[0] if inline else None
    response = FileResponse(
        handle,
        as_attachment=not inline,
        filename=name,
        content_type=content_type or "application/octet-stream",
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response

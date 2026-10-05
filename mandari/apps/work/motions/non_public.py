# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nichtöffentliche Unterlagen im Dokumentenspeicher (Issue #873).

Eine PDF-Unterlage (etwa die Tagesordnung des nichtöffentlichen Teils einer Ratssitzung) wird als Dokument im
Ordner „Nichtöffentliche Vorgänge“ abgelegt (``DocumentFolder.non_public_folder``):

- Den Ordner und seine Dokumente öffnen nur vereidigte Mitglieder der Organisation (``Motion.access_level``,
  ``Motion.visible_to``), Freigaben sind ausgeschlossen (``Motion.can_share``, Ordner-Verwaltung).
- Der erkannte Text liegt verschlüsselt im Dokumentinhalt wie bei anderen Work-Dokumenten. Eine unverschlüsselte
  Suchkopie am Anhang (``MotionDocument.text_content``) entsteht bewusst nicht.
- Die Texterkennung läuft nur im eigenen Betrieb (pypdf, Tesseract), nie über einen externen Dienst.
- Ablage und Lesezugriffe stehen in der Änderungshistorie der Fraktionssitzungen (``FactionAuditLog``, als
  nichtöffentlich gekennzeichnet): Aufruf im Editor, frühere Fassungen, Download, Export und die TOP-Auswahl der
  Fraktionssitzung. Live-Bearbeitung und Kommentare laufen im bereits protokollierten Editoraufruf.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from django.conf import settings
from django.db import transaction

from apps.work.sanitize import sanitize_editor_html

from . import import_text

if TYPE_CHECKING:
    from django.core.files.uploadedfile import UploadedFile

    from apps.tenants.models import Membership, Organization

    from .models import Motion, MotionDocument

logger = logging.getLogger(__name__)

#: Texterkennung im Seitenaufruf höchstens für so viele Seiten (rund 5 s je Seite); die Datei bleibt vollständig
OCR_MAX_PAGES = 10

#: Arten des Zugriffs in der Änderungshistorie
ACCESS_OPENED = "geöffnet"
ACCESS_DOWNLOADED = "heruntergeladen"
ACCESS_EXPORTED = "exportiert"
ACCESS_REVISION = "frühere Fassung geöffnet"
ACCESS_PROPOSALS = "zur TOP-Auswahl geöffnet"

#: Feste Meldungen
UNREADABLE = "Die Datei konnte nicht gelesen werden. Bitte prüfen Sie, ob es eine unbeschädigte PDF-Datei ist."
NO_TEXT = "<p><em>Text konnte nicht erkannt werden. Die Unterlage liegt als Anhang am Dokument.</em></p>"


@dataclass
class ExtractedUnterlage:
    """Erkannter Text einer Unterlage: Zeilen (für die Erkennung der TOPs) und Absätze (für den Dokumentinhalt)."""

    lines: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    page_count: int | None = None
    ocr_performed: bool = False
    #: Hinweis für die Oberfläche, etwa „Texterkennung nur für die ersten Seiten“
    notice: str = ""


def ocr_max_pages() -> int:
    return int(getattr(settings, "WORK_NON_PUBLIC_OCR_MAX_PAGES", OCR_MAX_PAGES))


def extract(data: bytes, file_name: str) -> ExtractedUnterlage | None:
    """
    Text einer PDF-Unterlage: Textebene mit Zeilen (pypdf), ohne Textebene Texterkennung im eigenen Betrieb.

    ``None``, wenn die Datei weder Text noch Seiten hat (beschädigt oder keine PDF).
    """
    from insight_core.services.document_extraction import extract_text_from_file

    parsed = import_text.pdf_page_lines(data)
    pages = parsed[0] if parsed else []
    page_count = parsed[1] if parsed else None
    lines = [line.strip() for page in pages for line in page]
    if any(lines):
        return ExtractedUnterlage(
            lines=lines,
            paragraphs=import_text.paragraphs_from_lines(pages),
            page_count=page_count,
        )

    limit = ocr_max_pages()
    text, ocr_performed, ocr_page_count, _method = extract_text_from_file(
        data=data,
        mime_type="application/pdf",
        file_name=file_name,
        ocr_max_pages=limit,
        allow_external=False,
    )
    page_count = page_count or ocr_page_count
    if not text and page_count is None:
        return None
    result = ExtractedUnterlage(
        lines=[line.strip() for line in text.splitlines()],
        paragraphs=import_text.text_paragraphs(text) if text else [],
        page_count=page_count,
        ocr_performed=ocr_performed,
    )
    if ocr_performed and page_count and page_count > limit:
        result.notice = (
            f"Texterkennung für die ersten {limit} von {page_count} Seiten. "
            "Die vollständige Unterlage liegt als Anhang am Dokument."
        )
    return result


def store(
    *,
    organization: Organization,
    author: Membership,
    uploaded_file: UploadedFile[Any],
    file_size: int,
    title: str,
    extracted: ExtractedUnterlage,
) -> Motion:
    """Unterlage als Dokument mit Anhang in „Nichtöffentliche Vorgänge“ ablegen – beides oder nichts."""
    from .models import DocumentFolder, Motion, MotionDocument

    html = import_text.paragraphs_to_html(extracted.paragraphs) if extracted.paragraphs else NO_TEXT
    with transaction.atomic():
        folder = DocumentFolder.non_public_folder(organization, created_by=author)
        motion = Motion(
            organization=organization,
            author=author,
            responsible=author,
            title=title[:500],
            # Eine Unterlage der Verwaltung, kein Entwurf: abgelegt (Inhalt gesperrt, keine Fristen)
            status="archived",
            # Im Ordner sehen alle Vereidigten das Dokument (Motion.access_level). Gespeichert als „privat“, damit
            # eine ältere Version ohne diese Regel (Rückfall) es nur Autor:in und Federführung zeigt.
            visibility="private",
            folder=folder,
        )
        cast(Any, motion).set_content_encrypted(sanitize_editor_html(html))
        motion.save()
        document = MotionDocument(
            motion=motion,
            file=uploaded_file,
            filename=(uploaded_file.name or "unterlage.pdf")[:255],
            mime_type="application/pdf",
            file_size=file_size,
            uploaded_by=author,
        )
        document.save()
    _log(motion, author, None, "internal_document_stored", {"seiten": extracted.page_count or 0})
    return motion


def hidden_document_ids(membership: Membership | None) -> list[str]:
    """
    Kennungen (als Text, wie in ``Notification.metadata``) der Unterlagen in „Nichtöffentliche Vorgänge“, die
    ``membership`` nicht öffnen darf – etwa nach Entzug der Vereidigung.

    Für vereidigte Mitglieder leer, ohne Abfrage. Sonst eine kleine Abfrage nach dem Ordner und nur, wenn es ihn
    gibt, eine zweite nach seinen Dokumenten.
    """
    from apps.work.faction.visibility import is_sworn_member

    from .models import DocumentFolder, Motion, sworn_in_only_q

    if membership is None or is_sworn_member(membership):
        return []
    folder_ids = list(
        DocumentFolder.objects.filter(organization_id=membership.organization_id)
        .filter(sworn_in_only_q(""))
        .values_list("id", flat=True)
    )
    if not folder_ids:
        return []
    return [
        str(pk)
        for pk in Motion.objects.filter(
            organization_id=membership.organization_id, folder_id__in=folder_ids
        ).values_list("id", flat=True)
    ]


def log_access(
    motion: Motion, membership: Membership, request: Any, kind: str, *, document: MotionDocument | None = None
) -> None:
    """Aufruf, Download oder Export einer nichtöffentlichen Unterlage in die Änderungshistorie schreiben."""
    _log(document or motion, membership, request, "internal_document_access", {"zugriff": kind}, motion=motion)


def _log(
    instance: Any,
    membership: Membership,
    request: Any,
    action: str,
    changes: dict[str, Any],
    *,
    motion: Motion | None = None,
) -> None:
    from apps.work.faction.audit import log_event

    organization = (motion or instance).organization
    cast(Any, log_event)(
        action,
        instance,
        organization=organization,
        membership=membership,
        request=request,
        changes=changes,
        is_internal=True,
    )

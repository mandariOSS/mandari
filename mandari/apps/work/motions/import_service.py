# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Import von PDF- und DOCX-Dateien als Dokumente.

Der Text wird mit Gliederung (Absätze, Listen, Tabellen) in den Editor übernommen
(``import_text``), die Originaldatei bleibt als Anhang am Dokument. Gescannte PDFs ohne
Textebene gehen durch die Texterkennung – im laufenden Seitenaufruf höchstens
``IMPORT_OCR_MAX_PAGES`` Seiten, damit Laufzeit und Arbeitsspeicher begrenzt bleiben (#620).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction

from apps.work.sanitize import sanitize_editor_html
from insight_core.services.document_extraction import extract_text_from_file

from . import import_text

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Organization

    from .models import Motion, MotionDocument, MotionType


logger = logging.getLogger(__name__)

#: Meldung bei unerwarteten Fehlern; die Ausnahme selbst steht im Protokoll
IMPORT_FAILED_MESSAGE = "Die Datei konnte nicht gelesen werden. Bitte prüfen Sie, ob sie beschädigt ist."

#: Texterkennung im Seitenaufruf: rund 5 s je Seite, darum begrenzt (Rest bleibt im Anhang)
IMPORT_OCR_MAX_PAGES = 10

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: Länge des Suchtexts am Anhang
_SEARCH_TEXT_LIMIT = 50000


@dataclass
class ImportResult:
    """Result of a file import operation (PDF or DOCX)."""

    success: bool
    motion: Motion | None = None
    document: MotionDocument | None = None
    error: str | None = None
    extracted_text_length: int = 0
    ocr_performed: bool = False
    #: Hinweis für die Nutzerin (z. B. Texterkennung nur für die ersten Seiten)
    notice: str | None = None


def import_ocr_max_pages() -> int:
    return int(getattr(settings, "WORK_IMPORT_OCR_MAX_PAGES", IMPORT_OCR_MAX_PAGES))


def title_from_filename(name: str, extensions: tuple[str, ...]) -> str:
    """Titel aus dem Dateinamen: ohne Endung, Unter-/Bindestriche als Leerzeichen, einfacher Abstand."""
    title = name or ""
    lowered = title.lower()
    for ext in extensions:
        if lowered.endswith(ext):
            title = title[: -len(ext)]
            break
    title = " ".join(title.replace("_", " ").replace("-", " ").split())
    return (title[0].upper() + title[1:])[:500] if title else "Importiertes Dokument"


class MotionImportService:
    """Importiert Dateien (PDF/DOCX) als neue Dokumente mit Anhang."""

    @classmethod
    def _create(
        cls,
        *,
        uploaded_file: UploadedFile,
        organization: Organization,
        author: Membership,
        motion_type: MotionType | None,
        title: str,
        visibility: str,
        html: str,
        search_text: str,
        mime_type: str,
        file_size: int,
    ) -> tuple[Motion, MotionDocument]:
        """Dokument und Anhang gemeinsam anlegen – scheitert der Anhang, entsteht kein halbes Dokument."""
        from .models import Motion, MotionDocument

        with transaction.atomic():
            motion = Motion(
                organization=organization,
                author=author,
                title=title,
                status="draft",
                visibility=visibility,
                responsible=author,
            )
            if motion_type:
                motion.document_type = motion_type
            motion.set_content_encrypted(html)
            motion.save()
            motion.apply_default_checklist()

            document = MotionDocument(
                motion=motion,
                file=uploaded_file,
                filename=(uploaded_file.name or "")[:255],
                mime_type=mime_type,
                file_size=file_size,
                text_content=search_text[:_SEARCH_TEXT_LIMIT],
                uploaded_by=author,
            )
            document.save()
        return motion, document

    @classmethod
    def import_pdf(
        cls,
        pdf_file: UploadedFile,
        organization: Organization,
        author: Membership,
        motion_type: MotionType | None = None,
        title: str | None = None,
        visibility: str = "private",
    ) -> ImportResult:
        """PDF als neues Dokument: Text mit Absätzen; ohne Textebene per Texterkennung."""
        try:
            file_content = pdf_file.read()
            pdf_file.seek(0)  # für das Speichern als Anhang

            ocr_performed = False
            notice = None
            parsed = import_text.pdf_page_lines(file_content)
            paragraphs = import_text.paragraphs_from_lines(parsed[0]) if parsed else []
            page_count = parsed[1] if parsed else None

            if not paragraphs:
                # Keine Textebene (Scan) oder pypdf kann die Datei nicht öffnen: Texterkennung
                limit = import_ocr_max_pages()
                text, ocr_performed, ocr_page_count, _method = extract_text_from_file(
                    data=file_content,
                    mime_type="application/pdf",
                    file_name=pdf_file.name or "",
                    ocr_max_pages=limit,
                )
                page_count = page_count or ocr_page_count
                # Weder Text noch Seiten: keine lesbare PDF (beschädigt oder nur dem Namen nach PDF).
                # Gescannte PDFs ohne verfügbare Texterkennung haben Seiten und werden mit Hinweis übernommen.
                if not text and page_count is None:
                    logger.warning("PDF '%s' ist nicht lesbar, Import abgebrochen", pdf_file.name)
                    return ImportResult(success=False, error=IMPORT_FAILED_MESSAGE)
                paragraphs = import_text.text_paragraphs(text) if text else []
                if ocr_performed and page_count and page_count > limit:
                    notice = (
                        f"Texterkennung für die ersten {limit} von {page_count} Seiten. "
                        "Die vollständige Datei liegt als Anhang am Dokument."
                    )

            if paragraphs:
                html = import_text.paragraphs_to_html(paragraphs)
            else:
                html = "<p><em>Text konnte nicht extrahiert werden. Bitte überprüfen Sie das Original-PDF.</em></p>"
            search_text = "\n\n".join(paragraphs)

            motion, document = cls._create(
                uploaded_file=pdf_file,
                organization=organization,
                author=author,
                motion_type=motion_type,
                title=title or title_from_filename(pdf_file.name or "", (".pdf",)),
                visibility=visibility,
                html=sanitize_editor_html(html),
                search_text=search_text,
                mime_type="application/pdf",
                file_size=len(file_content),
            )
            logger.info(
                "PDF-Import: Dokument %s angelegt (%s Seiten, %s Absätze, Texterkennung: %s)",
                motion.id,
                page_count,
                len(paragraphs),
                ocr_performed,
            )
            return ImportResult(
                success=True,
                motion=motion,
                document=document,
                extracted_text_length=len(search_text),
                ocr_performed=ocr_performed,
                notice=notice,
            )

        except Exception:
            logger.exception("PDF-Import fehlgeschlagen: %s", pdf_file.name)
            # Feste Meldung: Texte aus Bibliotheken bleiben im Protokoll
            return ImportResult(success=False, error=IMPORT_FAILED_MESSAGE)

    @classmethod
    def import_docx(
        cls,
        docx_file: UploadedFile,
        organization: Organization,
        author: Membership,
        motion_type: MotionType | None = None,
        title: str | None = None,
        visibility: str = "private",
    ) -> ImportResult:
        """DOCX als neues Dokument: Absätze, Überschriften, Listen, Tabellen und Formatierung."""
        try:
            file_content = docx_file.read()
            docx_file.seek(0)

            html_content = import_text.docx_to_html(file_content)
            search_text = import_text.html_to_text(html_content)
            html = (
                sanitize_editor_html(html_content) if search_text else "<p><em>Kein Text im Dokument gefunden.</em></p>"
            )

            motion, document = cls._create(
                uploaded_file=docx_file,
                organization=organization,
                author=author,
                motion_type=motion_type,
                title=title or title_from_filename(docx_file.name or "", (".docx",)),
                visibility=visibility,
                html=html,
                search_text=search_text,
                mime_type=DOCX_MIME,
                file_size=len(file_content),
            )
            logger.info("DOCX-Import: Dokument %s angelegt (%s Zeichen Text)", motion.id, len(search_text))
            return ImportResult(
                success=True,
                motion=motion,
                document=document,
                extracted_text_length=len(search_text),
            )

        except ImportError:
            logger.error("python-docx is not installed. Install with: pip install python-docx")
            return ImportResult(success=False, error="DOCX-Import nicht verfügbar (python-docx fehlt)")
        except Exception:
            logger.exception("DOCX-Import fehlgeschlagen: %s", docx_file.name)
            return ImportResult(success=False, error=IMPORT_FAILED_MESSAGE)

    @classmethod
    def import_file(
        cls,
        uploaded_file: UploadedFile,
        organization: Organization,
        author: Membership,
        motion_type: MotionType | None = None,
        title: str | None = None,
        visibility: str = "private",
    ) -> ImportResult:
        """Importiert eine Datei (PDF oder DOCX) je nach Endung."""
        name = (uploaded_file.name or "").lower()
        if name.endswith(".docx"):
            return cls.import_docx(
                docx_file=uploaded_file,
                organization=organization,
                author=author,
                motion_type=motion_type,
                title=title,
                visibility=visibility,
            )
        return cls.import_pdf(
            pdf_file=uploaded_file,
            organization=organization,
            author=author,
            motion_type=motion_type,
            title=title,
            visibility=visibility,
        )

    @classmethod
    def import_multiple_files(
        cls,
        files: list[UploadedFile],
        organization: Organization,
        author: Membership,
        motion_type: MotionType | None = None,
        visibility: str = "private",
    ) -> list[ImportResult]:
        """Import multiple files (PDF/DOCX) as Motion documents."""
        return [
            cls.import_file(
                uploaded_file=f,
                organization=organization,
                author=author,
                motion_type=motion_type,
                visibility=visibility,
            )
            for f in files
        ]

    @classmethod
    def import_multiple_pdfs(
        cls,
        pdf_files: list[UploadedFile],
        organization: Organization,
        author: Membership,
        motion_type: MotionType | None = None,
        visibility: str = "private",
    ) -> list[ImportResult]:
        """Import multiple PDF files as Motion documents."""
        return [
            cls.import_pdf(
                pdf_file=pdf_file,
                organization=organization,
                author=author,
                motion_type=motion_type,
                visibility=visibility,
            )
            for pdf_file in pdf_files
        ]


# Singleton instance for convenience
motion_import_service = MotionImportService()

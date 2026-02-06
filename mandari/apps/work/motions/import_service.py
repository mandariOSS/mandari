# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PDF Import Service for Motions.

Imports PDF files as documents, extracting text content
and storing the original file as an attachment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.files.uploadedfile import UploadedFile

from insight_core.services.document_extraction import extract_text_from_file

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Organization

    from .models import Motion, MotionDocument, MotionType


logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Result of a PDF import operation."""

    success: bool
    motion: Motion | None = None
    document: MotionDocument | None = None
    error: str | None = None
    extracted_text_length: int = 0
    ocr_performed: bool = False


class MotionImportService:
    """
    Service for importing PDFs as Motion documents.

    Extracts text content from PDFs and creates Motion instances
    with the extracted text as content.
    """

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
        """
        Import a PDF file as a new Motion document.

        Args:
            pdf_file: The uploaded PDF file
            organization: The organization to create the motion in
            author: The membership creating the motion
            motion_type: Optional document type
            title: Optional title (defaults to filename)
            visibility: Visibility setting (default: private)

        Returns:
            ImportResult with the created motion and document
        """
        from .models import Motion, MotionDocument

        try:
            # Read file content
            file_content = pdf_file.read()
            pdf_file.seek(0)  # Reset for later save

            # Extract text
            text_content, ocr_performed, page_count = extract_text_from_file(
                data=file_content,
                mime_type="application/pdf",
                file_name=pdf_file.name,
            )

            # Generate title from filename if not provided
            if not title:
                # Remove .pdf extension and clean up
                title = pdf_file.name
                if title.lower().endswith(".pdf"):
                    title = title[:-4]
                # Clean up common patterns
                title = title.replace("_", " ").replace("-", " ").strip()
                # Capitalize first letter
                if title:
                    title = title[0].upper() + title[1:]

            # Create the Motion
            motion = Motion(
                organization=organization,
                author=author,
                title=title,
                status="draft",
                visibility=visibility,
            )

            # Set document type if provided
            if motion_type:
                motion.document_type = motion_type

            # Set content (encrypted)
            if text_content:
                # Wrap in basic HTML if it's plain text
                if not text_content.strip().startswith("<"):
                    # Convert line breaks to paragraphs
                    paragraphs = text_content.split("\n\n")
                    html_content = "\n".join(f"<p>{p.strip()}</p>" for p in paragraphs if p.strip())
                    motion.set_content_encrypted(html_content)
                else:
                    motion.set_content_encrypted(text_content)
            else:
                motion.set_content_encrypted(
                    "<p><em>Text konnte nicht extrahiert werden. Bitte überprüfen Sie das Original-PDF.</em></p>"
                )

            motion.save()

            # Create MotionDocument attachment
            document = MotionDocument(
                motion=motion,
                file=pdf_file,
                filename=pdf_file.name,
                mime_type="application/pdf",
                file_size=len(file_content),
                text_content=text_content[:50000] if text_content else "",  # Limit for search
                uploaded_by=author,
            )
            document.save()

            logger.info(
                f"Imported PDF '{pdf_file.name}' as motion '{motion.title}' "
                f"(ID: {motion.id}, text length: {len(text_content)}, OCR: {ocr_performed})"
            )

            return ImportResult(
                success=True,
                motion=motion,
                document=document,
                extracted_text_length=len(text_content),
                ocr_performed=ocr_performed,
            )

        except Exception as e:
            logger.exception(f"Failed to import PDF '{pdf_file.name}': {e}")
            return ImportResult(
                success=False,
                error=str(e),
            )

    @classmethod
    def import_multiple_pdfs(
        cls,
        pdf_files: list[UploadedFile],
        organization: Organization,
        author: Membership,
        motion_type: MotionType | None = None,
        visibility: str = "private",
    ) -> list[ImportResult]:
        """
        Import multiple PDF files as Motion documents.

        Args:
            pdf_files: List of uploaded PDF files
            organization: The organization to create motions in
            author: The membership creating the motions
            motion_type: Optional document type for all imports
            visibility: Visibility setting for all imports

        Returns:
            List of ImportResult objects
        """
        results = []
        for pdf_file in pdf_files:
            result = cls.import_pdf(
                pdf_file=pdf_file,
                organization=organization,
                author=author,
                motion_type=motion_type,
                visibility=visibility,
            )
            results.append(result)
        return results


# Singleton instance for convenience
motion_import_service = MotionImportService()

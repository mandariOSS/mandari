# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PDF Export Service for Motions.

This module handles the generation of PDF documents from motion content,
including support for letterheads (PDF backgrounds) and proper formatting.
"""

import io
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

if TYPE_CHECKING:
    from .models import Motion


class MotionExportService:
    """Service for exporting motions to various formats."""

    # Default margins in mm
    MARGIN_TOP = 30
    MARGIN_BOTTOM = 25
    MARGIN_LEFT = 25
    MARGIN_RIGHT = 25

    def export_to_pdf(self, motion: "Motion") -> bytes:
        """
        Generate a PDF from the motion content.

        Args:
            motion: The motion to export

        Returns:
            bytes: The PDF file content
        """
        # Generate HTML content
        html_content = self._render_html(motion)

        # Convert HTML to PDF
        pdf_content = self._html_to_pdf(html_content)

        # Apply letterhead if available
        if motion.letterhead and motion.letterhead.pdf_file:
            pdf_content = self._apply_letterhead(pdf_content, motion.letterhead)

        return pdf_content

    def _render_html(self, motion: "Motion") -> str:
        """Render the motion content as HTML for PDF generation."""
        context = {
            "motion": motion,
            "title": motion.title,
            "content": motion.content or "",
            "author_name": motion.author.user.get_full_name() if motion.author else "",
            "organization_name": motion.organization.name,
            "created_at": motion.created_at,
            "document_type": motion.document_type.name if motion.document_type else motion.get_type_display(),
        }

        return render_to_string("work/motions/export/pdf_template.html", context)

    def _html_to_pdf(self, html_content: str) -> bytes:
        """Convert HTML to PDF using xhtml2pdf."""
        try:
            from xhtml2pdf import pisa

            result = io.BytesIO()

            # Create PDF
            pisa_status = pisa.CreatePDF(
                src=html_content,
                dest=result,
                encoding="UTF-8",
            )

            if pisa_status.err:
                raise Exception(f"PDF generation error: {pisa_status.err}")

            return result.getvalue()

        except ImportError:
            # Fallback: Generate a simple PDF with reportlab
            return self._simple_pdf(html_content)

    def _simple_pdf(self, html_content: str) -> bytes:
        """Fallback simple PDF generation using reportlab."""
        import re

        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=self.MARGIN_LEFT * mm,
            rightMargin=self.MARGIN_RIGHT * mm,
            topMargin=self.MARGIN_TOP * mm,
            bottomMargin=self.MARGIN_BOTTOM * mm,
        )

        styles = getSampleStyleSheet()
        story = []

        # Strip HTML tags for simple text
        text = re.sub(r"<[^>]+>", "", html_content)
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")

        # Add content
        for line in text.split("\n"):
            line = line.strip()
            if line:
                story.append(Paragraph(line, styles["Normal"]))
                story.append(Spacer(1, 6))

        doc.build(story)
        return buffer.getvalue()

    def _apply_letterhead(self, pdf_content: bytes, letterhead) -> bytes:
        """
        Overlay the PDF content on the letterhead background.

        Args:
            pdf_content: The generated PDF content
            letterhead: The OrganizationLetterhead instance

        Returns:
            bytes: The merged PDF with letterhead
        """
        try:
            from pypdf import PdfReader, PdfWriter

            # Read the content PDF
            content_pdf = PdfReader(io.BytesIO(pdf_content))

            # Read the letterhead PDF
            letterhead_pdf = PdfReader(letterhead.pdf_file)
            letterhead_page = letterhead_pdf.pages[0]

            # Create output PDF
            output = PdfWriter()

            # Merge each page with the letterhead
            for page_num, content_page in enumerate(content_pdf.pages):
                # Create a new page from letterhead
                from copy import copy

                new_page = copy(letterhead_page)

                # Merge content on top
                new_page.merge_page(content_page)
                output.add_page(new_page)

            # Write to bytes
            result = io.BytesIO()
            output.write(result)
            return result.getvalue()

        except Exception as e:
            # If letterhead merging fails, return original PDF
            print(f"Letterhead merge error: {e}")
            return pdf_content

    def export_to_docx(self, motion: "Motion") -> bytes:
        """
        Generate a DOCX from the motion content.

        Args:
            motion: The motion to export

        Returns:
            bytes: The DOCX file content
        """
        # DOCX export is more complex and would need python-docx
        # For now, return a simple text-based implementation
        raise NotImplementedError("DOCX export not yet implemented")


# Singleton instance
motion_export_service = MotionExportService()

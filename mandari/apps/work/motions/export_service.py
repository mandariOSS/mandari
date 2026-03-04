# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Export Service for Motions.

Handles generation of PDF and DOCX documents from motion content,
including support for letterheads (PDF backgrounds) and proper formatting.
TipTap editor HTML is cleaned before export (comment marks stripped etc.).
"""

import io
import logging
import re
from typing import TYPE_CHECKING

from django.template.loader import render_to_string

if TYPE_CHECKING:
    from .models import Motion

logger = logging.getLogger(__name__)


def clean_editor_html(html: str) -> str:
    """
    Clean TipTap editor HTML for export.

    Strips comment marks, data attributes, and other editor-specific
    elements that shouldn't appear in exported documents.
    """
    if not html:
        return ""

    # Remove comment mark wrappers (mark/span) and keep inner text
    html = re.sub(r"<(?:mark|span)[^>]*data-comment-id[^>]*>(.*?)</(?:mark|span)>", r"\1", html, flags=re.DOTALL)

    # Remove any remaining data- attributes from all elements
    html = re.sub(r'\s+data-[a-z-]+="[^"]*"', "", html)

    # Remove editor-specific CSS classes
    html = re.sub(r'\s+class="comment-mark[^"]*"', "", html)
    html = re.sub(r'\s+class="is-editor-empty"', "", html)

    return html


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
        # Clean editor-specific HTML before rendering
        content = clean_editor_html(motion.content or "")

        context = {
            "title": motion.title,
            "content": content,
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

        # Strip HTML tags for simple text
        text = re.sub(r"<[^>]+>", "", html_content)
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")

        # Add content
        story = []
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
            from copy import copy

            from pypdf import PdfReader, PdfWriter

            # Read the content PDF
            content_pdf = PdfReader(io.BytesIO(pdf_content))

            # Read the letterhead PDF
            letterhead_pdf = PdfReader(letterhead.pdf_file)
            letterhead_page = letterhead_pdf.pages[0]

            # Create output PDF
            output = PdfWriter()

            # Merge each page with the letterhead
            for content_page in content_pdf.pages:
                new_page = copy(letterhead_page)
                new_page.merge_page(content_page)
                output.add_page(new_page)

            # Write to bytes
            result = io.BytesIO()
            output.write(result)
            return result.getvalue()

        except Exception as e:
            logger.warning(f"Letterhead merge error: {e}")
            return pdf_content

    def export_to_docx(self, motion: "Motion") -> bytes:
        """
        Generate a DOCX from the motion content.

        Converts TipTap HTML to structured DOCX using python-docx.
        Content starts directly with the document body (no header/metadata).

        Args:
            motion: The motion to export

        Returns:
            bytes: The DOCX file content
        """
        from docx import Document
        from docx.shared import Pt

        doc = Document()

        # Set default font
        style = doc.styles["Normal"]
        font = style.font
        font.name = "Arial"
        font.size = Pt(11)

        # Parse and convert HTML content directly (no title/metadata header)
        content = clean_editor_html(motion.content or "")
        self._html_to_docx(doc, content)

        # Save to bytes
        buffer = io.BytesIO()
        doc.save(buffer)
        return buffer.getvalue()

    def _html_to_docx(self, doc, html: str):
        """
        Convert cleaned HTML to DOCX paragraphs in document order.

        Processes elements sequentially to maintain the original document
        structure rather than grouping by type.
        """
        from html.parser import HTMLParser

        from docx.shared import Pt

        if not html:
            return

        class DocxHTMLConverter(HTMLParser):
            """Sequential HTML-to-DOCX converter."""

            def __init__(self, doc):
                super().__init__()
                self.doc = doc
                self._text_buf: list[str] = []
                self._tag_stack: list[str] = []
                self._list_type_stack: list[str] = []  # 'ul' or 'ol'
                self._in_heading = 0  # heading level (1-3) or 0
                self._in_blockquote = False
                self._bold = False
                self._italic = False
                self._underline = False
                self._strike = False
                self._indent_px = 0

            def _flush_text(self):
                """Return accumulated text and reset buffer."""
                text = "".join(self._text_buf).strip()
                self._text_buf.clear()
                return text

            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                tag_lower = tag.lower()
                self._tag_stack.append(tag_lower)

                if tag_lower in ("h1", "h2", "h3"):
                    self._flush_text()
                    self._in_heading = int(tag_lower[1])
                elif tag_lower == "blockquote":
                    self._flush_text()
                    self._in_blockquote = True
                elif tag_lower == "ul":
                    self._list_type_stack.append("ul")
                elif tag_lower == "ol":
                    self._list_type_stack.append("ol")
                elif tag_lower == "li":
                    self._flush_text()
                elif tag_lower == "p":
                    self._flush_text()
                    # Check for indent via margin-left style
                    style = attrs_dict.get("style", "")
                    self._indent_px = 0
                    if "margin-left" in style:
                        match = re.search(r"margin-left:\s*(\d+)", style)
                        if match:
                            self._indent_px = int(match.group(1))
                elif tag_lower == "br":
                    self._text_buf.append("\n")
                elif tag_lower == "hr":
                    self._flush_text()
                    # Add a thin horizontal line as paragraph
                    para = self.doc.add_paragraph()
                    para.paragraph_format.space_before = Pt(8)
                    para.paragraph_format.space_after = Pt(8)
                    run = para.add_run("_" * 60)
                    run.font.color.rgb = None  # default color
                    run.font.size = Pt(6)
                elif tag_lower == "strong" or tag_lower == "b":
                    self._bold = True
                elif tag_lower == "em" or tag_lower == "i":
                    self._italic = True
                elif tag_lower == "u":
                    self._underline = True
                elif tag_lower in ("s", "del", "strike"):
                    self._strike = True

            def handle_endtag(self, tag):
                tag_lower = tag.lower()

                if tag_lower in ("h1", "h2", "h3"):
                    text = self._flush_text()
                    if text:
                        self.doc.add_heading(text, level=self._in_heading)
                    self._in_heading = 0
                elif tag_lower == "blockquote":
                    text = self._flush_text()
                    if text:
                        para = self.doc.add_paragraph(text)
                        para.paragraph_format.left_indent = Pt(36)
                        for run in para.runs:
                            run.font.italic = True
                    self._in_blockquote = False
                elif tag_lower in ("ul", "ol"):
                    if self._list_type_stack:
                        self._list_type_stack.pop()
                elif tag_lower == "li":
                    text = self._flush_text()
                    if text:
                        list_type = self._list_type_stack[-1] if self._list_type_stack else "ul"
                        style = "List Number" if list_type == "ol" else "List Bullet"
                        para = self.doc.add_paragraph(text, style=style)
                        # Indent nested lists
                        depth = len(self._list_type_stack)
                        if depth > 1:
                            para.paragraph_format.left_indent = Pt(18 * (depth - 1))
                elif tag_lower == "p":
                    text = self._flush_text()
                    if text:
                        para = self.doc.add_paragraph(text)
                        if self._indent_px > 0:
                            para.paragraph_format.left_indent = Pt(self._indent_px * 0.75)
                    self._indent_px = 0
                elif tag_lower == "strong" or tag_lower == "b":
                    self._bold = False
                elif tag_lower == "em" or tag_lower == "i":
                    self._italic = False
                elif tag_lower == "u":
                    self._underline = False
                elif tag_lower in ("s", "del", "strike"):
                    self._strike = False

                if self._tag_stack and self._tag_stack[-1] == tag_lower:
                    self._tag_stack.pop()

            def handle_data(self, data):
                self._text_buf.append(data)

        converter = DocxHTMLConverter(doc)
        converter.feed(html)


# Singleton instance
motion_export_service = MotionExportService()

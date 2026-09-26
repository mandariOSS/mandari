# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame PDF-Bausteine (Issue #29).

Generalisiert die Briefkopf-/PDF-Mechanik aus apps/work/motions/ für die
Wiederverwendung in anderen Apps (Session: Einladungen, Niederschriften,
Beschlussauszüge; Work: Antrags-Export).

Bausteine:
- html_to_pdf: HTML → PDF (xhtml2pdf, Fallback: reportlab-Minimal-PDF)
- overlay_pdf_letterhead: erzeugtes PDF auf ein Briefkopf-PDF legen
"""

import io
import logging
import re
import time

from apps.common.metrics import PDF_DOCUMENTS, PDF_DURATION

logger = logging.getLogger(__name__)

# Standard-Ränder in mm (A4)
DEFAULT_MARGINS = {"top": 30, "right": 25, "bottom": 25, "left": 25}


def html_to_pdf(html_content: str) -> bytes:
    """
    HTML in ein PDF umwandeln (xhtml2pdf, Fallback: reportlab).

    Zentrale Stelle für Einladungen, Niederschriften, Beschlussauszüge, Exporte und
    Bescheinigungen – deshalb werden hier Anzahl und Dauer als Metrik erfasst (Issue #231).

    Args:
        html_content: vollständiges HTML-Dokument

    Returns:
        bytes: PDF-Inhalt
    """
    start = time.perf_counter()
    try:
        try:
            from xhtml2pdf import pisa

            result = io.BytesIO()
            pisa_status = pisa.CreatePDF(src=html_content, dest=result, encoding="UTF-8")
            if pisa_status.err:
                raise RuntimeError(f"PDF generation error: {pisa_status.err}")
            pdf = result.getvalue()
        except ImportError:
            pdf = simple_text_pdf(html_content)
    except Exception:
        PDF_DOCUMENTS.labels(result="failed").inc()
        raise
    PDF_DURATION.observe(time.perf_counter() - start)
    PDF_DOCUMENTS.labels(result="ok").inc()
    return pdf


def simple_text_pdf(html_content: str, margins: dict | None = None) -> bytes:
    """
    Fallback: schlichtes Text-PDF mit reportlab (HTML-Tags werden entfernt).
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    m = {**DEFAULT_MARGINS, **(margins or {})}
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=m["left"] * mm,
        rightMargin=m["right"] * mm,
        topMargin=m["top"] * mm,
        bottomMargin=m["bottom"] * mm,
    )

    styles = getSampleStyleSheet()
    text = re.sub(r"<[^>]+>", "", html_content)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")

    story = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            story.append(Paragraph(line, styles["Normal"]))
            story.append(Spacer(1, 6))

    doc.build(story)
    return buffer.getvalue()


def overlay_pdf_letterhead(pdf_content: bytes, letterhead_pdf_file) -> bytes:
    """
    Erzeugtes PDF seitenweise auf die erste Seite eines Briefkopf-PDFs legen.

    Args:
        pdf_content: das erzeugte Inhalts-PDF
        letterhead_pdf_file: dateiartiges Objekt des Briefkopf-PDFs

    Returns:
        bytes: zusammengeführtes PDF (bei Fehlern: Inhalts-PDF unverändert)
    """
    try:
        from copy import copy

        from pypdf import PdfReader, PdfWriter

        content_pdf = PdfReader(io.BytesIO(pdf_content))
        letterhead_pdf = PdfReader(letterhead_pdf_file)
        letterhead_page = letterhead_pdf.pages[0]

        output = PdfWriter()
        for content_page in content_pdf.pages:
            new_page = copy(letterhead_page)
            new_page.merge_page(content_page)
            output.add_page(new_page)

        result = io.BytesIO()
        output.write(result)
        return result.getvalue()
    except Exception as e:
        logger.warning(f"Letterhead merge error: {e}")
        return pdf_content

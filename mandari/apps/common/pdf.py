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
from typing import Any

from apps.common.metrics import PDF_DOCUMENTS, PDF_DURATION

logger = logging.getLogger(__name__)

# Standard-Ränder in mm (A4)
DEFAULT_MARGINS = {"top": 30, "right": 25, "bottom": 25, "left": 25}


#: Grenzen der Umwandlung. Dokumente mit eingebetteten Bildern (``data:``) dürfen groß sein, deshalb großzügiger
#: als die Vorgaben der Bibliothek; die Pixelgrenze je Bild bleibt bei deren Vorgabe.
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_RENDER_SECONDS = 300


def _resource_policy() -> Any:
    """
    Richtlinie der Bibliothek für Dateien und Adressen im HTML: nichts außerhalb des Dokuments.

    Das HTML enthält Text aus dem Editor. Ohne Richtlinie läse die Bibliothek für ``<img src>`` Dateien unterhalb
    des Arbeitsverzeichnisses (dort liegt die Medienablage aller Mandanten) und riefe fremde Server ab, beides
    während sie die prozessweite Sperre der Umwandlung hält. Deshalb: keine lokalen Dateien, keine Fernabrufe,
    dazu Grenzen für Bildgröße, Dokumentgröße und Dauer. Bilder stehen als ``data:``-Adresse im HTML; wer Dateien
    oder Bilder fremder Server braucht, bettet sie vorher ein (``apps.work.pdf_bilder``).
    """
    from xhtml2pdf.config.resources import ResourceAccessPolicy

    return ResourceAccessPolicy.server(
        base_dir=None,
        allow_remote=False,
        max_document_bytes=MAX_DOCUMENT_BYTES,
        max_total_bytes=MAX_TOTAL_BYTES,
        max_render_seconds=MAX_RENDER_SECONDS,
    )


def html_to_pdf(html_content: str) -> bytes:
    """
    HTML in ein PDF umwandeln (xhtml2pdf, Fallback: reportlab).

    Zentrale Stelle für Einladungen, Niederschriften, Beschlussauszüge, Exporte und
    Bescheinigungen – deshalb werden hier Anzahl und Dauer als Metrik erfasst (Issue #231).
    Dateien und fremde Adressen im HTML werden nicht gelesen (``_resource_policy``).

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
            pisa_status = pisa.CreatePDF(
                src=html_content, dest=result, encoding="UTF-8", resource_policy=_resource_policy()
            )
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

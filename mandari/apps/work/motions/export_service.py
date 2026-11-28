# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Export Service for Motions.

Handles generation of PDF and DOCX documents from motion content,
including support for letterheads (PDF backgrounds) and proper formatting.
TipTap editor HTML is cleaned before export (comment marks stripped etc.).
"""

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import escape

from apps.common import schriften

if TYPE_CHECKING:
    from .models import Motion, OrganizationLetterhead

logger = logging.getLogger(__name__)

#: Unterstützte Platzhalter im Signaturblock (und in Inhaltsvorlagen).
PLACEHOLDER_KEYS = ("organisation", "datum", "verantwortlich")


def build_placeholder_values(organization, responsible_name: str = "") -> dict:
    """Platzhalter-Werte für Vorlagen/Signaturblöcke einer Organisation."""
    return {
        "organisation": organization.name,
        "datum": timezone.localdate().strftime("%d.%m.%Y"),
        "verantwortlich": responsible_name,
    }


def apply_placeholders(text: str, values: dict) -> str:
    """Ersetzt {{ platzhalter }} in einem Text durch die übergebenen Werte."""
    if not text:
        return ""
    for key, value in values.items():
        text = re.sub(r"\{\{\s*" + key + r"\s*\}\}", lambda _m, v=value: v, text)
    return text


def replace_placeholders(text: str, motion: "Motion") -> str:
    """
    Ersetzt einfache Platzhalter ({{ organisation }}, {{ datum }},
    {{ verantwortlich }}) in Vorlagen-/Signaturtexten eines Dokuments.
    """
    if not text:
        return ""

    responsible = motion.responsible or motion.author
    responsible_name = ""
    if responsible:
        responsible_name = responsible.user.get_display_name()

    return apply_placeholders(text, build_placeholder_values(motion.organization, responsible_name))


def get_signature_text(motion: "Motion") -> str:
    """Signaturblock der Vorlage mit ersetzten Platzhaltern (Plaintext)."""
    if not motion.template or not motion.template.signature_block:
        return ""
    return replace_placeholders(motion.template.signature_block, motion)


def generated_letterhead_context(letterhead: "OrganizationLetterhead") -> dict:
    """
    Kontext für die HTML-Darstellung eines generierten Briefkopfs
    (Formular-Vorschau, Editor-Vorschau und PDF-Export nutzen dieselben Daten).
    """
    org = letterhead.organization
    logo = org.effective_logo if letterhead.header_logo_enabled else None

    logo_url = ""
    logo_path = ""
    if logo:
        try:
            logo_url = logo.url
            logo_path = logo.path
        except (ValueError, NotImplementedError):  # Kein File / kein FS-Storage
            logo_url = ""
            logo_path = ""

    return {
        "letterhead": letterhead,
        "org_name": org.name,
        "primary_color": org.effective_primary_color,
        "accent_enabled": letterhead.accent_color_enabled,
        "logo_url": logo_url,
        "logo_path": logo_path,
        "sender_line": letterhead.sender_line,
        "address_lines": [line for line in letterhead.address_block.splitlines() if line.strip()],
        "footer_lines": [line for line in letterhead.footer_text.splitlines() if line.strip()],
    }


class ExportTooLargeError(ValueError):
    """Der Inhalt lässt sich nicht als PDF ausgeben; ``str()`` ist eine feste Meldung für die Oberfläche."""


#: Größte Zahl an Tabellenfeldern eines Dokuments im PDF-Export. Die Umwandlung legt für jedes Feld des Rasters
#: einer Tabelle Einträge an (auch für überspannte und für aufgefüllte Felder) und läuft im Webprozess, der alle
#: Anfragen bedient. Eine Tabelle ohne verbundene Zellen zählt doppelt (Zellen und Raster): 20.000 entspricht
#: 500 Zeilen mit 20 Spalten.
MAX_PDF_TABLE_CELLS = 20_000
PDF_TABLES_TOO_LARGE = (
    "Die Tabellen dieses Dokuments sind für die Ausgabe als PDF zu groß. Bitte die Tabellen kürzen oder aufteilen."
)

_TABLE_PART_RE = re.compile(r"<(/?)(table|tr|td|th)\b([^<>]*)>", re.IGNORECASE)
_SPAN_ATTR_RE = re.compile(r'\b(colspan|rowspan)="(\d{1,5})"')
#: Startmarken und darin die Attribute, die nur der Editor braucht. Beide Muster beginnen mit einem festen Zeichen
#: und laufen je Marke einmal: Ein vorangestelltes ``\s+`` über den ganzen Inhalt brauchte bei langem Leerraum
#: quadratische Zeit.
_START_TAG_RE = re.compile(r"<[a-zA-Z][^<>]*>")
_EDITOR_ATTR_RE = re.compile(r'\s(data-[a-z-]+)="[^"]*"|\sclass="(?:comment-mark[^"]*|is-editor-empty)"')
_COMMENT_MARK_RE = re.compile(r"<(?:mark|span)\b[^<>]*\bdata-comment-id[^<>]*>(.*?)</(?:mark|span)>", flags=re.DOTALL)


def clean_editor_html(html: str, *, behalten: frozenset[str] = frozenset()) -> str:
    """
    Editor-HTML für den Export vorbereiten.

    Der gespeicherte Inhalt geht zuerst durch die Positivliste des Editors (``apps/work/sanitize.py``), auch
    Altbestand von vor einer Regel: Danach ist das HTML wohlgeformt, ``<`` und ``>`` stehen nur noch in Marken, und
    es gelten die heutigen Grenzen für Bilder und Tabellen. Anschließend entfallen Kommentar-Markierungen,
    ``data-``-Attribute und Klassen, die nur der Editor braucht. ``behalten`` nennt ``data-``-Attribute, die ein
    Export noch liest (Aufgabenlisten im Dokumentmodell).
    """
    if not html:
        return ""
    from apps.work.sanitize import sanitize_editor_html

    html = sanitize_editor_html(html)

    # Kommentar-Markierungen (mark/span) entfernen, ihr Text bleibt
    html = _COMMENT_MARK_RE.sub(r"\1", html)

    # Übrige data-Attribute und Editor-Klassen aus den Startmarken entfernen
    def ohne_editor_attribute(marke: re.Match[str]) -> str:
        return _EDITOR_ATTR_RE.sub(lambda a: a.group(0) if a.group(1) in behalten else "", marke.group(0))

    return _START_TAG_RE.sub(ohne_editor_attribute, html)


@dataclass
class _TableGrid:
    """Zählwerk für eine Tabelle: belegte Felder ihrer Zellen und das Raster aus Zeilen und breitester Zeile."""

    cells: int = 0
    rows: int = 0
    width: int = 0
    #: Breite der laufenden Zeile aus ihren eigenen Zellen
    row_width: int = 0
    #: Breite der Zellen der laufenden Zeile, die in folgende Zeilen reichen
    row_started: int = 0
    #: Breite, die Zellen früherer Zeilen in der laufenden Zeile belegen
    carried: int = 0
    #: Zeile, nach der eine Spannweite endet: ihre Breite
    ending: dict[int, int] = field(default_factory=dict)

    def add_cell(self, columns: int, rows: int) -> None:
        self.cells += columns * rows
        self.row_width += columns
        if rows > 1:
            self.row_started += columns
            last_row = self.rows + rows - 1
            self.ending[last_row] = self.ending.get(last_row, 0) + columns

    def close_row(self) -> None:
        self.width = max(self.width, self.row_width + self.carried)
        self.carried += self.row_started - self.ending.pop(self.rows, 0)
        self.rows += 1
        self.row_width = 0
        self.row_started = 0

    def total(self) -> int:
        if self.row_width:
            self.close_row()
        return self.cells + self.rows * self.width


def table_cell_count(html: str) -> int:
    """
    Obergrenze der Tabellenfelder, die die PDF-Umwandlung für bereinigtes Editor-HTML anlegt.

    Je Tabelle die Felder aller Zellen (Spalten mal Zeilen ihrer Spannweite) und dazu das Raster aus Zeilen mal
    breitester Zeile, denn kürzere Zeilen werden aufgefüllt. Zellen, die aus einer früheren Zeile herunterreichen,
    zählen zur Breite der Zeile. Verschachtelte Tabellen zählen einzeln.
    """
    total = 0
    tables: list[_TableGrid] = []
    for closing, name, attrs in _TABLE_PART_RE.findall(html):
        name = name.lower()
        if name == "table":
            if not closing:
                tables.append(_TableGrid())
            elif tables:
                total += tables.pop().total()
            continue
        if not tables:
            # Zeilen und Zellen ohne Tabelle: wie eine eigene Tabelle zählen
            tables.append(_TableGrid())
        if name == "tr":
            if closing:
                tables[-1].close_row()
        elif not closing:
            spans = {key: int(value) for key, value in _SPAN_ATTR_RE.findall(attrs)}
            tables[-1].add_cell(max(spans.get("colspan", 1), 1), max(spans.get("rowspan", 1), 1))
    return total + sum(table.total() for table in tables)


class MotionExportService:
    """Service for exporting motions to various formats."""

    # Default margins in mm
    MARGIN_TOP = 30
    MARGIN_BOTTOM = 25
    MARGIN_LEFT = 25
    MARGIN_RIGHT = 25

    def export_to_pdf(self, motion: "Motion") -> bytes:
        """
        PDF des Dokuments.

        Mit eingerichtetem Chromium-Dienst (``GOTENBERG_URL``) setzt dieselbe Satz-Kette wie die Seitenansicht des
        Editors die Seiten (Issue #1584); sonst, und wenn das scheitert, der bisherige Weg über xhtml2pdf.
        """
        from apps.common import pdf_chromium

        if pdf_chromium.verfuegbar():
            from .druck import DruckNichtMoeglichError, druck_dateien

            try:
                pdf_content = pdf_chromium.html_zu_pdf(druck_dateien(motion))
            except (pdf_chromium.ChromiumNichtVerfuegbarError, DruckNichtMoeglichError) as fehler:
                logger.warning("PDF über die Satz-Kette nicht möglich, bisheriger Weg: %s", type(fehler).__name__)
            else:
                letterhead = motion.letterhead
                if letterhead and not letterhead.is_generated and letterhead.pdf_file:
                    pdf_content = self._apply_letterhead(pdf_content, letterhead)
                return pdf_content

        # Generate HTML content
        html_content = self._render_html(motion)

        # Convert HTML to PDF
        pdf_content = self._html_to_pdf(html_content)

        # Apply PDF letterhead overlay (kind=pdf only)
        letterhead = motion.letterhead
        if letterhead and not letterhead.is_generated and letterhead.pdf_file:
            pdf_content = self._apply_letterhead(pdf_content, letterhead)

        return pdf_content

    def _pdf_margins(self, letterhead) -> dict:
        """Ränder (mm) für den PDF-Export: aus dem Briefkopf oder Defaults."""
        if letterhead:
            return {
                "top": letterhead.content_margin_top,
                "right": letterhead.content_margin_right,
                "bottom": letterhead.content_margin_bottom,
                "left": letterhead.content_margin_left,
            }
        return {
            "top": self.MARGIN_TOP,
            "right": self.MARGIN_RIGHT,
            "bottom": self.MARGIN_BOTTOM,
            "left": self.MARGIN_LEFT,
        }

    def _render_html(self, motion: "Motion") -> str:
        """Render the motion content as HTML for PDF generation."""
        # Clean editor-specific HTML before rendering
        content = clean_editor_html(motion.content or "")
        if table_cell_count(content) > MAX_PDF_TABLE_CELLS:
            raise ExportTooLargeError(PDF_TABLES_TOO_LARGE)

        letterhead = motion.letterhead
        margins = self._pdf_margins(letterhead)
        # A4: 210 x 297 mm — Frame-Geometrie für xhtml2pdf vorberechnen
        frame = {
            "content_left": margins["left"],
            "content_top": margins["top"],
            "content_width": 210 - margins["left"] - margins["right"],
            "content_height": 297 - margins["top"] - margins["bottom"],
            "footer_top": 297 - margins["bottom"] + 3,
            "footer_height": max(margins["bottom"] - 6, 8),
        }

        signature_text = get_signature_text(motion)

        context = {
            "title": motion.title,
            "content": content,
            "margins": margins,
            "frame": frame,
            # Eine Familie, die xhtml2pdf kennt, nach der Art der Schrift (Georgia, Tinos → Times New Roman)
            "font_family": schriften.fuer_xhtml2pdf(letterhead.font_family) if letterhead else "Helvetica",
            "font_size": letterhead.font_size if letterhead else 11,
            "signature_html": escape(signature_text).replace("\n", "<br>") if signature_text else "",
            "generated": bool(letterhead and letterhead.is_generated),
        }

        if letterhead and letterhead.is_generated:
            context.update(generated_letterhead_context(letterhead))
            if letterhead.header_logo_enabled:
                from apps.work.pdf_bilder import file_data_uri

                # Die Umwandlung liest keine Dateien: Das Logo steht eingebettet im HTML
                context["logo_src"] = file_data_uri(letterhead.organization.effective_logo)

        return render_to_string("work/motions/export/pdf_template.html", context)

    def _html_to_pdf(self, html_content: str) -> bytes:
        """Convert HTML to PDF (gemeinsamer Baustein in apps/common/pdf.py)."""
        from apps.common.pdf import html_to_pdf
        from apps.work.pdf_bilder import inline_remote_images

        # Die Umwandlung liest keine fremden Server: Bilder aus dem Text vorher abgesichert laden und einbetten
        return html_to_pdf(inline_remote_images(html_content))

    def _apply_letterhead(self, pdf_content: bytes, letterhead) -> bytes:
        """
        Overlay the PDF content on the letterhead background
        (gemeinsamer Baustein in apps/common/pdf.py).
        """
        from apps.common.pdf import overlay_pdf_letterhead

        # Dokumenteigenes Briefpapier eines treu übernommenen PDFs: jede Originalseite unter ihre Seite (#1600)
        seiten = (letterhead.satz or {}).get("hintergrund_seiten") if letterhead.dokument_id else None
        return overlay_pdf_letterhead(pdf_content, letterhead.pdf_file, seiten if isinstance(seiten, list) else None)

    def export_to_docx(self, motion: "Motion") -> bytes:
        """
        Word-Datei des Dokuments (Issue #1584, ``apps/work/motions/formate``): Zeichenformate, Schriften des Satzes unter
        ihrem Office-Namen, Listen, Tabellen, Bilder, Links und Seitenumbrüche; Kopf- und Fußzeile aus dem Briefpapier.
        """
        from .formate import exportieren

        return exportieren(motion, "docx")

    def _docx_apply_header(self, doc, section, letterhead) -> None:
        """
        Kopfzeile: Logo (falls vorhanden/aktiviert) + Absenderblock rechts,
        optional Akzentlinie und Absenderzeile. Gilt für beide Briefkopf-Arten;
        ohne gepflegte generated-Felder wird ein schlichter Kopf mit dem
        Organisationsnamen in der Primärfarbe gesetzt.
        """
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Mm, Pt

        org = letterhead.organization
        ctx = generated_letterhead_context(letterhead)
        primary_rgb = self._hex_to_rgbcolor(ctx["primary_color"])

        header = section.header
        header.is_linked_to_previous = False

        has_generated_fields = bool(ctx["logo_path"] or ctx["address_lines"] or ctx["sender_line"])

        if not has_generated_fields:
            # Schlichter Kopf: Organisationsname in Primärfarbe
            para = header.paragraphs[0]
            run = para.add_run(org.name)
            run.font.bold = True
            run.font.size = Pt(14)
            if primary_rgb is not None:
                run.font.color.rgb = primary_rgb
            return

        # Logo + Absenderblock nebeneinander (Tabelle mit 2 Spalten)
        usable_width = section.page_width - section.left_margin - section.right_margin
        table = header.add_table(rows=1, cols=2, width=usable_width)
        cells = table.rows[0].cells
        cells[0].width = usable_width // 2
        cells[1].width = usable_width - usable_width // 2

        logo_cell = cells[0].paragraphs[0]
        if ctx["logo_path"]:
            try:
                logo_run = logo_cell.add_run()
                logo_run.add_picture(ctx["logo_path"], height=Mm(15))
            except Exception as e:
                logger.warning(f"DOCX-Logo konnte nicht eingefügt werden: {e}")

        address_para = cells[1].paragraphs[0]
        address_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for index, line in enumerate(ctx["address_lines"]):
            if index:
                address_para.add_run().add_break()
            run = address_para.add_run(line)
            run.font.size = Pt(9)

        # Akzentlinie in Primärfarbe (Rahmen unter einem Leerabsatz)
        if letterhead.accent_color_enabled:
            accent_para = header.add_paragraph()
            self._docx_set_bottom_border(accent_para, ctx["primary_color"])

        # Absenderzeile klein
        if ctx["sender_line"]:
            sender_para = header.add_paragraph()
            run = sender_para.add_run(ctx["sender_line"])
            run.font.size = Pt(7)
            run.font.underline = True

    def _docx_apply_footer(self, section, letterhead) -> None:
        """Fußzeile: footer_text-Zeilen + Seitenzahl-Feld."""
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt

        ctx = generated_letterhead_context(letterhead)

        footer = section.footer
        footer.is_linked_to_previous = False

        para = footer.paragraphs[0]
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for index, line in enumerate(ctx["footer_lines"]):
            if index:
                para.add_run().add_break()
            run = para.add_run(line)
            run.font.size = Pt(8)

        page_para = footer.add_paragraph()
        page_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        prefix_run = page_para.add_run("Seite ")
        prefix_run.font.size = Pt(8)
        self._docx_add_page_number_field(page_para)

    @staticmethod
    def _docx_add_page_number_field(paragraph) -> None:
        """Fügt ein PAGE-Feld (dynamische Seitenzahl) in den Absatz ein."""
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), "PAGE")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "1"
        run.append(text)
        fld.append(run)
        paragraph._p.append(fld)

    @staticmethod
    def _docx_set_bottom_border(paragraph, hex_color: str) -> None:
        """Setzt eine untere Rahmenlinie (Akzentlinie) am Absatz."""
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn

        p_pr = paragraph._p.get_or_add_pPr()
        borders = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "12")
        bottom.set(qn("w:space"), "1")
        bottom.set(qn("w:color"), hex_color.lstrip("#"))
        borders.append(bottom)
        p_pr.append(borders)

    @staticmethod
    def _hex_to_rgbcolor(hex_color: str):
        """#RRGGBB → docx RGBColor (None bei ungültigem Wert)."""
        from docx.shared import RGBColor

        value = (hex_color or "").lstrip("#")
        if len(value) != 6:
            return None
        try:
            return RGBColor.from_string(value.upper())
        except ValueError:
            return None


# Singleton instance
motion_export_service = MotionExportService()

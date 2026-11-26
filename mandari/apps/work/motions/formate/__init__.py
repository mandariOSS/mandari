# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weitere Formate eines Dokuments (Issue #1584): Word (.docx), OpenDocument (.odt), Markdown (.md) und Text (.txt).

Alle lesen den bereinigten Inhalt einmal in das Dokumentmodell (``modell.py``) und schreiben ihn mit der Typografie
des Satzes: Grundschrift und -größe des Briefpapiers (Schriften des Satzes unter ihrem Office-Namen), Ränder, Zeichen-
formate, Listen, Tabellen, Bilder, Links und Seitenumbrüche. Das PDF entsteht über die Satz-Kette
(``export_service.export_to_pdf``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.common import schriften

from . import modell

if TYPE_CHECKING:
    from apps.work.motions.models import Motion


@dataclass(frozen=True)
class Exportformat:
    schluessel: str
    endung: str
    content_type: str
    name: str


#: Größte Zahl an Tabellenfeldern (Zählweise ``export_service.table_cell_count``: Zellen plus Raster) für Word, ODT,
#: Markdown und Text. Word legt je Feld XML an und läuft im Webprozess: gemessen rund 2,5 s für eine Tabelle mit
#: 110 × 100 Feldern (22.000 nach dieser Zählung), ODT ein Bruchteil davon. Höher als beim PDF (``MAX_PDF_TABLE_CELLS``),
#: weil diese Wege schneller sind.
MAX_TABELLENFELDER = 40_000
#: Feste Meldung, wenn die Tabellen eines Dokuments das Raster der Exporte sprengen
TABELLEN_ZU_GROSS = (
    "Die Tabellen dieses Dokuments sind für den Export zu groß. Bitte die Tabellen kürzen oder aufteilen."
)
#: ``data-``-Attribute, die das Dokumentmodell liest: Aufgabenlisten und ihr Stand
_MODELL_ATTRIBUTE = frozenset({"data-type", "data-checked"})

FORMATE: dict[str, Exportformat] = {
    f.schluessel: f
    for f in (
        Exportformat("docx", "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "Word"),
        Exportformat("odt", "odt", "application/vnd.oasis.opendocument.text", "OpenDocument"),
        Exportformat("md", "md", "text/markdown; charset=utf-8", "Markdown"),
        Exportformat("txt", "txt", "text/plain; charset=utf-8", "Text"),
    )
}


def _vorbereiten(motion: Motion) -> tuple[list[modell.Block], schriften.Schrift, float, str]:
    from apps.work.motions.export_service import (
        ExportTooLargeError,
        clean_editor_html,
        get_signature_text,
        table_cell_count,
    )
    from apps.work.pdf_bilder import inline_remote_images

    letterhead = motion.letterhead
    schrift = schriften.aufloesen(letterhead.font_family if letterhead else None)
    groesse = float(letterhead.font_size if letterhead and letterhead.font_size else 11)
    groesse = max(schriften.GROESSE_MIN, min(groesse, schriften.GROESSE_MAX))
    inhalt = clean_editor_html(motion.content or "", behalten=_MODELL_ATTRIBUTE)
    # Word und ODT legen jedes Feld des Rasters an, Markdown und Text füllen es auf: Größe vorher begrenzen
    if table_cell_count(inhalt) > MAX_TABELLENFELDER:
        raise ExportTooLargeError(TABELLEN_ZU_GROSS)
    if "<img" in inhalt:
        # Bilder fremder Server abgesichert laden und einbetten (wie beim PDF)
        inhalt = inline_remote_images(inhalt)
    return modell.lesen(inhalt, groesse), schrift, groesse, get_signature_text(motion)


def exportieren(motion: Motion, format_schluessel: str) -> bytes:
    """
    Datei im gewünschten Format; ``KeyError`` bei unbekanntem Format, ``ExportTooLargeError`` (Meldung
    ``TABELLEN_ZU_GROSS``) bei zu großen Tabellen.
    """
    from apps.work.motions.export_service import generated_letterhead_context, motion_export_service

    exportformat = FORMATE[format_schluessel]
    bloecke, schrift, groesse, signatur = _vorbereiten(motion)
    letterhead = motion.letterhead
    # Office-Name der Grundschrift; früher gespeicherte Office-Namen (Georgia) bleiben erhalten
    schriftname = schriften.office_name(letterhead.font_family) if letterhead else schrift.fuer_office
    raender = motion_export_service._pdf_margins(letterhead)

    if exportformat.schluessel == "docx":
        from .docx import docx_schreiben

        def kopf_und_fuss(doc, section) -> None:  # type: ignore[no-untyped-def]
            if letterhead:
                motion_export_service._docx_apply_header(doc, section, letterhead)
                motion_export_service._docx_apply_footer(section, letterhead)

        return docx_schreiben(
            bloecke,
            schrift=schrift,
            groesse=groesse,
            raender=raender,
            kopf_und_fuss=kopf_und_fuss,
            signatur=signatur,
            schriftname=schriftname,
        )
    if exportformat.schluessel == "odt":
        from .odt import odt_schreiben

        generiert = bool(letterhead and letterhead.is_generated)
        fuss = generated_letterhead_context(letterhead)["footer_lines"] if letterhead and generiert else []
        return odt_schreiben(
            bloecke,
            schrift=schrift,
            groesse=groesse,
            raender=raender,
            titel=motion.title,
            fuss_zeilen=fuss,
            seitenzahlen=generiert,
            signatur=signatur,
            schriftname=schriftname,
        )
    from .text import markdown_schreiben, text_schreiben

    if exportformat.schluessel == "md":
        return markdown_schreiben(bloecke, titel=motion.title, signatur=signatur).encode("utf-8")
    return text_schreiben(bloecke, signatur=signatur).encode("utf-8")

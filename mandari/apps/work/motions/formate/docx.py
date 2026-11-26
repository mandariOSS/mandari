# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Word-Export eines Dokuments aus dem Dokumentmodell (Issue #1584).

Gleiche Typografie wie Editor und PDF (``static/css/dokument-satz.css``): Grundschrift und -größe des Briefpapiers,
Zeilenabstand 1,5 (mindestens), Abstände nach den Absätzen, Überschriften 1,55/1,27/1,09 der Grundgröße. Schriften
stehen mit ihrem Office-Namen in der Datei (Arimo → Arial, Carlito → Calibri …): Word setzt sie mit dem Original,
das dieselben Zeichenbreiten hat.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Mm, Pt, RGBColor

from apps.common import schriften

from .modell import Absatz, Bild, Block, Format, Seitenumbruch, Tabelle, Trennlinie

if TYPE_CHECKING:
    from docx.document import Document as DocumentObject

ZEILENABSTAND = 1.5
ABSTAND_NACH = 0.7
UEBERSCHRIFTEN = {"h1": (1.55, 0.55, 0.3), "h2": (1.27, 0.4, 0.45), "h3": (1.09, 0.3, 0.35)}
AUSRICHTUNG = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "start": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "end": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}
GRAU = "9ca3af"


def _rgb(farbe: str) -> RGBColor:
    return RGBColor.from_string(farbe.lstrip("#").upper())


def _schriftname(schluessel: str | None) -> str | None:
    schrift = schriften.schrift(schluessel) if schluessel else None
    return schrift.fuer_office if schrift else None


def _schrift_setzen(run_oder_stil: Any, name: str) -> None:
    """Schriftname für alle Schriftsysteme (sonst nimmt Word für Umlaute u. Ä. womöglich eine andere)."""
    run_oder_stil.font.name = name
    rpr = run_oder_stil.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    for attribut in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attribut), name)


def _rahmen(absatz: Any, seite: str, farbe: str, staerke: int, abstand: int = 4) -> None:
    ppr = absatz._p.get_or_add_pPr()
    rahmen = ppr.find(qn("w:pBdr"))
    if rahmen is None:
        rahmen = OxmlElement("w:pBdr")
        ppr.append(rahmen)
    linie = OxmlElement(f"w:{seite}")
    linie.set(qn("w:val"), "single")
    linie.set(qn("w:sz"), str(staerke))
    linie.set(qn("w:space"), str(abstand))
    linie.set(qn("w:color"), farbe)
    rahmen.append(linie)


def _schattierung(element: Any, farbe: str) -> None:
    eigenschaften = element.get_or_add_rPr() if hasattr(element, "get_or_add_rPr") else element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), farbe.lstrip("#").upper())
    eigenschaften.append(shd)


#: Innenabstand einer Tabellenzelle links und rechts in Word (je 0,08 Zoll)
ZELLRAND = 2 * 73152


class _Schreiber:
    def __init__(self, doc: DocumentObject, groesse: float, inhaltsbreite: int) -> None:
        self.doc = doc
        self.groesse = groesse
        self.inhaltsbreite = inhaltsbreite
        #: Nummerierung je nummerierter Liste (Stil, Liste) → numId
        self.nummern: dict[tuple[str, int], int] = {}

    def _nummerierung(self, stil: str, liste_nr: int, start: int) -> int:
        """
        Eigene Nummerierung je Liste mit ihrem Startwert: Über den Stil allein zählt Word alle Listen des Dokuments
        durch und beginnt nie bei einem anderen Wert als 1.
        """
        schluessel = (stil, liste_nr)
        if schluessel not in self.nummern:
            nummerierung = self.doc.part.numbering_part.element
            vorlage = nummerierung.num_having_numId(self.doc.styles[stil].element.pPr.numPr.numId.val)
            num = nummerierung.add_num(vorlage.abstractNumId.val)
            num.add_lvlOverride(ilvl=0).add_startOverride(max(1, start))
            self.nummern[schluessel] = num.numId
        return self.nummern[schluessel]

    # --- Läufe ----------------------------------------------------------------------------------------------------

    def _lauf(self, absatz: Any, text: str, f: Format) -> Any:
        run = absatz.add_run(text)
        run.bold = f.fett or None
        run.italic = f.kursiv or None
        run.underline = f.unterstrichen or None
        if f.durchgestrichen:
            run.font.strike = True
        if f.hoch:
            run.font.superscript = True
        if f.tief:
            run.font.subscript = True
        name = _schriftname(f.schrift) or ("Courier New" if f.code else None)
        if name:
            _schrift_setzen(run, name)
        if f.groesse:
            run.font.size = Pt(f.groesse)
        if f.farbe:
            run.font.color.rgb = _rgb(f.farbe)
        if f.hervorhebung:
            _schattierung(run._r, f.hervorhebung)
        return run

    def _link(self, absatz: Any, text: str, f: Format) -> None:
        """Hyperlink (python-docx hat dafür keine Funktion): Beziehung und w:hyperlink mit einem Lauf darin."""
        assert f.link is not None
        rid = absatz.part.relate_to(
            f.link, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True
        )
        link = OxmlElement("w:hyperlink")
        link.set(qn("r:id"), rid)
        run = self._lauf(absatz, text, f)
        run.font.color.rgb = _rgb(f.farbe or "#1d4ed8")
        run.underline = True
        link.append(run._r)
        absatz._p.append(link)

    def _laeufe(self, absatz: Any, quelle: Absatz) -> None:
        for lauf in quelle.laeufe:
            if lauf.umbruch:
                absatz.add_run().add_break(WD_BREAK.LINE)
            elif lauf.format.link and lauf.format.link.startswith(("http://", "https://", "mailto:", "tel:")):
                self._link(absatz, lauf.text, lauf.format)
            else:
                self._lauf(absatz, lauf.text, lauf.format)

    # --- Blöcke ---------------------------------------------------------------------------------------------------

    def absatz(self, quelle: Absatz, ziel: Any = None) -> Any:
        stil = None
        if quelle.art in UEBERSCHRIFTEN:
            stil = f"Heading {quelle.art[1]}"
        elif quelle.liste and not quelle.fortsetzung:
            art, ebene = quelle.liste
            if art != "aufgaben":
                basis = "List Number" if art == "zahlen" else "List Bullet"
                stil = basis if ebene == 0 else f"{basis} {min(ebene + 1, 3)}"
        absatz = (ziel or self.doc).add_paragraph(style=stil)
        if stil and quelle.liste and quelle.liste[0] == "zahlen" and stil.startswith("List Number"):
            num_pr = absatz._p.get_or_add_pPr().get_or_add_numPr()
            num_pr.get_or_add_numId().val = self._nummerierung(stil, quelle.liste_nr, quelle.nummer or 1)
            num_pr.get_or_add_ilvl().val = 0
        if quelle.liste and quelle.liste[0] == "aufgaben" and not quelle.fortsetzung:
            self._lauf(absatz, "☑ " if quelle.erledigt else "☐ ", Format())
        self._laeufe(absatz, quelle)
        pf = absatz.paragraph_format
        if quelle.ausrichtung in AUSRICHTUNG:
            absatz.alignment = AUSRICHTUNG[quelle.ausrichtung]
        einzug = quelle.einzug
        if quelle.liste and (quelle.fortsetzung or quelle.liste[0] == "aufgaben"):
            einzug += 18 * (quelle.liste[1] + 1)
        if quelle.art == "zitat":
            einzug += 0.9 * self.groesse
            _rahmen(absatz, "left", GRAU, 18, 8)
            for run in absatz.runs:
                run.font.color.rgb = run.font.color.rgb or _rgb("#374151")
        if quelle.art == "code":
            for run in absatz.runs:
                _schrift_setzen(run, "Courier New")
        if einzug:
            pf.left_indent = Pt(einzug)
        if quelle.liste:
            pf.space_after = Pt(0.2 * self.groesse)
        return absatz

    def bild(self, bild: Bild, ziel: Any = None, hoechstbreite: int | None = None) -> Any:
        grenze = max(hoechstbreite or self.inhaltsbreite, 9525)
        breite = Emu(min(int(bild.breite * 9525), grenze))  # 1 px = 9525 EMU (96 dpi)
        absatz = (ziel or self.doc).add_paragraph()
        absatz.add_run().add_picture(io.BytesIO(bild.daten), width=breite)
        return absatz

    def trennlinie(self) -> None:
        absatz = self.doc.add_paragraph()
        _rahmen(absatz, "bottom", GRAU, 6, 1)

    def tabelle(self, tabelle: Tabelle) -> None:
        spalten, plaetze = tabelle.raster()
        if not spalten:
            return
        tab = self.doc.add_table(rows=len(tabelle.zeilen), cols=spalten)
        tab.style = "Table Grid"
        tab.alignment = WD_TABLE_ALIGNMENT.LEFT
        tab.autofit = False
        breiten = (
            tabelle.breiten[:spalten] if len(tabelle.breiten) >= spalten and all(tabelle.breiten[:spalten]) else []
        )
        summe = sum(breiten) or spalten
        spaltenbreiten = [
            Emu(int(self.inhaltsbreite * ((breiten[i] / summe) if breiten else 1 / spalten))) for i in range(spalten)
        ]
        for i, spalte in enumerate(tab.columns):
            spalte.width = spaltenbreiten[i]
        # Alle Felder einmal holen: Table.cell() zählt bei jedem Aufruf alle Zellen neu auf (quadratische Laufzeit,
        # bei 10.000 Feldern Minuten). Vor dem ersten Verbinden ist das Raster regelmäßig: Feld (z, s) = z·spalten + s.
        felder = list(tab._cells)
        # Auch die Breite jeder Zelle (w:tcW): add_table legt sie gleich breit an, Word läse sonst Widersprüche
        for k, feld in enumerate(felder):
            feld.width = spaltenbreiten[k % spalten]
        for reihe in plaetze:
            for platz in reihe:
                # Zellen überlappen nicht (Tabelle.raster): Beide Ecken sind noch keiner früheren Verbindung zugeschlagen
                ziel = felder[platz.zeile * spalten + platz.spalte]
                ende_z = platz.zeile + platz.zeilen - 1
                ende_s = platz.spalte + platz.spalten - 1
                if (ende_z, ende_s) != (platz.zeile, platz.spalte):
                    ziel = ziel.merge(felder[ende_z * spalten + ende_s])
                # Leere Absätze der (verbundenen) Zellen entfernen, dann den Inhalt setzen
                for leer in list(ziel.paragraphs):
                    leer._p.getparent().remove(leer._p)
                zellbreite = sum(spaltenbreiten[platz.spalte : ende_s + 1]) - ZELLRAND
                for block in platz.zelle.bloecke or [Absatz()]:
                    if isinstance(block, Bild):
                        neu = self.bild(block, ziel, zellbreite)
                    else:
                        neu = self.absatz(block, ziel)
                        if platz.zelle.kopf:
                            for run in neu.runs:
                                run.bold = True
                    neu.paragraph_format.space_after = Pt(0)
                if platz.zelle.kopf:
                    _schattierung(ziel._tc, "#f3f4f6")
        self.doc.add_paragraph().paragraph_format.space_after = Pt(0)

    def bloecke(self, bloecke: list[Block]) -> None:
        for block in bloecke:
            if isinstance(block, Absatz):
                self.absatz(block)
            elif isinstance(block, Bild):
                self.bild(block)
            elif isinstance(block, Trennlinie):
                self.trennlinie()
            elif isinstance(block, Seitenumbruch):
                self.doc.add_page_break()  # type: ignore[no-untyped-call]
            elif isinstance(block, Tabelle):
                self.tabelle(block)


def _stile(doc: DocumentObject, schriftname: str, groesse: float) -> None:
    """Grundschrift, Zeilenabstand und Überschriften wie im Satz (schwarz, Schrift des Dokuments)."""
    normal = doc.styles["Normal"]
    _schrift_setzen(normal, schriftname)
    normal.font.size = Pt(groesse)
    pf = normal.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
    pf.line_spacing = Pt(groesse * ZEILENABSTAND)
    pf.space_before = Pt(0)
    pf.space_after = Pt(groesse * ABSTAND_NACH)
    for art, (faktor, nach, vor) in UEBERSCHRIFTEN.items():
        stil = doc.styles[f"Heading {art[1]}"]
        _schrift_setzen(stil, schriftname)
        stil.font.size = Pt(round(groesse * faktor, 1))
        stil.font.bold = True
        stil.font.italic = False
        stil.font.color.rgb = RGBColor(0x11, 0x18, 0x27)
        spf = stil.paragraph_format
        spf.space_before = Pt(groesse * faktor * vor)
        spf.space_after = Pt(groesse * faktor * nach)
        spf.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
        spf.line_spacing = Pt(groesse * faktor * 1.25)
    for name in ("List Bullet", "List Bullet 2", "List Bullet 3", "List Number", "List Number 2", "List Number 3"):
        if name in [s.name for s in doc.styles]:
            _schrift_setzen(doc.styles[name], schriftname)


def docx_schreiben(
    bloecke: list[Block],
    *,
    schrift: schriften.Schrift,
    groesse: float,
    raender: dict[str, float],
    kopf_und_fuss: Any = None,
    signatur: str = "",
    schriftname: str | None = None,
) -> bytes:
    """Word-Datei aus den Blöcken. ``kopf_und_fuss(doc, section)`` setzt Kopf- und Fußzeile (Briefpapier)."""
    doc = Document()
    section = doc.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.top_margin = Mm(raender["top"])
    section.right_margin = Mm(raender["right"])
    section.bottom_margin = Mm(raender["bottom"])
    section.left_margin = Mm(raender["left"])
    # Grundschrift unter ihrem Office-Namen; ein früher gespeicherter Office-Name (Georgia) bleibt
    _stile(doc, schriftname or schrift.fuer_office, groesse)
    if kopf_und_fuss is not None:
        kopf_und_fuss(doc, section)
    inhaltsbreite = int(section.page_width - section.left_margin - section.right_margin)
    schreiber = _Schreiber(doc, groesse, inhaltsbreite)
    schreiber.bloecke(bloecke)
    if signatur:
        doc.add_paragraph()
        for zeile in signatur.splitlines():
            doc.add_paragraph(zeile)
    puffer = io.BytesIO()
    doc.save(puffer)
    return puffer.getvalue()

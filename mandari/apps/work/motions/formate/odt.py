# SPDX-License-Identifier: AGPL-3.0-or-later
"""
OpenDocument-Text (ODT) aus dem Dokumentmodell (Issue #1584).

Offenes Format der Verwaltung (Deutschland-Stack, ODF): LibreOffice, Collabora und Word öffnen die Datei. Ohne eigene
Bibliothek: Das Paket besteht aus ``mimetype`` (unkomprimiert zuerst), ``content.xml``, ``styles.xml``, ``meta.xml``,
``META-INF/manifest.xml`` und den Bildern unter ``Pictures/``.

Typografie wie Editor und PDF: Grundschrift (Office-Name der Schrift des Satzes) und -größe, Zeilenabstand mindestens
1,5-fach, Abstände nach den Absätzen, Überschriften 1,55/1,27/1,09 der Grundgröße, Seitenränder des Briefpapiers. Beim
generierten Briefkopf stehen Fußzeilen und „Seite x von y“ in der Fußzeile der Seite.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast
from xml.sax.saxutils import escape, quoteattr

from django.utils import timezone

from apps.common import schriften

from .modell import Absatz, Bild, Block, Format, Lauf, Seitenumbruch, Tabelle, Trennlinie

ZEILENABSTAND = 1.5
ABSTAND_NACH = 0.7
UEBERSCHRIFTEN = {"h1": (1.55, 0.55, 0.3), "h2": (1.27, 0.4, 0.45), "h3": (1.09, 0.3, 0.35)}
AUSRICHTUNG = {
    "left": "start",
    "start": "start",
    "center": "center",
    "right": "end",
    "end": "end",
    "justify": "justify",
}
NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:loext="urn:org:documentfoundation:names:experimental:office:xmlns:loext:1.0" '
    'office:version="1.3"'
)


def _pt(wert: float) -> str:
    return f"{wert:.2f}pt"


def _text(text: str) -> str:
    """Text mit Leerzeichenfolgen, Tabulatoren und Zeilenumbrüchen (Codeblöcke) als ODF-Elemente."""
    teile: list[str] = []
    leer = 0
    for zeichen in text:
        if zeichen == " ":
            leer += 1
            continue
        if leer:
            teile.append(" " + (f'<text:s text:c="{leer - 1}"/>' if leer > 1 else ""))
            leer = 0
        if zeichen == "\t":
            teile.append("<text:tab/>")
        elif zeichen == "\n":
            # ODF fasst Leerraum zusammen: Ein Zeilenumbruch im Text wäre sonst ein Leerzeichen
            teile.append("<text:line-break/>")
        else:
            teile.append(escape(zeichen))
    if leer:
        teile.append(" " + (f'<text:s text:c="{leer - 1}"/>' if leer > 1 else ""))
    return "".join(teile)


@dataclass
class _Stile:
    """Automatische Stile: je Zeichenformat und je Absatzvariante einer, mit festem Namen."""

    groesse: float
    text: dict[tuple[Any, ...], str] = field(default_factory=dict)
    absatz: dict[tuple[Any, ...], str] = field(default_factory=dict)
    xml: list[str] = field(default_factory=list)
    schriften: set[str] = field(default_factory=set)

    def zeichen(self, f: Format) -> str | None:
        schluessel = (
            f.fett,
            f.kursiv,
            f.unterstrichen or bool(f.link),
            f.durchgestrichen,
            f.hoch,
            f.tief,
            f.code,
            f.farbe or ("#1d4ed8" if f.link else None),
            f.hervorhebung,
            f.schrift,
            f.groesse,
        )
        if not any(schluessel):
            return None
        if schluessel in self.text:
            return self.text[schluessel]
        name = f"T{len(self.text) + 1}"
        self.text[schluessel] = name
        fett, kursiv, unter, durch, hoch, tief, code, farbe, hervor, schrift, groesse = schluessel
        e = []
        if fett:
            e.append('fo:font-weight="bold" style:font-weight-complex="bold"')
        if kursiv:
            e.append('fo:font-style="italic" style:font-style-complex="italic"')
        if unter:
            e.append(
                'style:text-underline-style="solid" style:text-underline-width="auto" style:text-underline-color="font-color"'
            )
        if durch:
            e.append('style:text-line-through-style="solid" style:text-line-through-type="single"')
        if hoch:
            e.append('style:text-position="super 58%"')
        if tief:
            e.append('style:text-position="sub 58%"')
        if farbe:
            e.append(f'fo:color="{farbe}"')
        if hervor:
            e.append(f'fo:background-color="{hervor}"')
        name_schrift = "Courier New" if code else None
        if schrift:
            eintrag = schriften.schrift(schrift)
            name_schrift = eintrag.fuer_office if eintrag else name_schrift
        if name_schrift:
            self.schriften.add(name_schrift)
            e.append(f"style:font-name={quoteattr(name_schrift)}")
        if groesse:
            e.append(f'fo:font-size="{_pt(groesse)}" style:font-size-complex="{_pt(groesse)}"')
        self.xml.append(
            f'<style:style style:name="{name}" style:family="text"><style:text-properties {" ".join(e)}/></style:style>'
        )
        return name

    def absatz_stil(
        self,
        eltern: str,
        ausrichtung: str | None,
        einzug: float,
        umbruch: bool,
        zitat: bool,
        rand: bool,
        nach: float | None,
    ) -> str:
        schluessel = (eltern, ausrichtung, round(einzug, 2), umbruch, zitat, rand, nach)
        if schluessel == (eltern, None, 0.0, False, False, False, None):
            return eltern
        if schluessel in self.absatz:
            return self.absatz[schluessel]
        name = f"P{len(self.absatz) + 1}"
        self.absatz[schluessel] = name
        e = []
        if ausrichtung in AUSRICHTUNG:
            e.append(f'fo:text-align="{AUSRICHTUNG[ausrichtung]}"')
        if einzug or zitat:
            e.append(f'fo:margin-left="{_pt(einzug + (0.9 * self.groesse if zitat else 0))}"')
        if umbruch:
            e.append('fo:break-before="page"')
        if zitat:
            e.append('fo:border-left="2.25pt solid #9ca3af" fo:padding-left="6pt"')
        if rand:
            e.append('fo:border-bottom="0.75pt solid #9ca3af" fo:padding-bottom="2pt"')
        if nach is not None:
            e.append(f'fo:margin-bottom="{_pt(nach)}"')
        text = '<style:text-properties fo:color="#374151"/>' if zitat else ""
        self.xml.append(
            f'<style:style style:name="{name}" style:family="paragraph" style:parent-style-name="{eltern}">'
            f"<style:paragraph-properties {' '.join(e)}/>{text}</style:style>"
        )
        return name


class _Schreiber:
    def __init__(self, groesse: float, inhaltsbreite_pt: float) -> None:
        self.groesse = groesse
        self.inhaltsbreite = inhaltsbreite_pt
        self.stile = _Stile(groesse)
        self.bilder: list[tuple[str, bytes]] = []
        self.tabellen = 0
        self.umbruch_offen = False

    def _laeufe(self, laeufe: list[Lauf]) -> str:
        teile = []
        for lauf in laeufe:
            if lauf.umbruch:
                teile.append("<text:line-break/>")
                continue
            inhalt = _text(lauf.text)
            stil = self.stile.zeichen(lauf.format)
            if stil:
                inhalt = f'<text:span text:style-name="{stil}">{inhalt}</text:span>'
            if lauf.format.link and lauf.format.link.startswith(("http://", "https://", "mailto:", "tel:")):
                inhalt = f'<text:a xlink:type="simple" xlink:href={quoteattr(lauf.format.link)}>{inhalt}</text:a>'
            teile.append(inhalt)
        return "".join(teile)

    def _umbruch_nehmen(self) -> bool:
        """Steht ein Seitenumbruch aus, trägt ihn der nächste Block (Absatz, Bild, Trennlinie oder Tabelle)."""
        umbruch, self.umbruch_offen = self.umbruch_offen, False
        return umbruch

    def absatz(self, a: Absatz, *, in_zelle: bool = False) -> str:
        umbruch = False if in_zelle else self._umbruch_nehmen()
        vorsatz = ""
        if a.liste and a.liste[0] == "aufgaben" and not a.fortsetzung:
            vorsatz = "☑ " if a.erledigt else "☐ "
        inhalt = _text(vorsatz) + self._laeufe(a.laeufe)
        nach = 0.0 if in_zelle else (0.2 * self.groesse if a.liste else None)
        if a.art in UEBERSCHRIFTEN:
            stil = self.stile.absatz_stil(
                f"Heading_20_{a.art[1]}", a.ausrichtung, a.einzug, umbruch, False, False, None
            )
            return f'<text:h text:style-name="{stil}" text:outline-level="{a.art[1]}">{inhalt}</text:h>'
        einzug = a.einzug
        if a.liste and a.liste[0] == "aufgaben":
            # Aufgaben stehen als eingerückte Absätze da (Kästchen im Text); andere Punkte rückt ihre Liste ein
            einzug += 18 * (a.liste[1] + 1)
        eltern = "Code" if a.art == "code" else "Standard"
        stil = self.stile.absatz_stil(eltern, a.ausrichtung, einzug, umbruch, a.art == "zitat", False, nach)
        return f'<text:p text:style-name="{stil}">{inhalt}</text:p>'

    def liste(self, punkte: list[Absatz], *, in_zelle: bool = False) -> str:
        """
        Punkte einer Liste samt Unterlisten als verschachtelte ``text:list``: Jede Liste trägt den Stil ihrer Art
        (Punkte oder Zahlen), weitere Absätze bleiben im Listenpunkt, und eine nummerierte Liste beginnt mit ihrer
        ersten Nummer (``start`` im Editor).
        """
        teile: list[str] = []
        offen: list[bool] = []  # je offener Liste: Listenpunkt offen?
        for a in punkte:
            assert a.liste is not None
            art, ebene = a.liste
            inhalt = self.absatz(a, in_zelle=in_zelle)
            while len(offen) > ebene + 1:
                teile.append("</text:list-item></text:list>" if offen.pop() else "</text:list>")
            if a.fortsetzung and len(offen) == ebene + 1 and offen[-1]:
                # Weiterer Absatz desselben Punkts (auch nach seiner Unterliste)
                teile.append(inhalt)
                continue
            while len(offen) < ebene + 1:
                if offen and not offen[-1]:
                    teile.append("<text:list-item>")
                    offen[-1] = True
                teile.append(f'<text:list text:style-name="{"LZahlen" if art == "zahlen" else "LPunkte"}">')
                offen.append(False)
            if offen[-1]:
                teile.append("</text:list-item>")
            start = ""
            if art == "zahlen" and a.listenbeginn and a.nummer and a.nummer != 1:
                start = f' text:start-value="{a.nummer}"'
            teile.append(f"<text:list-item{start}>{inhalt}")
            offen[-1] = True
        while offen:
            teile.append("</text:list-item></text:list>" if offen.pop() else "</text:list>")
        return "".join(teile)

    def bild(self, b: Bild, *, in_zelle: bool = False, hoechstbreite: float | None = None) -> str:
        umbruch = False if in_zelle else self._umbruch_nehmen()
        nummer = len(self.bilder) + 1
        datei = f"Pictures/bild{nummer}.{'jpg' if b.art == 'jpeg' else b.art}"
        self.bilder.append((datei, b.daten))
        breite = min(b.breite * 0.75, max(hoechstbreite or self.inhaltsbreite, 1))
        hoehe = b.hoehe * breite / max(b.breite * 0.75, 1)
        titel = f"<svg:title>{escape(b.alt)}</svg:title>" if b.alt else ""
        stil = self.stile.absatz_stil("Standard", None, 0, umbruch, False, False, 0.0 if in_zelle else None)
        return (
            f'<text:p text:style-name="{stil}"><draw:frame draw:name="Bild{nummer}" text:anchor-type="as-char" '
            f'svg:width="{_pt(breite)}" svg:height="{_pt(hoehe)}"><draw:image xlink:href="{datei}" xlink:type="simple" '
            f'xlink:show="embed" xlink:actuate="onLoad"/>{titel}</draw:frame></text:p>'
        )

    def tabelle(self, t: Tabelle) -> str:
        spalten, plaetze = t.raster()
        if not spalten:
            return ""
        umbruch = self._umbruch_nehmen()
        self.tabellen += 1
        name = f"Tabelle{self.tabellen}"
        breiten = t.breiten[:spalten] if len(t.breiten) >= spalten and all(t.breiten[:spalten]) else [1] * spalten
        summe = sum(breiten)
        spaltenbreiten = [self.inhaltsbreite * breite / summe for breite in breiten]
        teile = [f'<table:table table:name="{name}" table:style-name="{name}">']
        self.stile.xml.append(
            f'<style:style style:name="{name}" style:family="table"><style:table-properties '
            f'style:width="{_pt(self.inhaltsbreite)}" table:align="left"'
            f"{' fo:break-before=' + chr(34) + 'page' + chr(34) if umbruch else ''}/></style:style>"
        )
        for i, breite_pt in enumerate(spaltenbreiten):
            spaltenstil = f"{name}.S{i + 1}"
            self.stile.xml.append(
                f'<style:style style:name="{spaltenstil}" style:family="table-column"><style:table-column-properties '
                f'style:column-width="{_pt(breite_pt)}"/></style:style>'
            )
            teile.append(f'<table:table-column table:style-name="{spaltenstil}"/>')
        anfaenge = {(platz.zeile, platz.spalte): platz for reihe in plaetze for platz in reihe}
        belegt = {
            (platz.zeile + dz, platz.spalte + ds)
            for platz in anfaenge.values()
            for dz in range(platz.zeilen)
            for ds in range(platz.spalten)
        }
        for z in range(len(t.zeilen)):
            teile.append("<table:table-row>")
            for s in range(spalten):
                platz = anfaenge.get((z, s))
                if platz is None:
                    teile.append(
                        "<table:covered-table-cell/>"
                        if (z, s) in belegt
                        else '<table:table-cell table:style-name="Zelle"><text:p/></table:table-cell>'
                    )
                    continue
                attribute = ' table:style-name="ZelleKopf"' if platz.zelle.kopf else ' table:style-name="Zelle"'
                if platz.spalten > 1:
                    attribute += f' table:number-columns-spanned="{platz.spalten}"'
                if platz.zeilen > 1:
                    attribute += f' table:number-rows-spanned="{platz.zeilen}"'
                # Innenabstand der Zelle: 3 pt je Seite
                zellbreite = sum(spaltenbreiten[s : s + platz.spalten]) - 6
                inhalt = self.folge(
                    platz.zelle.bloecke or [Absatz()], in_zelle=True, kopf=platz.zelle.kopf, breite=zellbreite
                )
                teile.append(f'<table:table-cell office:value-type="string"{attribute}>{inhalt}</table:table-cell>')
            teile.append("</table:table-row>")
        teile.append("</table:table>")
        return "".join(teile)

    @staticmethod
    def _fett(a: Absatz) -> Absatz:
        return replace(a, laeufe=[replace(lauf, format=replace(lauf.format, fett=True)) for lauf in a.laeufe])

    def folge(
        self,
        bloecke: Sequence[Block],
        *,
        in_zelle: bool = False,
        kopf: bool = False,
        breite: float | None = None,
    ) -> str:
        """Blöcke nacheinander; aufeinanderfolgende Punkte derselben Liste werden eine verschachtelte Liste."""
        teile: list[str] = []
        i = 0
        while i < len(bloecke):
            block = bloecke[i]
            erster = _listenpunkt(block)
            if erster is not None:
                j = i + 1
                while (
                    j < len(bloecke) and (punkt := _listenpunkt(bloecke[j])) and punkt.oberste_nr == erster.oberste_nr
                ):
                    j += 1
                punkte = [cast(Absatz, b) for b in bloecke[i:j]]
                teile.append(self.liste([self._fett(a) if kopf else a for a in punkte], in_zelle=in_zelle))
                i = j
                continue
            if isinstance(block, Absatz):
                teile.append(self.absatz(self._fett(block) if kopf else block, in_zelle=in_zelle))
            elif isinstance(block, Bild):
                teile.append(self.bild(block, in_zelle=in_zelle, hoechstbreite=breite))
            elif isinstance(block, Trennlinie):
                umbruch = False if in_zelle else self._umbruch_nehmen()
                stil = self.stile.absatz_stil("Standard", None, 0, umbruch, False, True, None)
                teile.append(f'<text:p text:style-name="{stil}"/>')
            elif isinstance(block, Seitenumbruch):
                self.umbruch_offen = True
            elif isinstance(block, Tabelle):
                teile.append(self.tabelle(block))
            i += 1
        return "".join(teile)

    def bloecke(self, bloecke: list[Block]) -> str:
        teile = [self.folge(bloecke)]
        if self.umbruch_offen:
            stil = self.stile.absatz_stil("Standard", None, 0, self._umbruch_nehmen(), False, False, None)
            teile.append(f'<text:p text:style-name="{stil}"/>')
        return "".join(teile)


def _listenpunkt(block: object) -> Absatz | None:
    """Ein Absatz als Punkt einer Liste mit Punkten oder Zahlen (Aufgaben stehen als eingerückte Absätze da)."""
    if isinstance(block, Absatz) and block.liste is not None and block.liste[0] != "aufgaben":
        return block
    return None


def _listenstile() -> str:
    punkte = "".join(
        f'<text:list-level-style-bullet text:level="{e}" text:bullet-char="{z}"><style:list-level-properties '
        f'text:list-level-position-and-space-mode="label-alignment"><style:list-level-label-alignment '
        f'text:label-followed-by="listtab" fo:text-indent="-12pt" fo:margin-left="{12 + 18 * (e - 1) + 6}pt"/>'
        f"</style:list-level-properties></text:list-level-style-bullet>"
        for e, z in ((1, "•"), (2, "◦"), (3, "▪"))
    )
    zahlen = "".join(
        f'<text:list-level-style-number text:level="{e}" style:num-format="{f}" style:num-suffix="."><style:list-level-properties '
        f'text:list-level-position-and-space-mode="label-alignment"><style:list-level-label-alignment '
        f'text:label-followed-by="listtab" fo:text-indent="-14pt" fo:margin-left="{14 + 18 * (e - 1) + 6}pt"/>'
        f"</style:list-level-properties></text:list-level-style-number>"
        for e, f in ((1, "1"), (2, "a"), (3, "i"))
    )
    return f'<text:list-style style:name="LPunkte">{punkte}</text:list-style><text:list-style style:name="LZahlen">{zahlen}</text:list-style>'


def odt_schreiben(
    bloecke: list[Block],
    *,
    schrift: schriften.Schrift,
    groesse: float,
    raender: dict[str, float],
    titel: str = "",
    fuss_zeilen: list[str] | None = None,
    seitenzahlen: bool = False,
    signatur: str = "",
    schriftname: str | None = None,
) -> bytes:
    inhaltsbreite = (210 - raender["left"] - raender["right"]) * 72 / 25.4
    schreiber = _Schreiber(groesse, inhaltsbreite)
    if signatur:
        bloecke = [*bloecke, Absatz(), *[Absatz(laeufe=[Lauf(text=zeile)]) for zeile in signatur.splitlines()]]
    koerper = schreiber.bloecke(bloecke)
    grundschrift = schriftname or schrift.fuer_office
    alle_schriften = sorted({grundschrift, *schreiber.stile.schriften})
    font_faces = "".join(
        f"<style:font-face style:name={quoteattr(n)} svg:font-family={quoteattr(chr(39) + n + chr(39))}/>"
        for n in alle_schriften
    )

    content = (
        f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {NS}>'
        f"<office:font-face-decls>{font_faces}</office:font-face-decls>"
        f"<office:automatic-styles>{''.join(schreiber.stile.xml)}{_listenstile()}</office:automatic-styles>"
        f"<office:body><office:text>{koerper}</office:text></office:body></office:document-content>"
    )

    ueberschriften = "".join(
        f'<style:style style:name="Heading_20_{art[1]}" style:display-name="Heading {art[1]}" style:family="paragraph" '
        f'style:parent-style-name="Standard" style:next-style-name="Standard" style:default-outline-level="{art[1]}">'
        f'<style:paragraph-properties fo:margin-top="{_pt(groesse * faktor * vor)}" fo:margin-bottom="{_pt(groesse * faktor * nach)}" '
        f'style:line-height-at-least="{_pt(groesse * faktor * 1.25)}" fo:keep-with-next="always"/>'
        f'<style:text-properties fo:font-size="{_pt(groesse * faktor)}" fo:font-weight="bold" fo:color="#111827"/></style:style>'
        for art, (faktor, nach, vor) in UEBERSCHRIFTEN.items()
    )
    fuss = ""
    if fuss_zeilen or seitenzahlen:
        zeilen = "".join(f'<text:p text:style-name="Fuss">{_text(z)}</text:p>' for z in fuss_zeilen or [])
        if seitenzahlen:
            zeilen += (
                '<text:p text:style-name="Fuss">Seite <text:page-number text:select-page="current">1</text:page-number>'
                " von <text:page-count>1</text:page-count></text:p>"
            )
        fuss = f"<style:footer>{zeilen}</style:footer>"
    styles = (
        f'<?xml version="1.0" encoding="UTF-8"?><office:document-styles {NS}>'
        f"<office:font-face-decls>{font_faces}</office:font-face-decls>"
        "<office:styles>"
        f'<style:default-style style:family="paragraph"><style:text-properties style:font-name={quoteattr(grundschrift)} '
        f'fo:font-size="{_pt(groesse)}" fo:language="de" fo:country="DE" fo:hyphenate="false"/></style:default-style>'
        f'<style:style style:name="Standard" style:family="paragraph" style:class="text"><style:paragraph-properties '
        f'fo:margin-top="0pt" fo:margin-bottom="{_pt(groesse * ABSTAND_NACH)}" style:line-height-at-least="{_pt(groesse * ZEILENABSTAND)}" '
        f'fo:orphans="2" fo:widows="2"/><style:text-properties style:font-name={quoteattr(grundschrift)} fo:font-size="{_pt(groesse)}" fo:color="#111827"/></style:style>'
        f'<style:style style:name="Code" style:family="paragraph" style:parent-style-name="Standard"><style:text-properties style:font-name="Courier New" fo:font-size="{_pt(groesse * 0.9)}"/></style:style>'
        f'<style:style style:name="Fuss" style:family="paragraph"><style:paragraph-properties fo:text-align="center"/>'
        f'<style:text-properties style:font-name={quoteattr(grundschrift)} fo:font-size="7.5pt" fo:color="#4b5563"/></style:style>'
        f"{ueberschriften}"
        '<style:style style:name="Zelle" style:family="table-cell"><style:table-cell-properties fo:padding="3pt" fo:border="0.75pt solid #9ca3af"/></style:style>'
        '<style:style style:name="ZelleKopf" style:family="table-cell"><style:table-cell-properties fo:padding="3pt" fo:border="0.75pt solid #9ca3af" fo:background-color="#f3f4f6"/></style:style>'
        "</office:styles>"
        "<office:automatic-styles>"
        f'<style:page-layout style:name="Seite"><style:page-layout-properties fo:page-width="210mm" fo:page-height="297mm" '
        f'style:print-orientation="portrait" fo:margin-top="{raender["top"]}mm" fo:margin-bottom="{raender["bottom"]}mm" '
        f'fo:margin-left="{raender["left"]}mm" fo:margin-right="{raender["right"]}mm"/>'
        f'<style:footer-style><style:header-footer-properties fo:min-height="0mm" fo:margin-top="4mm"/></style:footer-style></style:page-layout>'
        "</office:automatic-styles>"
        f'<office:master-styles><style:master-page style:name="Standard" style:page-layout-name="Seite">{fuss}</style:master-page></office:master-styles>'
        "</office:document-styles>"
    )
    meta = (
        f'<?xml version="1.0" encoding="UTF-8"?><office:document-meta {NS}><office:meta>'
        f"<meta:generator>mandari</meta:generator><dc:title>{escape(titel)}</dc:title>"
        f"<meta:creation-date>{timezone.now().strftime('%Y-%m-%dT%H:%M:%S')}</meta:creation-date>"
        "</office:meta></office:document-meta>"
    )
    eintraege = "".join(
        f'<manifest:file-entry manifest:full-path="{datei}" manifest:media-type="image/{datei.rsplit(".", 1)[1].replace("jpg", "jpeg")}"/>'
        for datei, _ in schreiber.bilder
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">'
        '<manifest:file-entry manifest:full-path="/" manifest:version="1.3" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="meta.xml" manifest:media-type="text/xml"/>'
        f"{eintraege}</manifest:manifest>"
    )
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as paket:
        paket.writestr(
            zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED
        )
        paket.writestr("content.xml", content, compress_type=zipfile.ZIP_DEFLATED)
        paket.writestr("styles.xml", styles, compress_type=zipfile.ZIP_DEFLATED)
        paket.writestr("meta.xml", meta, compress_type=zipfile.ZIP_DEFLATED)
        paket.writestr("META-INF/manifest.xml", manifest, compress_type=zipfile.ZIP_DEFLATED)
        for datei, daten in schreiber.bilder:
            paket.writestr(datei, daten, compress_type=zipfile.ZIP_STORED)
    return puffer.getvalue()

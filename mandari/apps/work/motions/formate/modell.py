# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumentmodell für die Exporte eines Dokuments (Issue #1584): Word, OpenDocument, Markdown und Text.

Der Editor speichert HTML (TipTap, Positivliste ``apps/work/sanitize.py``). Alle Exporte lesen es einmal in dieses
Modell und schreiben es dann in ihr Format; so tragen sie dieselben Zeichenformate, Listen, Tabellen und Bilder.

- ``Lauf``: Text mit Zeichenformat (fett, kursiv, unterstrichen, durchgestrichen, hoch/tief, Code, Farbe,
  Hervorhebung, Schrift des Satzes, Größe in pt, Link) oder ein Zeilenumbruch
- ``Absatz``: Text, Überschrift 1–3, Zitat oder Code, mit Ausrichtung, Einzug und gegebenenfalls Listenpunkt
- ``Tabelle`` (Zellen mit Absätzen und Bildern, Kopfzellen, verbundene Zellen; ``raster()`` liefert die Lage
  jeder Zelle), ``Bild`` (eingebettete Rasterbilder, WebP als PNG), ``Trennlinie`` und ``Seitenumbruch``

Gelesen wird nur bereinigtes HTML (``clean_editor_html``); Bilder fremder Server sind vorher eingebettet.
"""

from __future__ import annotations

import base64
import io
import re
import struct
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Any, Literal

from apps.common import schriften

AbsatzArt = Literal["p", "h1", "h2", "h3", "zitat", "code"]
ListenArt = Literal["punkte", "zahlen", "aufgaben"]

_DATA_BILD_RE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=]+)$")
_PT_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2})?)pt$")
_LAENGE_RE = re.compile(r"^(\d{1,5}(?:\.\d{1,3})?)(px|pt|em|rem|mm|cm)?$")
_FARBE_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")


@dataclass(frozen=True)
class Format:
    fett: bool = False
    kursiv: bool = False
    unterstrichen: bool = False
    durchgestrichen: bool = False
    hoch: bool = False
    tief: bool = False
    code: bool = False
    #: Textfarbe als #RRGGBB
    farbe: str | None = None
    #: Hervorhebung als #RRGGBB
    hervorhebung: str | None = None
    #: Schlüssel einer Schrift des Satzes (apps/common/schriften.py)
    schrift: str | None = None
    #: Größe in pt
    groesse: float | None = None
    link: str | None = None


@dataclass
class Lauf:
    text: str
    format: Format = field(default_factory=Format)
    #: Zeilenumbruch statt Text
    umbruch: bool = False


@dataclass
class Absatz:
    art: AbsatzArt = "p"
    laeufe: list[Lauf] = field(default_factory=list)
    ausrichtung: str | None = None
    #: Einzug links in pt (Editor: margin-left)
    einzug: float = 0.0
    #: Listenpunkt: Art und Ebene (0 = oberste); None = kein Listenpunkt
    liste: tuple[ListenArt, int] | None = None
    #: Nummer in einer nummerierten Liste (für Text und Markdown)
    nummer: int | None = None
    #: Aufgabenliste: erledigt?
    erledigt: bool | None = None
    #: Weiterer Absatz desselben Listenpunkts (ohne eigenes Aufzählungszeichen)
    fortsetzung: bool = False
    #: Kennung der eigenen Liste (``<ul>``/``<ol>``) und der äußersten Liste: Punkte verschiedener Listen bleiben getrennt
    liste_nr: int = 0
    oberste_nr: int = 0
    #: Erster Punkt seiner Liste (dort beginnt die Nummerierung mit ``nummer``)
    listenbeginn: bool = False

    @property
    def text(self) -> str:
        return "".join("\n" if lauf.umbruch else lauf.text for lauf in self.laeufe)


@dataclass
class Bild:
    daten: bytes
    art: str
    breite: int
    hoehe: int
    alt: str = ""


@dataclass
class Trennlinie:
    pass


@dataclass
class Seitenumbruch:
    pass


@dataclass
class Zelle:
    #: Inhalt: Absätze und Bilder (eine Trennlinie in einer Zelle entfällt)
    bloecke: list[Absatz | Bild] = field(default_factory=list)
    kopf: bool = False
    spalten: int = 1
    zeilen: int = 1

    @property
    def absaetze(self) -> list[Absatz]:
        return [b for b in self.bloecke if isinstance(b, Absatz)]


@dataclass(frozen=True)
class Platz:
    """Lage einer Zelle im Raster: erste Zeile und Spalte, Spannweite auf das Raster gekürzt."""

    zelle: Zelle
    zeile: int
    spalte: int
    zeilen: int
    spalten: int


@dataclass
class Tabelle:
    zeilen: list[list[Zelle]] = field(default_factory=list)
    #: Breiten der Spalten in px aus dem Editor (leer = gleich breit)
    breiten: list[int] = field(default_factory=list)

    def raster(self) -> tuple[int, list[list[Platz]]]:
        """
        Spaltenzahl und je Zeile die Plätze ihrer Zellen, wie ein Browser die Tabelle legt: Eine Zelle rückt hinter
        Felder, die Zellen früherer Zeilen mit ``rowspan`` belegen. Die Größe ist vor dem Export begrenzt
        (``export_service.table_cell_count``).
        """
        belegt: set[tuple[int, int]] = set()
        breite = 0
        plaetze: list[list[Platz]] = []
        anzahl = len(self.zeilen)
        for z, zeile in enumerate(self.zeilen):
            reihe: list[Platz] = []
            s = 0
            for zelle in zeile:
                while (z, s) in belegt:
                    s += 1
                zeilen = max(1, min(zelle.zeilen, anzahl - z))
                # Höchstens bis zum nächsten Feld, das eine Zelle einer früheren Zeile belegt (keine Überlappung)
                spalten = 1
                while spalten < zelle.spalten and (z, s + spalten) not in belegt:
                    spalten += 1
                for zz in range(z, z + zeilen):
                    for ss in range(s, s + spalten):
                        belegt.add((zz, ss))
                reihe.append(Platz(zelle, z, s, zeilen, spalten))
                s += spalten
                breite = max(breite, s)
            plaetze.append(reihe)
        return breite, plaetze

    @property
    def spaltenzahl(self) -> int:
        return self.raster()[0]


Block = Absatz | Bild | Trennlinie | Seitenumbruch | Tabelle


#: Größtes WebP, das für die Exporte in PNG umgewandelt wird (Pixel)
MAX_WEBP_PIXEL = 40_000_000


def webp_als_png(daten: bytes) -> bytes | None:
    """
    WebP als PNG: Word (python-docx) und manche Programme für ODT kennen kein WebP. ``None``, wenn das Bild nicht
    lesbar oder zu groß ist.
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(daten)) as bild:
            if bild.format != "WEBP" or bild.width * bild.height > MAX_WEBP_PIXEL:
                return None
            puffer = io.BytesIO()
            bild.save(puffer, format="PNG")
            return puffer.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def bildgroesse(daten: bytes) -> tuple[int, int]:
    """Breite und Höhe eines PNG, JPEG oder GIF in Pixeln; (0, 0), wenn unbekannt."""
    try:
        if daten.startswith(b"\x89PNG"):
            breite, hoehe = struct.unpack(">II", daten[16:24])
            return breite, hoehe
        if daten.startswith((b"GIF87a", b"GIF89a")):
            breite, hoehe = struct.unpack("<HH", daten[6:10])
            return breite, hoehe
        if daten.startswith(b"\xff\xd8"):
            i = 2
            while i < len(daten) - 9:
                if daten[i] != 0xFF:
                    i += 1
                    continue
                marke = daten[i + 1]
                if marke in (0xC0, 0xC1, 0xC2):
                    hoehe, breite = struct.unpack(">HH", daten[i + 5 : i + 9])
                    return breite, hoehe
                laenge = struct.unpack(">H", daten[i + 2 : i + 4])[0]
                i += 2 + laenge
    except struct.error:
        pass
    return 0, 0


def _stil(wert: str | None) -> dict[str, str]:
    ergebnis: dict[str, str] = {}
    for teil in (wert or "").split(";"):
        if ":" in teil:
            name, _, inhalt = teil.partition(":")
            ergebnis[name.strip().lower()] = inhalt.strip()
    return ergebnis


def _farbe(wert: str | None) -> str | None:
    if not wert or not _FARBE_RE.match(wert):
        return None
    wert = wert.lstrip("#")
    if len(wert) == 3:
        wert = "".join(c * 2 for c in wert)
    return f"#{wert.lower()}"


def _laenge_pt(wert: str | None, schriftgroesse: float) -> float:
    treffer = _LAENGE_RE.match((wert or "").strip())
    if not treffer:
        return 0.0
    zahl = float(treffer.group(1))
    einheit = treffer.group(2) or "px"
    return {
        "px": zahl * 0.75,
        "pt": zahl,
        "em": zahl * schriftgroesse,
        "rem": zahl * 12,
        "mm": zahl * 72 / 25.4,
        "cm": zahl * 72 / 2.54,
    }[einheit]


class _Leser(HTMLParser):
    def __init__(self, grundgroesse: float) -> None:
        super().__init__(convert_charrefs=True)
        self.grundgroesse = grundgroesse
        self.bloecke: list[Block] = []
        self.formate: list[tuple[str, Format]] = []
        self.absatz: Absatz | None = None
        self.listen: list[list[Any]] = []  # [art, zähler, kennung, erster Punkt gesetzt?]
        self.listen_zaehler = 0
        self.oberste_liste = 0
        self.zitat = 0
        self.code = 0
        self.tabellen: list[Tabelle] = []
        self.zelle: Zelle | None = None
        self.erledigt: bool | None = None
        self.li_offen: list[bool] = []  # je offenem <li>: erster Absatz schon gesetzt?
        self.ausrichtung: str | None = None
        self.einzug = 0.0

    # --- Hilfen ---------------------------------------------------------------------------------------------------

    @property
    def format(self) -> Format:
        return self.formate[-1][1] if self.formate else Format()

    def _ziel(self) -> list[Any]:
        """Wohin ein fertiger Absatz gehört: in die offene Zelle oder in die Blöcke."""
        return self.zelle.bloecke if self.zelle is not None else self.bloecke

    def _absatz_beginnen(self, art: AbsatzArt, attrs: dict[str, str | None]) -> None:
        self._absatz_beenden()
        stil = _stil(attrs.get("style"))
        if self.zitat and art == "p":
            art = "zitat"
        if self.code:
            art = "code"
        absatz = Absatz(art=art, ausrichtung=stil.get("text-align") or None)
        absatz.einzug = _laenge_pt(stil.get("margin-left"), self.grundgroesse)
        if self.listen and self.li_offen:
            art_liste = self.listen[-1][0]
            absatz.liste = (art_liste, len(self.listen) - 1)
            absatz.liste_nr = self.listen[-1][2]
            absatz.oberste_nr = self.oberste_liste
            if not self.li_offen[-1]:
                if art_liste == "zahlen":
                    absatz.nummer = self.listen[-1][1]
                if art_liste == "aufgaben":
                    absatz.erledigt = bool(self.erledigt)
                absatz.listenbeginn = not self.listen[-1][3]
                self.listen[-1][3] = True
                self.li_offen[-1] = True
            else:
                absatz.fortsetzung = True
        self.absatz = absatz

    def _absatz_beenden(self) -> None:
        if self.absatz is None:
            return
        absatz = self.absatz
        self.absatz = None
        # Leerraum am Rand wie im Browser zusammenfassen
        if absatz.laeufe and absatz.art != "code":
            absatz.laeufe[0].text = absatz.laeufe[0].text.lstrip()
            absatz.laeufe[-1].text = absatz.laeufe[-1].text.rstrip()
        absatz.laeufe = [lauf for lauf in absatz.laeufe if lauf.umbruch or lauf.text]
        self._ziel().append(absatz)

    def _text(self, text: str) -> None:
        if self.absatz is None:
            if not text.strip():
                return
            # Text ohne Absatz (Listenpunkt ohne <p>, Zelle ohne <p>): eigenen Absatz beginnen
            self._absatz_beginnen("p", {})
        assert self.absatz is not None
        if not self.code:
            text = re.sub(r"\s+", " ", text)
        if self.absatz.laeufe and not self.absatz.laeufe[-1].umbruch and self.absatz.laeufe[-1].format == self.format:
            self.absatz.laeufe[-1].text += text
        else:
            self.absatz.laeufe.append(Lauf(text=text, format=self.format))

    def _format_oeffnen(self, tag: str, attrs: dict[str, str | None]) -> None:
        f = self.format
        stil = _stil(attrs.get("style"))
        if tag in ("strong", "b"):
            f = replace(f, fett=True)
        elif tag in ("em", "i"):
            f = replace(f, kursiv=True)
        elif tag in ("u", "ins"):
            f = replace(f, unterstrichen=True)
        elif tag in ("s", "strike", "del"):
            f = replace(f, durchgestrichen=True)
        elif tag == "sup":
            f = replace(f, hoch=True, tief=False)
        elif tag == "sub":
            f = replace(f, tief=True, hoch=False)
        elif tag == "code":
            f = replace(f, code=True)
        elif tag == "a":
            f = replace(f, link=attrs.get("href") or None)
        elif tag == "mark":
            f = replace(f, hervorhebung=_farbe(attrs.get("data-color") or stil.get("background-color")) or "#fef08a")
        if "color" in stil:
            f = replace(f, farbe=_farbe(stil["color"]) or f.farbe)
        if "background-color" in stil and tag != "mark":
            f = replace(f, hervorhebung=_farbe(stil["background-color"]) or f.hervorhebung)
        if "font-family" in stil:
            schrift = schriften.finden(stil["font-family"])
            f = replace(f, schrift=schrift.schluessel if schrift else f.schrift)
        if "font-size" in stil:
            groesse = _PT_RE.match(stil["font-size"])
            f = replace(f, groesse=float(groesse.group(1)) if groesse else f.groesse)
        self.formate.append((tag, f))

    def _format_schliessen(self, tag: str) -> None:
        for i in range(len(self.formate) - 1, -1, -1):
            if self.formate[i][0] == tag:
                del self.formate[i:]
                return

    # --- Parser ---------------------------------------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs_liste: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_liste)
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            art: AbsatzArt = "p" if tag == "p" else ("h3" if tag in ("h4", "h5", "h6") else tag)  # type: ignore[assignment]
            self._absatz_beginnen(art, attrs)
        elif tag == "pre":
            self._absatz_beenden()
            self.code += 1
            self._absatz_beginnen("code", attrs)
        elif tag == "blockquote":
            self._absatz_beenden()
            self.zitat += 1
        elif tag in ("ul", "ol"):
            self._absatz_beenden()
            art_liste: ListenArt = (
                "aufgaben" if attrs.get("data-type") == "taskList" else ("zahlen" if tag == "ol" else "punkte")
            )
            start_text = attrs.get("start") or ""
            start = int(start_text) if start_text.isdigit() else 1
            self.listen_zaehler += 1
            if not self.listen:
                self.oberste_liste = self.listen_zaehler
            self.listen.append([art_liste, start - 1, self.listen_zaehler, False])
        elif tag == "li":
            self._absatz_beenden()
            if self.listen:
                self.listen[-1][1] += 1
            self.erledigt = attrs.get("data-checked") == "true"
            self.li_offen.append(False)
        elif tag == "br":
            if self.absatz is None:
                self._absatz_beginnen("p", {})
            assert self.absatz is not None
            self.absatz.laeufe.append(Lauf(text="", format=self.format, umbruch=True))
        elif tag == "hr":
            self._absatz_beenden()
            if self.zelle is None:
                self.bloecke.append(Trennlinie())
        elif tag == "div" and "page-break" in (attrs.get("class") or ""):
            self._absatz_beenden()
            self.bloecke.append(Seitenumbruch())
        elif tag == "img":
            self._bild(attrs)
        elif tag == "table":
            self._absatz_beenden()
            self.tabellen.append(Tabelle())
        elif tag == "col" and self.tabellen:
            breite = _stil(attrs.get("style")).get("width", "")
            treffer = _LAENGE_RE.match(breite)
            self.tabellen[-1].breiten.append(int(float(treffer.group(1))) if treffer else 0)
        elif tag == "tr" and self.tabellen:
            self._absatz_beenden()
            self.tabellen[-1].zeilen.append([])
        elif tag in ("td", "th") and self.tabellen:
            self._absatz_beenden()
            if not self.tabellen[-1].zeilen:
                self.tabellen[-1].zeilen.append([])
            colspan = attrs.get("colspan") or ""
            rowspan = attrs.get("rowspan") or ""
            spalten = int(colspan) if colspan.isdigit() else 1
            zeilen = int(rowspan) if rowspan.isdigit() else 1
            breiten = [int(b) for b in (attrs.get("colwidth") or "").split(",") if b.isdigit()]
            zelle = Zelle(kopf=tag == "th", spalten=max(1, spalten), zeilen=max(1, zeilen))
            self.tabellen[-1].zeilen[-1].append(zelle)
            if breiten and len(self.tabellen) == 1:
                tabelle = self.tabellen[-1]
                spalte = sum(z.spalten for z in tabelle.zeilen[-1][:-1])
                while len(tabelle.breiten) < spalte + len(breiten):
                    tabelle.breiten.append(0)
                for k, b in enumerate(breiten):
                    tabelle.breiten[spalte + k] = tabelle.breiten[spalte + k] or b
            self.zelle = zelle
        elif tag == "input" and attrs.get("type") == "checkbox":
            self.erledigt = "checked" in attrs
        elif tag in (
            "strong",
            "b",
            "em",
            "i",
            "u",
            "ins",
            "s",
            "strike",
            "del",
            "sup",
            "sub",
            "code",
            "a",
            "mark",
            "span",
        ):
            self._format_oeffnen(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._absatz_beenden()
        elif tag == "pre":
            self._absatz_beenden()
            self.code = max(0, self.code - 1)
        elif tag == "blockquote":
            self._absatz_beenden()
            self.zitat = max(0, self.zitat - 1)
        elif tag in ("ul", "ol"):
            self._absatz_beenden()
            if self.listen:
                self.listen.pop()
        elif tag == "li":
            self._absatz_beenden()
            if self.li_offen:
                self.li_offen.pop()
        elif tag in ("td", "th"):
            self._absatz_beenden()
            self.zelle = None
        elif tag == "table" and self.tabellen:
            self._absatz_beenden()
            tabelle = self.tabellen.pop()
            if tabelle.zeilen:
                # Verschachtelte Tabellen: als eigene Tabelle hinter der äußeren (Exporte kennen keine Schachtelung)
                self.bloecke.append(tabelle)
        elif tag in (
            "strong",
            "b",
            "em",
            "i",
            "u",
            "ins",
            "s",
            "strike",
            "del",
            "sup",
            "sub",
            "code",
            "a",
            "mark",
            "span",
        ):
            self._format_schliessen(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self.tabellen and self.zelle is None:
            return  # Leerraum zwischen Zeilen und Zellen
        if self.listen and self.absatz is None and not data.strip():
            return
        self._text(data)

    def _bild(self, attrs: dict[str, str | None]) -> None:
        treffer = _DATA_BILD_RE.match((attrs.get("src") or "").strip())
        if not treffer:
            return
        try:
            daten = base64.b64decode(treffer.group(2))
        except ValueError:
            return
        art = "jpeg" if treffer.group(1) == "jpg" else treffer.group(1)
        if art == "webp":
            png = webp_als_png(daten)
            if png is None:
                return
            daten, art = png, "png"
        breite, hoehe = bildgroesse(daten)
        if not breite or not hoehe:
            return
        self._absatz_beenden()
        angabe = (attrs.get("width") or "").strip()
        if angabe.isdigit() and int(angabe) > 0:
            hoehe = round(hoehe * int(angabe) / breite)
            breite = int(angabe)
        bild = Bild(daten=daten, art=art, breite=breite, hoehe=hoehe, alt=attrs.get("alt") or "")
        if self.zelle is not None:
            self.zelle.bloecke.append(bild)
        else:
            self.bloecke.append(bild)

    def ergebnis(self) -> list[Block]:
        self._absatz_beenden()
        while self.tabellen:
            tabelle = self.tabellen.pop()
            if tabelle.zeilen:
                self.bloecke.append(tabelle)
        return self.bloecke


def lesen(html: str, grundgroesse: float = 11.0) -> list[Block]:
    """Bereinigtes Editor-HTML in Blöcke lesen."""
    leser = _Leser(grundgroesse)
    leser.feed(html or "")
    leser.close()
    return leser.ergebnis()

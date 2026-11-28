# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Treue PDF-Übernahme (Issue #1600): ein PDF so in den Editor bringen, dass Editor und Export aussehen wie das Original.

Das PDF wird in zwei Ebenen geteilt:

- **Hintergrund**: alles, was an der Seite hängt (Logos, Briefkopf, Linien, Kopf- und Fußzeilen, Text neben dem
  Satzspiegel). Er bleibt als Vektor-PDF der Originalseiten erhalten und wird zum dokumenteigenen Briefpapier.
- **Fließtext**: der Text des Satzspiegels mit Rändern, Schrift, Größe, Fett/Kursiv/Unterstrichen, Farbe,
  Ausrichtung, Zeilenabstand, Abstand vor dem Absatz, Einzügen und Listenzeichen. Grafiken mitten im Text
  (Tabellen, Kästen, Abbildungen) stehen als Bild im Fluss.

Die Schriften des Satzes haben dieselben Zeichenbreiten wie die Office-Schriften (Arial → Arimo, Calibri → Carlito,
Times New Roman → Tinos …). Mit denselben Rändern und Abständen bricht die Satz-Kette deshalb an denselben Stellen um
wie das Original. Getrennte Wörter am Zeilenende bekommen ein weiches Trennzeichen an derselben Stelle.

Die Analyse nutzt PDFium (``pypdfium2``, Apache-2.0/BSD-3-Clause), wie die Seitenanalyse in ``insight_core``.
Alle Längen in Punkt (1/72 Zoll), senkrecht von der Oberkante der Seite gemessen.
"""

from __future__ import annotations

import base64
import ctypes
import html
import io
import logging
import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from apps.common import schriften

logger = logging.getLogger(__name__)

PT_JE_MM = 72 / 25.4
#: Größte Seitenzahl für die treue Übernahme (darüber: bisheriger Textimport)
MAX_SEITEN = 150
#: Auflösung der Grafiken im Textfluss (Faktor zu 72 dpi): 2,5 ergibt 180 dpi
GRAFIK_SKALA = 2.5

#: Senkrechte Schriftmaße (hhea, Anteil der Schriftgröße) der Schriften des Satzes. Chromium setzt die Grundlinie in
#: einer Zeile mit fester Zeilenhöhe bei (Zeilenhöhe − (Aufstieg + Abstieg)) / 2 + Aufstieg.
SCHRIFT_MASSE: dict[str, tuple[float, float]] = {
    "arimo": (1854 / 2048, 434 / 2048),
    "carlito": (1950 / 2048, 550 / 2048),
    "tinos": (1825 / 2048, 443 / 2048),
    "caladea": (0.9, 0.25),
    "cousine": (1705 / 2048, 615 / 2048),
    "open-sans": (2189 / 2048, 600 / 2048),
    "roboto": (1900 / 2048, 500 / 2048),
    "inter": (1984 / 2048, 494 / 2048),
    "atkinson-hyperlegible": (0.95, 0.29),
    "noto-serif": (1.069, 0.293),
    "literata": (1.177, 0.308),
    "ubuntu": (0.932, 0.189),
}

#: Breite des Leerzeichens (Anteil der Schriftgröße) der Schriften des Satzes
LEERZEICHEN: dict[str, float] = {
    "arimo": 0.2778,
    "carlito": 0.2261,
    "tinos": 0.25,
    "caladea": 0.22,
    "cousine": 0.6001,
    "open-sans": 0.2598,
    "roboto": 0.248,
    "inter": 0.2812,
    "atkinson-hyperlegible": 0.28,
    "noto-serif": 0.26,
    "literata": 0.2,
    "ubuntu": 0.226,
}

_LISTENZEICHEN_RE = re.compile(
    r"^(?:\d{1,3}(?:\.\d{1,3})*\.?|\(?[a-zA-Z]\)|[ivxIVX]{1,5}\.|[•·▪■◦●○‣\-–—*\uf0b7\uf0a7\uf02d\uf076\uf0d8])$"
)
_SYMBOL_ERSATZ = {"\uf0b7": "•", "\uf0a7": "▪", "\uf02d": "–", "\uf076": "❖", "\uf0d8": "➢", "\uf0fc": "✓"}
_SEITENZAHL_RE = re.compile(r"\d+")
#: Zeile, die nur eine Seitenzahl ist
_SEITENZAHL_ALLEIN_RE = re.compile(r"^(?:Seite\s+\d+(?:\s+(?:von|/)\s+\d+)?|-\s*\d+\s*-|\d+\s*/\s*\d+)$", re.IGNORECASE)
#: Bindestrich am Zeilenende vor diesen Wörtern ist ein Ergänzungsstrich („Fuß- und Radverkehr“), keine Trennung
_ERGAENZUNG = frozenset({"und", "oder", "bzw.", "sowie", "bis", "als", "u.", "&"})


# --- Datenmodell ---------------------------------------------------------------------------------------------------


@dataclass
class Lauf:
    text: str
    schrift: str
    groesse: float
    fett: bool
    kursiv: bool
    farbe: str
    x0: float
    x1: float
    unterstrichen: bool = False
    #: Zusätzlicher Abstand vor dem Lauf in pt (große Lücke in der Zeile, etwa Unterschriften nebeneinander)
    luecke: float = 0.0
    #: Wortabstand in pt (negativ: im Blocksatz gestauchte Zeile des Originals)
    wortabstand: float = 0.0
    #: Zeichenabstand in pt: gleicht Zeilen aus, die das Original mit gerundeten Zeichenbreiten gesetzt hat
    zeichenabstand: float = 0.0
    #: Schriftname im PDF
    quelle: str = ""

    def gleich_formatiert(self, other: Lauf) -> bool:
        return (
            other.luecke == 0.0
            and self.schrift == other.schrift
            and abs(self.groesse - other.groesse) < 0.05
            and self.fett == other.fett
            and self.kursiv == other.kursiv
            and self.farbe == other.farbe
            and self.unterstrichen == other.unterstrichen
            and self.quelle == other.quelle
        )


@dataclass
class Zeile:
    seite: int
    grund: float
    x0: float
    x1: float
    oben: float
    unten: float
    laeufe: list[Lauf] = field(default_factory=list)
    rolle: str = "fluss"  # fluss | hintergrund | grafik | seitenzahl
    #: Endet mit einem manuellen Zeilenumbruch (im Blocksatz nicht bis zum Rand gedehnt)
    harter_umbruch: bool = False
    #: Zeichen mit linker und rechter Kante, parallel zu ``text`` (Listenzeichen, Lücken, natürliche Breite)
    zeichen: list[tuple[str, float, float]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(lauf.text for lauf in self.laeufe)

    @property
    def groesse(self) -> float:
        sichtbar = [lauf.groesse for lauf in self.laeufe if lauf.text.strip()]
        return max(sichtbar) if sichtbar else (self.laeufe[0].groesse if self.laeufe else 0.0)

    def haupt(self) -> Lauf:
        """Lauf mit den meisten Zeichen (bestimmt Schrift und Größe des Absatzes)."""
        zaehler: Counter[int] = Counter()
        for i, lauf in enumerate(self.laeufe):
            zaehler[i] += len(lauf.text.strip())
        return self.laeufe[zaehler.most_common(1)[0][0]]


@dataclass
class Bereich:
    """Grafik (Bild oder Gruppe von Pfaden) auf einer Seite."""

    seite: int
    x0: float
    oben: float
    x1: float
    unten: float
    art: str  # bild | pfad
    rolle: str = "hintergrund"  # hintergrund | fluss | unterstreichung

    def ueberlappt(self, other: Bereich, rand: float = 1.0) -> bool:
        return not (
            other.x0 > self.x1 + rand
            or other.x1 < self.x0 - rand
            or other.oben > self.unten + rand
            or other.unten < self.oben - rand
        )


@dataclass
class Tabelle:
    """Spalten ohne Rahmen (Tagesordnung: Vorlage ⇥ Nummer ⇥ Text): Reihen aus Zellen mit Teilzeilen."""

    seite: int
    #: Linke Kanten der Spalten (x in pt); die erste Spalte beginnt am Satzspiegel
    spalten: list[float]
    reihen: list[list[list[Zeile]]]
    #: Ober- und Unterkante im Original (beim Setzen bestimmt)
    oben: float = 0.0
    unten: float = 0.0


@dataclass
class Absatz:
    zeilen: list[Zeile]
    art: str = "text"  # text | grafik | tabelle
    bereich: Bereich | None = None
    bild: bytes = b""
    tabelle: Tabelle | None = None
    #: Rechte Kante des Blocksatzes (kann vor dem Satzspiegel enden: Einzug rechts)
    rand_rechts: float | None = None


@dataclass
class Ergebnis:
    html: str
    hintergrund_pdf: bytes
    #: Satzspiegel in mm (Gleitkomma)
    raender: dict[str, float]
    schrift: str
    groesse_pt: float
    seiten: int
    #: Hintergrundseite je Seite (0-basiert); Seiten darüber hinaus nehmen die letzte
    hintergrund_seiten: list[int]
    seitenzahl: dict[str, Any] | None
    seitenbreite_mm: float
    seitenhoehe_mm: float
    hinweise: list[str] = field(default_factory=list)
    #: Das Original setzt mit Unterschneidung (LibreOffice, InDesign); Word setzt standardmäßig ohne
    unterschneidung: bool = False


class NichtGeeignetError(Exception):
    """Das PDF eignet sich nicht für die treue Übernahme (Scan, zu groß, kein Text)."""


# --- Lesen ---------------------------------------------------------------------------------------------------------


def _schrift_aus_name(name: str) -> tuple[str, bool, bool]:
    """PostScript-Name → (Schlüssel der Satzschrift, fett, kursiv)."""
    roh = name.split("+")[-1]
    klein = roh.lower()
    fett = any(teil in klein for teil in ("bold", "black", "heavy", "semibold", "demi"))
    kursiv = "italic" in klein or "oblique" in klein
    basis = re.sub(
        r"[-,](bold|italic|oblique|regular|roman|black|light|semibold|medium|boldmt|italicmt|bolditalicmt)\b.*$",
        "",
        roh,
        flags=re.IGNORECASE,
    )
    basis = re.sub(r"(PSMT|MT|PS)$", "", basis)
    basis = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", basis).strip()
    kandidaten = [basis, basis.replace(" ", ""), roh]
    for kandidat in kandidaten:
        schrift = schriften.finden(kandidat)
        if schrift is not None:
            return schrift.schluessel, fett, kursiv
    if "times" in klein:
        return "tinos", fett, kursiv
    if "courier" in klein:
        return "cousine", fett, kursiv
    if "calibri" in klein:
        return "carlito", fett, kursiv
    if "cambria" in klein:
        return "caladea", fett, kursiv
    if "georgia" in klein or "garamond" in klein or "serif" in klein and "sans" not in klein:
        return "noto-serif", fett, kursiv
    return "", fett, kursiv


def _farbe(r: int, g: int, b: int) -> str:
    if max(r, g, b) < 40 and max(r, g, b) - min(r, g, b) < 12:
        return ""  # Schwarz bzw. fast Schwarz: Standardfarbe
    return f"#{r:02x}{g:02x}{b:02x}"


def _zeilen_der_seite(raw: Any, seite: Any, nr: int, hoehe: float, unbekannt: Counter[str]) -> list[Zeile]:
    tp = seite.get_textpage()
    try:
        n = tp.count_chars()
        zeilen: list[Zeile] = []
        akt: Zeile | None = None
        name_buf = ctypes.create_string_buffer(256)
        flags = ctypes.c_int()
        rect = raw.FS_RECTF()
        matrix = raw.FS_MATRIX()
        ox, oy = ctypes.c_double(), ctypes.c_double()
        rr, gg, bb, aa = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
        cache: dict[str, tuple[str, bool, bool]] = {}
        for i in range(n):
            code = raw.FPDFText_GetUnicode(tp.raw, i)
            if code in (0x0D, 0x0A, 0xFFFE):
                if code in (0x0D, 0x0A):
                    akt = None
                continue
            # PDFium meldet einen Bindestrich am Zeilenende als 0x02 (Trennung oder Wortverbindung wie „KSD-“)
            zeichen = "-" if code == 0x02 else chr(code) if code else ""
            if not zeichen:
                continue
            zeichen = _SYMBOL_ERSATZ.get(zeichen, zeichen)
            raw.FPDFText_GetLooseCharBox(tp.raw, i, ctypes.byref(rect))
            raw.FPDFText_GetCharOrigin(tp.raw, i, ctypes.byref(ox), ctypes.byref(oy))
            # Schriftgröße mal Maßstab der Textmatrix (Druckertreiber setzen in Pixeln und skalieren mit 0,75)
            raw.FPDFText_GetMatrix(tp.raw, i, ctypes.byref(matrix))
            massstab = math.sqrt(abs(matrix.a * matrix.d - matrix.b * matrix.c)) or 1.0
            groesse = round(raw.FPDFText_GetFontSize(tp.raw, i) * massstab, 2)
            generiert = raw.FPDFText_IsGenerated(tp.raw, i) == 1
            if generiert and zeichen == " " and akt is not None and akt.laeufe:
                vorher = akt.laeufe[-1]
                akt.laeufe.append(
                    Lauf(" ", vorher.schrift, vorher.groesse, vorher.fett, vorher.kursiv, vorher.farbe, akt.x1, akt.x1)
                )
                akt.zeichen.append((" ", akt.x1, akt.x1))
                continue
            raw.FPDFText_GetFontInfo(tp.raw, i, name_buf, 256, ctypes.byref(flags))
            name = name_buf.value.decode("latin-1", "replace")
            if name not in cache:
                schluessel, fett, kursiv = _schrift_aus_name(name)
                if not schluessel and name:
                    unbekannt[name.split("+")[-1]] += 1
                cache[name] = (schluessel or schriften.STANDARD, fett, kursiv or bool(flags.value & 64))
            schluessel, fett, kursiv = cache[name]
            gewicht = raw.FPDFText_GetFontWeight(tp.raw, i)
            if 600 <= gewicht <= 1000:
                fett = True
            farbe = ""
            if raw.FPDFText_GetFillColor(
                tp.raw, i, ctypes.byref(rr), ctypes.byref(gg), ctypes.byref(bb), ctypes.byref(aa)
            ):
                farbe = _farbe(rr.value, gg.value, bb.value)
            grund = hoehe - oy.value
            x0, x1 = rect.left, rect.right
            if akt is None or abs(akt.grund - grund) > max(1.5, groesse * 0.35) or x0 < akt.x1 - groesse * 2:
                akt = Zeile(nr, grund, x0, x1, hoehe - rect.top, hoehe - rect.bottom)
                zeilen.append(akt)
            lauf = Lauf(zeichen, schluessel, groesse, fett, kursiv, farbe, x0, x1, quelle=name)
            akt.zeichen.append((zeichen, x0, x1))
            if akt.laeufe and akt.laeufe[-1].gleich_formatiert(lauf):
                letzter = akt.laeufe[-1]
                letzter.text += zeichen
                letzter.x1 = max(letzter.x1, x1)
            else:
                akt.laeufe.append(lauf)
            if zeichen.strip():
                akt.x0 = min(akt.x0, x0)
                akt.x1 = max(akt.x1, x1)
                akt.oben = min(akt.oben, hoehe - rect.top)
                akt.unten = max(akt.unten, hoehe - rect.bottom)
        for zeile in zeilen:
            # Leerzeichen am Ende und Anfang zählen nicht zur Zeile
            while zeile.laeufe and not zeile.laeufe[-1].text.strip():
                zeile.laeufe.pop()
            if zeile.laeufe:
                zeile.laeufe[-1].text = zeile.laeufe[-1].text.rstrip()
            while zeile.laeufe and not zeile.laeufe[0].text.strip():
                zeile.laeufe.pop(0)
            if zeile.laeufe:
                zeile.laeufe[0].text = zeile.laeufe[0].text.lstrip()
            # Zeichenliste auf denselben Text kürzen
            anfang = 0
            while anfang < len(zeile.zeichen) and not zeile.zeichen[anfang][0].strip():
                anfang += 1
            zeile.zeichen = zeile.zeichen[anfang : anfang + len(zeile.text)]
        return [z for z in zeilen if z.text.strip()]
    finally:
        tp.close()


def _bereiche_der_seite(raw: Any, seite: Any, nr: int, hoehe: float) -> list[Bereich]:
    bereiche: list[Bereich] = []
    for obj in seite.get_objects(max_depth=4):
        if obj.type not in (raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_SHADING):
            continue
        links_x, b, r, t = obj.get_bounds()
        if r - links_x <= 0.1 and t - b <= 0.1:
            continue
        if obj.type == raw.FPDF_PAGEOBJ_PATH and _unsichtbar(raw, obj):
            continue
        art = "bild" if obj.type == raw.FPDF_PAGEOBJ_IMAGE else "pfad"
        bereiche.append(Bereich(nr, links_x, hoehe - t, r, hoehe - b, art))
    return bereiche


def _unsichtbar(raw: Any, obj: Any) -> bool:
    """Weiße Füllflächen ohne Strich (Seitenhintergrund) zählen nicht als Grafik."""
    modus = ctypes.c_int()
    strich = ctypes.c_int()
    if not raw.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(modus), ctypes.byref(strich)):
        return False
    if strich.value:
        return False
    if modus.value == 0:
        return True
    r, g, b, a = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
    if raw.FPDFPageObj_GetFillColor(obj.raw, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)):
        return a.value == 0 or (r.value > 250 and g.value > 250 and b.value > 250)
    return False


# --- Analyse -------------------------------------------------------------------------------------------------------


#: Breite einiger Kleinbuchstaben (Anteil der Schriftgröße) in Arial normal und fett: Daran erkennt die Übernahme
#: fetten Text in Schriften ohne sprechenden Namen (Druckertreiber benennen sie F1, F2 …)
_BREITE_NORMAL = {"n": 0.556, "o": 0.556, "r": 0.333, "t": 0.278, "i": 0.222, "s": 0.5, "u": 0.556}
_BREITE_FETT = {"n": 0.611, "o": 0.611, "r": 0.389, "t": 0.333, "i": 0.278, "s": 0.556, "u": 0.611}


def _fett_aus_breiten(zeilen: list[Zeile]) -> None:
    """Fett für Schriften ohne erkennbaren Namen an den Zeichenbreiten erkennen."""
    abweichung: dict[str, list[float]] = {}
    for zeile in zeilen:
        i = 0
        for lauf in zeile.laeufe:
            name = lauf.quelle.split("+")[-1]
            if name and _schrift_aus_name(name)[0] == "":
                for k in range(i, i + len(lauf.text)):
                    z, x0, x1 = zeile.zeichen[k]
                    if z in _BREITE_NORMAL and lauf.groesse > 0:
                        anteil = (x1 - x0) / lauf.groesse
                        abweichung.setdefault(lauf.quelle, []).append(
                            abs(anteil - _BREITE_FETT[z]) - abs(anteil - _BREITE_NORMAL[z])
                        )
            i += len(lauf.text)
    fett = {name for name, werte in abweichung.items() if len(werte) >= 10 and statistics.median(werte) < 0}
    normal = {name for name, werte in abweichung.items() if len(werte) >= 10 and statistics.median(werte) >= 0}
    for zeile in zeilen:
        for lauf in zeile.laeufe:
            if lauf.quelle in fett:
                lauf.fett = True
            elif lauf.quelle in normal:
                lauf.fett = False


def _unterstreichungen(zeilen: list[Zeile], bereiche: list[Bereich]) -> None:
    """
    Dünne waagerechte Linien knapp unter der Grundlinie werden zur Unterstreichung genau der Zeichen darüber. Eine
    Linie zählt nur, wenn sie überwiegend unter Text liegt: Tabellenlinien laufen auch unter Lücken zwischen Zellen.
    """
    markiert: dict[int, list[bool]] = {}
    for bereich in bereiche:
        if bereich.art != "pfad" or bereich.unten - bereich.oben > 1.6 or bereich.x1 - bereich.x0 < 4:
            continue
        mitte = (bereich.oben + bereich.unten) / 2
        for zeile in zeilen:
            if zeile.seite != bereich.seite:
                continue
            if not (zeile.grund - 0.5 <= mitte <= zeile.grund + max(3.5, zeile.groesse * 0.3)):
                continue
            unter = [
                i
                for i, (z, x0, x1) in enumerate(zeile.zeichen)
                if bereich.x0 - 0.5 <= (x0 + x1) / 2 <= bereich.x1 + 0.5
            ]
            sichtbar = [i for i in unter if zeile.zeichen[i][0].strip()]
            if not sichtbar:
                continue
            bedeckt = sum(zeile.zeichen[i][2] - zeile.zeichen[i][1] for i in sichtbar)
            # Unterstreichungen beginnen und enden mit dem Text; Tabellenlinien reichen über die Zelle hinaus
            buendig = (
                abs(zeile.zeichen[sichtbar[0]][1] - bereich.x0) < 2.0
                and abs(zeile.zeichen[sichtbar[-1]][2] - bereich.x1) < 2.0
            )
            if not buendig or bedeckt < (bereich.x1 - bereich.x0) * 0.6:
                continue
            flags = markiert.setdefault(id(zeile), [False] * len(zeile.zeichen))
            for i in unter:
                flags[i] = True
            bereich.rolle = "unterstreichung"
            break
    for zeile in zeilen:
        markierung = markiert.get(id(zeile))
        if markierung is None:
            continue
        flags = markierung
        neu: list[Lauf] = []
        pos = 0
        for lauf in zeile.laeufe:
            stueck_anfang = 0
            for k in range(1, len(lauf.text) + 1):
                ende_erreicht = k == len(lauf.text)
                if ende_erreicht or flags[pos + k] != flags[pos + stueck_anfang]:
                    teil = Lauf(
                        lauf.text[stueck_anfang:k],
                        lauf.schrift,
                        lauf.groesse,
                        lauf.fett,
                        lauf.kursiv,
                        lauf.farbe,
                        lauf.x0,
                        lauf.x1,
                        flags[pos + stueck_anfang] or lauf.unterstrichen,
                    )
                    if neu and neu[-1].gleich_formatiert(teil):
                        neu[-1].text += teil.text
                    else:
                        neu.append(teil)
                    stueck_anfang = k
            pos += len(lauf.text)
        zeile.laeufe = neu


def _gruppen(bereiche: list[Bereich]) -> list[Bereich]:
    """Benachbarte Pfade (Rahmen, Tabellenlinien, Vektorlogos) zu einer Grafik zusammenfassen."""
    gruppen: list[Bereich] = []
    for bereich in sorted(bereiche, key=lambda b: (b.seite, b.oben, b.x0)):
        if bereich.rolle == "unterstreichung":
            continue
        for gruppe in gruppen:
            if gruppe.seite == bereich.seite and gruppe.ueberlappt(bereich, rand=3.0):
                gruppe.x0 = min(gruppe.x0, bereich.x0)
                gruppe.x1 = max(gruppe.x1, bereich.x1)
                gruppe.oben = min(gruppe.oben, bereich.oben)
                gruppe.unten = max(gruppe.unten, bereich.unten)
                if bereich.art == "bild":
                    gruppe.art = "bild"
                break
        else:
            gruppen.append(Bereich(bereich.seite, bereich.x0, bereich.oben, bereich.x1, bereich.unten, bereich.art))
    # Zusammenfassen kann neue Überlappungen ergeben
    geaendert = True
    while geaendert:
        geaendert = False
        for i, a in enumerate(gruppen):
            for b in gruppen[i + 1 :]:
                if a.seite == b.seite and a.ueberlappt(b, rand=3.0):
                    a.x0, a.x1 = min(a.x0, b.x0), max(a.x1, b.x1)
                    a.oben, a.unten = min(a.oben, b.oben), max(a.unten, b.unten)
                    if b.art == "bild":
                        a.art = "bild"
                    gruppen.remove(b)
                    geaendert = True
                    break
            if geaendert:
                break
    return gruppen


def _norm(text: str) -> str:
    return _SEITENZAHL_RE.sub("#", re.sub(r"\s+", " ", text.strip()))


def _seitenzahl_vorlage(treffer: list[Zeile], seiten: int) -> dict[str, Any] | None:
    """Vorlage der Seitenzahl aus ihren Zeilen (Text mit ``{n}``/``{gesamt}``, Lage, Schrift); markiert die Zeilen."""
    beispiel = treffer[0]
    text = beispiel.text
    nummer = str(beispiel.seite + 1)
    vorlage = re.sub(rf"(?<!\d){re.escape(nummer)}(?!\d)", "{n}", text, count=1)
    vorlage = re.sub(rf"(?<!\d){seiten}(?!\d)", "{gesamt}", vorlage, count=1) if "{n}" in vorlage else vorlage
    if "{n}" not in vorlage:
        return None
    mitte = (beispiel.x0 + beispiel.x1) / 2
    lauf = beispiel.haupt()
    seitenzahl = {
        "text": vorlage,
        "grund_mm": round(beispiel.grund / PT_JE_MM, 2),
        "x_mm": round(beispiel.x0 / PT_JE_MM, 2),
        "mitte_mm": round(mitte / PT_JE_MM, 2),
        "rechts_mm": round(beispiel.x1 / PT_JE_MM, 2),
        "ausrichtung": "",  # unten bestimmt
        "schrift": lauf.schrift,
        "groesse_pt": lauf.groesse,
        "fett": lauf.fett,
        "ab_seite": min(z.seite for z in treffer) + 1,
    }
    for zeile in treffer:
        zeile.rolle = "seitenzahl"
    return seitenzahl


def _kopf_und_fuss(zeilen: list[Zeile], seiten: int, hoehe: float) -> dict[str, Any] | None:
    """Wiederkehrende Zeilen am Seitenrand: Hintergrund; enthalten sie die Seitennummer, wird daraus die Seitenzahl."""
    rand = [z for z in zeilen if z.grund < hoehe * 0.12 or z.grund > hoehe * 0.88]
    if seiten < 2:
        # Ein Blatt: nur eine eindeutige Seitenzahl („Seite 1 von 1“, „- 1 -“) wird zur Seitenzahl
        for zeile in rand:
            if _SEITENZAHL_ALLEIN_RE.match(zeile.text.strip()):
                return _seitenzahl_vorlage([zeile], seiten)
        return None
    gruppen: dict[tuple[str, int], list[Zeile]] = {}
    for zeile in rand:
        gruppen.setdefault((_norm(zeile.text), round(zeile.grund / 3)), []).append(zeile)
    seitenzahl: dict[str, Any] | None = None
    for (muster, _), treffer in gruppen.items():
        seiten_mit = {z.seite for z in treffer}
        if len(seiten_mit) < max(2, (seiten - 1) // 2):
            continue
        for zeile in treffer:
            zeile.rolle = "hintergrund"
        if "#" not in muster:
            continue
        # Enthält die Zeile auf jeder Seite deren Nummer?
        passend = all(str(z.seite + 1) in _SEITENZAHL_RE.findall(z.text) for z in treffer)
        if not passend or seitenzahl is not None:
            continue
        seitenzahl = _seitenzahl_vorlage(treffer, seiten)
        if seitenzahl is None:
            continue
        for zeile in treffer:
            zeile.rolle = "seitenzahl"
    return seitenzahl


def _spalte(fluss: list[Zeile], breite: float) -> tuple[float, float]:
    """Linker und rechter Rand des Satzspiegels (x in pt)."""
    anfaenge = Counter(round(z.x0) for z in fluss if z.x1 - z.x0 > breite * 0.3)
    if not anfaenge:
        anfaenge = Counter(round(z.x0) for z in fluss)
    links = min(x for x, n in anfaenge.items() if n >= max(1, anfaenge.most_common(1)[0][1] * 0.25))
    enden = sorted(z.x1 for z in fluss if z.x1 - z.x0 > breite * 0.3) or sorted(z.x1 for z in fluss)
    # Rechter Rand = das Ende der längsten Zeile: Jede Zeile des Originals passt dann, und ein Wort, das dort nicht
    # mehr passte, passt auch hier nicht. Einzelne Zeilen, die deutlich über die übrigen ragen (überlange Wörter,
    # Adressen), zählen nicht.
    while len(enden) > 10 and enden[-1] - statistics.median(enden[-10:]) > 3:
        enden.pop()
    rechts = enden[-1] if enden else breite - links
    return float(links) - 0.5, rechts + 0.25


def _wiederkehrend(gruppe: Bereich, gruppen: list[Bereich]) -> bool:
    """Grafik an derselben Stelle auf einer anderen Seite (Logo, Linie des Briefpapiers)."""
    return any(
        andere.seite != gruppe.seite
        and abs(andere.x0 - gruppe.x0) < 1
        and abs(andere.oben - gruppe.oben) < 1
        and abs(andere.x1 - gruppe.x1) < 1
        and abs(andere.unten - gruppe.unten) < 1
        for andere in gruppen
    )


def _klassifizieren(zeilen: list[Zeile], gruppen: list[Bereich], links: float, rechts: float, breite: float) -> None:
    satzbreite = rechts - links
    for zeile in zeilen:
        if zeile.rolle != "fluss":
            continue
        rechtsbuendig = abs(zeile.x1 - rechts) < 3
        neben_dem_satzspiegel = zeile.x0 < links - 12 or zeile.x1 > rechts + 12
        if (zeile.x0 > links + satzbreite * 0.45 and not rechtsbuendig) or neben_dem_satzspiegel:
            zeile.rolle = "hintergrund"
    # Zeilen neben anderen Zeilen auf gleicher Höhe (zweispaltige Köpfe): die am linken Rand bleibt im Fluss
    nach_seite: dict[int, list[Zeile]] = {}
    for zeile in zeilen:
        nach_seite.setdefault(zeile.seite, []).append(zeile)
    for seiten_zeilen in nach_seite.values():
        for a in seiten_zeilen:
            for b in seiten_zeilen:
                if a is b or a.rolle != "fluss" or b.rolle != "fluss":
                    continue
                if abs(a.grund - b.grund) < min(a.groesse, b.groesse) * 0.6 and b.x0 > a.x1 + 8:
                    b.rolle = "hintergrund"
    # Grafiken: im Fluss, wenn oberhalb Fließtext derselben Seite steht und kein Fließtext daneben
    for gruppe in gruppen:
        seiten_zeilen = nach_seite.get(gruppe.seite, [])
        fluss = [z for z in seiten_zeilen if z.rolle == "fluss"]
        innen = [
            z
            for z in fluss
            if gruppe.oben - 1 <= z.grund <= gruppe.unten + 1 and gruppe.x0 - 2 <= z.x0 <= gruppe.x1 + 2
        ]
        neben = [z for z in fluss if z not in innen and z.unten > gruppe.oben + 1 and z.oben < gruppe.unten - 1]
        darueber = [z for z in fluss if z.grund < gruppe.oben and z not in innen]
        darunter = [z for z in fluss if z.grund > gruppe.unten and z not in innen]
        innerhalb_spalte = gruppe.x0 >= links - 15 and gruppe.x1 <= rechts + 15
        # Briefkopf: auf der ersten Seite über allem Text, oder an derselben Stelle auf mehreren Seiten
        briefkopf = (gruppe.seite == 0 and not darueber) or _wiederkehrend(gruppe, gruppen)
        im_text = bool(darueber or darunter)
        if im_text and not neben and innerhalb_spalte and not briefkopf:
            gruppe.rolle = "fluss"
            for zeile in innen:
                zeile.rolle = "grafik"
        else:
            gruppe.rolle = "hintergrund"
            # Text, der ganz in einer Hintergrundgrafik steht (Briefkopf-Kasten), gehört zum Hintergrund
            for zeile in innen:
                if gruppe.unten - gruppe.oben > zeile.unten - zeile.oben + 4:
                    zeile.rolle = "hintergrund"


# --- Absätze -------------------------------------------------------------------------------------------------------


def _listenzeichen(zeile: Zeile) -> tuple[str, float] | None:
    """Listenzeichen am Zeilenanfang und Anfang des Texts danach (x in pt), wenn eine deutliche Lücke folgt."""
    zeichen = zeile.zeichen
    i = 0
    while i < len(zeichen) and not zeichen[i][0].strip():
        i += 1
    anfang = i
    while i < len(zeichen) and zeichen[i][0].strip():
        i += 1
    kandidat = "".join(z[0] for z in zeichen[anfang:i])
    if not kandidat or not _LISTENZEICHEN_RE.match(kandidat):
        return None
    ende_x = zeichen[i - 1][2]
    while i < len(zeichen) and not zeichen[i][0].strip():
        i += 1
    if i >= len(zeichen):
        return None
    text_x = zeichen[i][1]
    # Deutlich breiter als ein Leerzeichen (ein Tabulator): sonst ist es Fließtext, der mit „1.“ beginnt
    if text_x - ende_x < zeile.groesse * 0.5:
        return None
    return kandidat, text_x


def _teilzeile(zeile: Zeile, von: int, bis: int) -> Zeile:
    """Zeichen ``von`` bis ``bis`` (ohne) einer Zeile als eigene Zeile (Zelle einer Spalte)."""
    while von < bis and not zeile.zeichen[von][0].strip():
        von += 1
    while bis > von and not zeile.zeichen[bis - 1][0].strip():
        bis -= 1
    laeufe: list[Lauf] = []
    pos = 0
    for lauf in zeile.laeufe:
        anfang, ende = pos, pos + len(lauf.text)
        a, b = max(anfang, von), min(ende, bis)
        if a < b:
            teil = Lauf(
                lauf.text[a - anfang : b - anfang],
                lauf.schrift,
                lauf.groesse,
                lauf.fett,
                lauf.kursiv,
                lauf.farbe,
                lauf.x0,
                lauf.x1,
                lauf.unterstrichen,
                quelle=lauf.quelle,
            )
            laeufe.append(teil)
        pos = ende
    zeichen = zeile.zeichen[von:bis]
    return Zeile(
        zeile.seite,
        zeile.grund,
        zeichen[0][1] if zeichen else zeile.x0,
        zeichen[-1][2] if zeichen else zeile.x1,
        zeile.oben,
        zeile.unten,
        laeufe,
        "zelle",
        zeichen=zeichen,
    )


def _segmente(zeile: Zeile) -> list[tuple[int, int, float, float]]:
    """Teile einer Zeile zwischen großen Lücken (Spalten): (Anfang, Ende, linke, rechte Kante)."""
    woerter = _woerter(zeile)
    if not woerter:
        return []
    grenze = max(3 * _leer(zeile), 8.0)
    segmente = [list(woerter[0])]
    for wort in woerter[1:]:
        if wort[2] - segmente[-1][3] > grenze:
            segmente.append(list(wort))
        else:
            segmente[-1][1], segmente[-1][3] = wort[1], wort[3]
    return [(int(a), int(b), float(x0), float(x1)) for a, b, x0, x1 in segmente]


def _tabellen_erkennen(fluss: list[Zeile], links: float, rechts: float) -> list[Tabelle]:
    """
    Spalten ohne Rahmen erkennen: Folgen von Zeilen, deren Teile an wenigen festen Stellen beginnen (mindestens zwei
    Zeilen mit mehreren Teilen). Ihre Zeilen werden zu Zellen einer Tabelle; eine Reihe beginnt nach einem größeren
    Abstand.
    """
    tabellen: list[Tabelle] = []
    nach_seite: dict[int, list[Zeile]] = {}
    for zeile in fluss:
        nach_seite.setdefault(zeile.seite, []).append(zeile)
    for seite, zeilen in nach_seite.items():
        zeilen.sort(key=lambda z: z.grund)
        teile = {id(z): _segmente(z) for z in zeilen}

        def liste(zeile: Zeile, teile: dict[int, list[tuple[int, int, float, float]]] = teile) -> bool:
            stuecke = teile[id(zeile)]
            erstes = zeile.text[stuecke[0][0] : stuecke[0][1]].strip() if stuecke else ""
            return len(stuecke) == 2 and bool(_LISTENZEICHEN_RE.match(erstes))

        mehrteilig = [z for z in zeilen if len(teile[id(z)]) >= 2 and not liste(z)]
        if len(mehrteilig) < 2:
            continue
        anfaenge = sorted(t[2] for z in mehrteilig for t in teile[id(z)])
        haufen: list[list[float]] = []
        for x in anfaenge:
            if haufen and x - haufen[-1][-1] < 3:
                haufen[-1].append(x)
            else:
                haufen.append([x])
        spalten = [statistics.median(h) for h in haufen if len(h) >= 2]
        if len(spalten) < 2:
            continue

        def spalte_von(x: float, spalten: list[float] = spalten) -> int | None:
            return next((i for i, s in enumerate(spalten) if abs(x - s) < 3), None)

        def passt(
            zeile: Zeile, spalten: list[float] = spalten, teile: dict[int, list[tuple[int, int, float, float]]] = teile
        ) -> bool:
            stuecke = teile[id(zeile)]
            if not stuecke:
                return False
            nummern = [spalte_von(t[2]) for t in stuecke]
            if any(n is None for n in nummern) or nummern != sorted(set(nummern)):  # type: ignore[type-var]
                return False
            # Jeder Teil endet vor der nächsten Spalte
            for stueck, nummer in zip(stuecke, nummern, strict=True):
                assert nummer is not None
                if nummer + 1 < len(spalten) and stueck[3] > spalten[nummer + 1] - 1:
                    return False
            return True

        folge: list[Zeile] = []
        kandidaten: list[Zeile | None] = [*zeilen, None]
        for kandidat in kandidaten:
            if kandidat is not None and passt(kandidat):
                folge.append(kandidat)
                continue
            if len(folge) >= 3 and sum(1 for z in folge if len(teile[id(z)]) >= 2 and not liste(z)) >= 2:
                tabellen.append(_tabelle_bauen(seite, folge, teile, spalten, links))
            folge = []
    return tabellen


def _tabelle_bauen(
    seite: int,
    zeilen: list[Zeile],
    teile: dict[int, list[tuple[int, int, float, float]]],
    spalten: list[float],
    links: float,
) -> Tabelle:
    abstaende = [b.grund - a.grund for a, b in pairwise(zeilen)]
    takt = statistics.median([a for a in abstaende if a > 0] or [zeilen[0].groesse * 1.2])
    reihen: list[list[list[Zeile]]] = []
    vorige: Zeile | None = None
    for zeile in zeilen:
        if vorige is None or zeile.grund - vorige.grund > takt * 1.3:
            reihen.append([[] for _ in spalten])
        for von, bis, x0, _ in teile[id(zeile)]:
            nummer = next(i for i, s in enumerate(spalten) if abs(x0 - s) < 3)
            reihen[-1][nummer].append(_teilzeile(zeile, von, bis))
        zeile.rolle = "tabelle"
        vorige = zeile
    return Tabelle(seite, [links, *spalten[1:]], reihen)


def _absaetze(
    fluss: list[Zeile], links: float, rechts: float, grafiken: list[Bereich], tabellen: list[Tabelle] | None = None
) -> list[Absatz]:
    elemente: list[tuple[int, float, Any]] = [(z.seite, z.grund, z) for z in fluss if z.rolle != "tabelle"]
    elemente += [(g.seite, g.oben, g) for g in grafiken]
    elemente += [(t.seite, _erste_zeile(t).grund - 0.01, t) for t in tabellen or []]
    elemente.sort(key=lambda e: (e[0], e[1], e[2].x0 if isinstance(e[2], Zeile) else 0))
    absaetze: list[Absatz] = []
    akt: Absatz | None = None
    for _, _, element in elemente:
        if isinstance(element, Bereich):
            absaetze.append(Absatz([], art="grafik", bereich=element))
            akt = None
            continue
        if isinstance(element, Tabelle):
            absaetze.append(Absatz([], art="tabelle", tabelle=element))
            akt = None
            continue
        zeile = element
        if akt is not None and _gehoert_dazu(akt, zeile, links, rechts):
            akt.zeilen.append(zeile)
        else:
            akt = Absatz([zeile])
            absaetze.append(akt)
    return absaetze


def _erste_zeile(tabelle: Tabelle) -> Zeile:
    return min((z for reihe in tabelle.reihen[:1] for zelle in reihe for z in zelle), key=lambda z: z.grund)


def _gehoert_dazu(absatz: Absatz, zeile: Zeile, links: float, rechts: float) -> bool:
    vorige = absatz.zeilen[-1]
    if _listenzeichen(zeile) is not None:
        return False
    haupt_alt, haupt_neu = vorige.haupt(), zeile.haupt()
    if abs(haupt_alt.groesse - haupt_neu.groesse) > 0.6 or haupt_alt.schrift != haupt_neu.schrift:
        return False
    if zeile.seite != vorige.seite:
        # Fortsetzung auf der nächsten Seite: gleicher Einzug wie die Folgezeilen
        if not (_fortsetzung_x(absatz) is None or abs(zeile.x0 - (_fortsetzung_x(absatz) or zeile.x0)) < 3):
            return False
    else:
        abstand = zeile.grund - vorige.grund
        abstaende = [b.grund - a.grund for a, b in pairwise(absatz.zeilen) if a.seite == b.seite]
        erwartet = statistics.median(abstaende) if abstaende else vorige.groesse * 1.25
        if abstand > erwartet * 1.25 + 0.5 or abstand <= 0:
            return False
        folge_x = _fortsetzung_x(absatz)
        if folge_x is not None and abs(zeile.x0 - folge_x) > 3:
            return False
        if folge_x is None and len(absatz.zeilen) == 1:
            # Zweite Zeile: links höchstens so weit eingerückt wie die erste (hängender oder Erstzeileneinzug erlaubt)
            marke = _listenzeichen(vorige)
            if marke is not None:
                if abs(zeile.x0 - marke[1]) > 3:
                    return False
            elif zeile.x0 > vorige.x0 + 40 or zeile.x0 < links - 3:
                return False
    # Hätte das erste Wort der Zeile noch in die vorige gepasst, endete diese von Hand: Absatzende nach einem
    # Satzende, sonst ein manueller Zeilenumbruch im selben Absatz
    woerter = _woerter(zeile)
    wortbreite = woerter[0][3] - woerter[0][2] if woerter else 0.0
    if vorige.x1 + _leer(vorige) + wortbreite <= rechts - 0.3:
        if vorige.text.rstrip()[-1:] in ".:!?;" or rechts - vorige.x1 > (rechts - links) * 0.6:
            return False
        vorige.harter_umbruch = True
    return True


def _fortsetzung_x(absatz: Absatz) -> float | None:
    if len(absatz.zeilen) < 2:
        return None
    return min(z.x0 for z in absatz.zeilen[1:])


# --- HTML ----------------------------------------------------------------------------------------------------------


def _masse(schluessel: str) -> tuple[float, float]:
    return SCHRIFT_MASSE.get(schluessel, (0.9, 0.22))


def _grundlinie_im_kasten(zeilenhoehe: float, schluessel: str, groesse: float) -> float:
    """
    Abstand der Grundlinie von der Oberkante des Zeilenkastens (pt), wie Chromium ihn setzt: Aufstieg und Abstieg
    auf ganze Pixel gerundet, der halbe Durchschuss abgerundet (gemessen an Gotenberg, für Arimo, Carlito und Tinos
    von 8 bis 18 pt ohne Abweichung). Die Lage auf der Seite rundet Chromium danach auf ganze Pixel (± 0,375 pt).
    """
    auf, ab = _masse(schluessel)
    px = groesse * 4 / 3
    auf_px, ab_px = round(auf * px), round(ab * px)
    hoehe_px = round(zeilenhoehe * 4 / 3 * 64) / 64
    halb = math.floor((hoehe_px - (auf_px + ab_px)) / 2)
    return (auf_px + halb) * 0.75


def _pt(wert: float) -> str:
    return f"{round(wert, 2):g}pt"


def _lauf_html(lauf: Lauf, basis_schrift: str, basis_groesse: float, text: str) -> str:
    inhalt = html.escape(text, quote=False).replace("\u00ad", "&shy;").replace("\n", "<br>")
    stile = []
    if lauf.schrift != basis_schrift:
        schrift = schriften.schrift(lauf.schrift)
        if schrift is not None:
            stile.append(f'font-family: "{schrift.name}"')
    if abs(lauf.groesse - basis_groesse) >= 0.005 and schriften.groesse_zulaessig(round(lauf.groesse, 2)):
        stile.append(f"font-size: {round(lauf.groesse, 2):g}pt")
    if lauf.farbe:
        stile.append(f"color: {lauf.farbe}")
    if lauf.luecke > 0.05:
        stile.append(f"margin-left: {_pt(lauf.luecke)}")
    if lauf.wortabstand < -0.004:
        stile.append(f"word-spacing: {round(lauf.wortabstand, 3):g}pt")
    if abs(lauf.zeichenabstand) >= 0.005:
        stile.append(f"letter-spacing: {round(lauf.zeichenabstand, 3):g}pt")
    if stile:
        inhalt = f'<span style="{"; ".join(stile)}">{inhalt}</span>'
    if lauf.unterstrichen:
        inhalt = f"<u>{inhalt}</u>"
    if lauf.kursiv:
        inhalt = f"<em>{inhalt}</em>"
    if lauf.fett:
        inhalt = f"<strong>{inhalt}</strong>"
    return inhalt


def _zeilen_text(absatz: Absatz) -> list[tuple[Lauf, str]]:
    """Läufe des Absatzes mit Zeilenübergängen (Leerzeichen oder weiches Trennzeichen)."""
    teile: list[tuple[Lauf, str]] = []
    for i, zeile in enumerate(absatz.zeilen):
        for j, lauf in enumerate(zeile.laeufe):
            text = lauf.text
            if i > 0 and j == 0 and teile:
                letzter_lauf, letzter_text = teile[-1]
                naechstes = zeile.text.split(" ", 1)[0]
                bindestrich = letzter_text.endswith("-") and len(letzter_text) > 1 and letzter_text[-2].isalnum()
                if absatz.zeilen[i - 1].harter_umbruch:
                    teile[-1] = (letzter_lauf, letzter_text + "\n")
                elif bindestrich and naechstes not in _ERGAENZUNG and naechstes[:1].islower():
                    # Silbentrennung am Zeilenende: Wort zusammenführen, Trennstelle als weiches Trennzeichen
                    teile[-1] = (letzter_lauf, letzter_text[:-1] + "\u00ad")
                elif bindestrich and naechstes not in _ERGAENZUNG and naechstes[:1].isalnum():
                    # Wortverbindung („KSD-Mitarbeitende“): Bindestrich bleibt, kein Leerzeichen
                    pass
                else:
                    teile[-1] = (letzter_lauf, letzter_text + " ")
            teile.append((lauf, text))
    return teile


def _woerter(zeile: Zeile, ab: int = 0) -> list[tuple[int, int, float, float]]:
    """Wörter einer Zeile ab Zeichen ``ab``: (Anfang, Ende, linke Kante, rechte Kante)."""
    woerter = []
    i = ab
    zeichen = zeile.zeichen
    while i < len(zeichen):
        while i < len(zeichen) and not zeichen[i][0].strip():
            i += 1
        if i >= len(zeichen):
            break
        anfang = i
        while i < len(zeichen) and zeichen[i][0].strip():
            i += 1
        woerter.append((anfang, i, zeichen[anfang][1], zeichen[i - 1][2]))
    return woerter


def _leer(zeile: Zeile) -> float:
    haupt = zeile.haupt()
    return LEERZEICHEN.get(haupt.schrift, 0.25) * haupt.groesse


def _natuerlich(woerter: list[tuple[int, int, float, float]], leer: float) -> float:
    """Breite der Wörter mit normalen Leerzeichen dazwischen."""
    return sum(w[3] - w[2] for w in woerter) + leer * max(0, len(woerter) - 1)


def _stauchen(absatz: Absatz, rechts: float, text_ab: int) -> None:
    """
    Word staucht im Blocksatz die Wortabstände einer Zeile, wenn ein Wort sonst nicht mehr passt. Die Satz-Kette bildet
    das je Zeile über einen negativen Wortabstand ihrer Läufe nach: gerade so viel, dass die Zeile des Originals passt,
    und nicht so viel, dass das erste Wort der nächsten Zeile noch hochrutscht.
    """
    zeilen = absatz.zeilen
    for nr, zeile in enumerate(zeilen[:-1]):
        woerter = _woerter(zeile, text_ab if nr == 0 else 0)
        if len(woerter) < 2:
            continue
        leer = _leer(zeile)
        breite = rechts - woerter[0][2]
        natuerlich = _natuerlich(woerter, leer)
        # Mit Zeichenabstand (auf Pixel gerundete Breiten) wird jede Zeile um ihn je Zeichen schmaler oder breiter
        abstand = zeile.laeufe[0].zeichenabstand if zeile.laeufe else 0.0
        natuerlich += abstand * (sum(w[1] - w[0] for w in woerter) + len(woerter) - 1)
        abstaende = len(woerter) - 1
        # Auch knapp passende Zeilen etwas stauchen: Rundungen im Browser kosten sonst das letzte Wort
        noetig = (natuerlich - breite + 0.3) / abstaende
        if noetig <= 0:
            continue
        wert = noetig + 0.02
        naechste = _woerter(zeilen[nr + 1])
        if naechste:
            mit_naechstem = natuerlich + leer + (naechste[0][3] - naechste[0][2])
            grenze = (mit_naechstem - breite) / (abstaende + 1)
            wert = min(wert, max(noetig + 0.005, grenze - 0.02))
        for lauf in zeile.laeufe:
            lauf.wortabstand = -round(wert, 3)


def _zeichenabstand(zeile: Zeile, ab: int, nur_im_wort: bool = False) -> None:
    """
    Breite der Zeile wie im Original: Manche Programme (Word im Kompatibilitätsmodus) runden jede Zeichenbreite auf
    ganze Bildschirmpixel, die Zeile wird dadurch schmaler oder breiter als mit den Breiten der Schrift. Der Mittelwert
    der Abweichung zwischen benachbarten Zeichen wird als Zeichenabstand der Läufe dieser Zeile übernommen; dann
    passt in die Zeile genau, was im Original darin stand.
    """
    leer = _leer(zeile)
    zeichen = zeile.zeichen
    abweichungen = []
    for k in range(max(ab, 0), len(zeichen) - 1):
        a, b = zeichen[k], zeichen[k + 1]
        if b[1] - a[2] > max(2 * leer, 4.0):
            continue  # Tabulator oder große Lücke
        if nur_im_wort and (not a[0].strip() or not b[0].strip()):
            continue  # Blocksatz: Wortabstände sind gedehnt
        abweichungen.append(b[1] - a[2])
    if len(abweichungen) < 8:
        return
    wert = sum(abweichungen) / len(abweichungen)
    if abs(wert) < 0.01 or abs(wert) > 0.6:
        return
    for lauf in zeile.laeufe:
        lauf.zeichenabstand = round(wert, 3)


def _luecken_setzen(zeile: Zeile, ab: int, rechts: float, tab_frei: bool) -> float | None:
    """
    Große Lücken innerhalb einer Zeile (kein Blocksatz) übernehmen. Eine einzelne Lücke wird zum Tabulator (Rückgabe:
    Tabulatorweite in pt, gemessen vom Zeilenanfang), wie Word sie setzt, etwa „Ratsantrag ⇥ Münster, 28.09.2026“.
    Mehrere Lücken (Unterschriften nebeneinander) werden zum Abstand vor dem folgenden Lauf.
    """
    leer = _leer(zeile)
    woerter = _woerter(zeile, ab)
    luecken = [
        (vorher, nachher) for vorher, nachher in pairwise(woerter) if nachher[2] - vorher[3] > max(3 * leer, 6.0)
    ]
    if not luecken:
        return None
    if len(luecken) == 1 and tab_frei and ab == 0:
        vorher, nachher = luecken[0]
        _text_ersetzen(zeile, vorher[1], nachher[0], "\t")
        return nachher[2] - zeile.x0
    schnitte = {nachher[0]: max(0.0, nachher[2] - vorher[3] - leer) for vorher, nachher in luecken}
    neu: list[Lauf] = []
    pos = 0
    for lauf in zeile.laeufe:
        text = lauf.text
        grenzen = [0, *[i - pos for i in sorted(schnitte) if pos < i < pos + len(text)], len(text)]
        for a, b in pairwise(grenzen):
            if a == b:
                continue
            neu.append(
                Lauf(
                    text[a:b],
                    lauf.schrift,
                    lauf.groesse,
                    lauf.fett,
                    lauf.kursiv,
                    lauf.farbe,
                    lauf.x0,
                    lauf.x1,
                    lauf.unterstrichen,
                    luecke=schnitte.get(pos + a, 0.0),
                )
            )
        pos += len(text)
    zeile.laeufe = neu
    return None


def _text_ersetzen(zeile: Zeile, von: int, bis: int, ersatz: str) -> None:
    """Zeichen ``von`` bis ``bis`` (ohne) der Zeile durch ``ersatz`` ersetzen (im Lauf, in dem ``von`` liegt)."""
    pos = 0
    for lauf in zeile.laeufe:
        anfang, ende = pos, pos + len(lauf.text)
        if anfang <= von < ende or (von == ende and lauf is zeile.laeufe[-1]):
            lokal_von = von - anfang
            lokal_bis = min(bis, ende) - anfang
            lauf.text = lauf.text[:lokal_von] + ersatz + lauf.text[lokal_bis:]
            rest = bis - ende
            pos = ende
            # Was über den Lauf hinausreicht (nur Leerzeichen), in den folgenden Läufen entfernen
            for folgender in zeile.laeufe[zeile.laeufe.index(lauf) + 1 :]:
                if rest <= 0:
                    break
                weg = min(rest, len(folgender.text))
                folgender.text = folgender.text[weg:]
                rest -= weg
            zeile.laeufe = [lauf_ for lauf_ in zeile.laeufe if lauf_.text]
            return
        pos = ende


def _absatz_html(
    absatz: Absatz,
    vorher: Absatz | None,
    links: float,
    rechts: float,
    basis_schrift: str,
    basis_groesse: float,
    zeilenhoehen: dict[tuple[str, float], float],
    pixel: bool = False,
    abstand_vor: float | None = None,
) -> str:
    erste = absatz.zeilen[0]
    haupt = erste.haupt()
    groesse = haupt.groesse
    schluessel = haupt.schrift
    abstaende = [b.grund - a.grund for a, b in pairwise(absatz.zeilen) if a.seite == b.seite]
    zeilenhoehe = (
        statistics.median(abstaende) if abstaende else zeilenhoehen.get((schluessel, round(groesse, 1)), groesse * 1.15)
    )
    stile: list[str] = []
    # Senkrechte Lage: Abstand so, dass die erste Grundlinie genau an der Stelle des Originals steht
    oben_kasten = erste.grund - _grundlinie_im_kasten(zeilenhoehe, schluessel, groesse)
    if abstand_vor is not None:
        abstand = oben_kasten - abstand_vor  # erster Absatz einer Tabellenzelle: Abstand zur Reihe darüber
    elif vorher is not None and _letzte_seite(vorher) == erste.seite:
        unten_vorher = _unterkante(vorher, zeilenhoehen)
        abstand = oben_kasten - unten_vorher
    else:
        abstand = 0.0  # am Seitenanfang setzt die Satz-Kette den Block an den Satzspiegel
    if abs(abstand) >= 0.05:
        stile.append(f"margin-top: {_pt(abstand)}")
    stile.append("margin-bottom: 0pt")
    stile.append(f"line-height: {_pt(zeilenhoehe)}")
    if schluessel != basis_schrift:
        schrift = schriften.schrift(schluessel)
        if schrift is not None:
            stile.append(f'font-family: "{schrift.name}"')
    if abs(groesse - basis_groesse) >= 0.005 and schriften.groesse_zulaessig(round(groesse, 2)):
        stile.append(f"font-size: {round(groesse, 2):g}pt")

    marker = _listenzeichen(erste)
    folge_x = _fortsetzung_x(absatz)
    ausrichtung = _ausrichtung(absatz, links, rechts)
    tabweite: float | None = None
    # Index des ersten Zeichens nach dem Listenzeichen (dort beginnt der Blocksatz der ersten Zeile)
    text_ab = 0
    if marker is not None:
        text_ab = next((i for i, z in enumerate(erste.zeichen) if abs(z[1] - marker[1]) < 0.01), 0)
    if ausrichtung == "justify":
        if pixel:
            for nr, zeile in enumerate(absatz.zeilen):
                _zeichenabstand(zeile, text_ab if nr == 0 else 0, nur_im_wort=True)
        kante = rechts
        if absatz.rand_rechts is not None and absatz.rand_rechts < rechts - 0.5:
            kante = absatz.rand_rechts + 0.25
            stile.append(f"padding-right: {_pt(rechts - kante)}")
        _stauchen(absatz, kante, text_ab)
    else:
        for nr, zeile in enumerate(absatz.zeilen):
            if pixel:
                _zeichenabstand(zeile, text_ab if nr == 0 else 0)
            tab = _luecken_setzen(
                zeile, text_ab if nr == 0 else 0, rechts, tab_frei=marker is None and tabweite is None
            )
            if tab is not None:
                tabweite = tab
    teile = _zeilen_text(absatz)
    if marker is not None:
        zeichen, text_x = marker
        einzug = text_x - links
        tab = text_x - erste.x0
        stile.append(f"padding-left: {_pt(einzug)}")
        stile.append(f"text-indent: {_pt(-tab)}")
        stile.append(f"tab-size: {_pt(tab)}")
        # Das Listenzeichen und die Lücke als Tabulator
        erster_lauf, erster_text = teile[0]
        rest = erster_text.split(" ", 1)[1] if " " in erster_text else ""
        if erster_text.strip() == zeichen:
            teile[0] = (erster_lauf, zeichen + "\t")
            # Folgendes Leerzeichen vor dem Text entfernen
            if len(teile) > 1:
                teile[1] = (teile[1][0], teile[1][1].lstrip())
        else:
            teile[0] = (erster_lauf, zeichen + "\t" + rest.lstrip())
    else:
        einzug_x = folge_x if folge_x is not None else erste.x0
        if ausrichtung in ("right", "center"):
            einzug_x = links
        einzug = einzug_x - links
        if einzug > 0.3:
            stile.append(f"padding-left: {_pt(einzug)}")
        erstzeile = erste.x0 - einzug_x
        if folge_x is not None and abs(erstzeile) > 0.3:
            stile.append(f"text-indent: {_pt(erstzeile)}")
    if tabweite is not None:
        stile.append(f"tab-size: {_pt(tabweite)}")
    if ausrichtung != "left":
        stile.append(f"text-align: {ausrichtung}")
    inhalt = "".join(_lauf_html(lauf, schluessel, groesse, text) for lauf, text in teile if text)
    return f'<p style="{"; ".join(stile)}">{inhalt}</p>'


def _tabelle_html(
    absatz: Absatz,
    vorher: Absatz | None,
    links: float,
    rechts: float,
    basis_schrift: str,
    basis_groesse: float,
    zeilenhoehen: dict[tuple[str, float], float],
    pixel: bool,
) -> str:
    """Spalten ohne Rahmen als Tabelle: feste Spaltenbreiten, Zellen ohne Innenabstand, Abstände über die Absätze."""
    tabelle = absatz.tabelle
    assert tabelle is not None
    grenzen = [*tabelle.spalten, rechts]
    breiten_px = [max(1, round((b - a) * 4 / 3)) for a, b in pairwise(grenzen)]
    erste = _erste_zeile(tabelle)
    if vorher is not None and _letzte_seite(vorher) == tabelle.seite:
        unten = _unterkante(vorher, zeilenhoehen)
    else:
        haupt = erste.haupt()
        unten = erste.grund - _grundlinie_im_kasten(
            zeilenhoehen.get((haupt.schrift, round(haupt.groesse, 1)), haupt.groesse * 1.15),
            haupt.schrift,
            haupt.groesse,
        )
    tabelle.oben = unten
    reihen_html = []
    for reihe in tabelle.reihen:
        zellen_html = []
        reihe_unten = unten
        for nummer, zellzeilen in enumerate(reihe):
            links_zelle, rechts_zelle = grenzen[nummer], grenzen[nummer + 1]
            inhalt = ""
            if zellzeilen:
                absaetze = _absaetze(zellzeilen, links_zelle, rechts_zelle, [])
                teile = []
                vorher_zelle: Absatz | None = None
                for zellabsatz in absaetze:
                    teile.append(
                        _absatz_html(
                            zellabsatz,
                            vorher_zelle,
                            links_zelle,
                            rechts_zelle,
                            basis_schrift,
                            basis_groesse,
                            zeilenhoehen,
                            pixel,
                            abstand_vor=unten if vorher_zelle is None else None,
                        )
                    )
                    vorher_zelle = zellabsatz
                inhalt = "".join(teile)
                reihe_unten = max(reihe_unten, _unterkante(absaetze[-1], zeilenhoehen))
            else:
                inhalt = "<p></p>"
            zellen_html.append(f'<td colwidth="{breiten_px[nummer]}">{inhalt}</td>')
        reihen_html.append(f"<tr>{''.join(zellen_html)}</tr>")
        unten = reihe_unten
    tabelle.unten = unten
    return f'<table data-rahmen="ohne"><tbody>{"".join(reihen_html)}</tbody></table>'


def _letzte_seite(absatz: Absatz) -> int:
    if absatz.art == "grafik" and absatz.bereich is not None:
        return absatz.bereich.seite
    if absatz.art == "tabelle" and absatz.tabelle is not None:
        return absatz.tabelle.seite
    return absatz.zeilen[-1].seite


def _unterkante(absatz: Absatz, zeilenhoehen: dict[tuple[str, float], float]) -> float:
    """Unterkante des letzten Zeilenkastens (bzw. der Grafik oder Tabelle) im Original."""
    if absatz.art == "grafik" and absatz.bereich is not None:
        return absatz.bereich.unten
    if absatz.art == "tabelle" and absatz.tabelle is not None:
        return absatz.tabelle.unten
    letzte = absatz.zeilen[-1]
    haupt = absatz.zeilen[0].haupt()
    abstaende = [b.grund - a.grund for a, b in pairwise(absatz.zeilen) if a.seite == b.seite]
    zeilenhoehe = (
        statistics.median(abstaende)
        if abstaende
        else zeilenhoehen.get((haupt.schrift, round(haupt.groesse, 1)), haupt.groesse * 1.15)
    )
    return letzte.grund - _grundlinie_im_kasten(zeilenhoehe, haupt.schrift, haupt.groesse) + zeilenhoehe


def _ausrichtung(absatz: Absatz, links: float, rechts: float) -> str:
    zeilen = absatz.zeilen
    mitte = (links + rechts) / 2
    if all(abs(z.x1 - rechts) < 2.5 for z in zeilen) and any(z.x0 > links + 20 for z in zeilen):
        return "right"
    if all(abs((z.x0 + z.x1) / 2 - mitte) < 3 for z in zeilen) and all(z.x0 > links + 10 for z in zeilen):
        return "center"
    innen = zeilen[:-1]
    if len(innen) >= 2:
        # Blocksatz: die meisten Zeilen enden an derselben Kante, am Satzspiegel oder kurz davor (Einzug rechts)
        kante = statistics.median(z.x1 for z in innen)
        am_rand = [z for z in innen if abs(z.x1 - kante) < 0.8]
        if kante > rechts - 20 and len(am_rand) >= max(2, round(len(innen) * 0.6)):
            absatz.rand_rechts = max(z.x1 for z in am_rand)
            # Zeilen, die nicht bis zur Kante reichen, enden mit einem manuellen Zeilenumbruch: Word dehnt sie nicht
            for zeile in innen:
                zeile.harter_umbruch = abs(zeile.x1 - kante) >= 1.5
            return "justify"
    if len(zeilen) == 2 and abs(zeilen[0].x1 - rechts) < 1.0:
        return "justify"
    return "left"


def _grafik_html(
    absatz: Absatz, vorher: Absatz | None, links: float, zeilenhoehen: dict[tuple[str, float], float]
) -> str:
    bereich = absatz.bereich
    assert bereich is not None
    stile = []
    if vorher is not None and _letzte_seite(vorher) == bereich.seite:
        abstand = bereich.oben - _unterkante(vorher, zeilenhoehen)
        stile.append(f"margin-top: {_pt(abstand)}")
    if bereich.x0 - links > 0.3:
        stile.append(f"margin-left: {_pt(bereich.x0 - links)}")
    stile.append("margin-bottom: 0pt")
    stile.append(f"width: {_pt(bereich.x1 - bereich.x0)}")
    daten = base64.b64encode(absatz.bild).decode("ascii")
    return f'<img src="data:image/png;base64,{daten}" alt="" style="{"; ".join(stile)}">'


# --- Hintergrund und Grafiken --------------------------------------------------------------------------------------


def _zusammenfassen(grafiken: list[Bereich], fluss: list[Zeile]) -> list[Bereich]:
    """
    Grafiken im Fluss, die dicht untereinander stehen (Zeilen einer Tabelle mit eigenem Rahmen), zu einem Bild
    zusammenfassen, wenn kein Fließtext dazwischen steht: ein Bild statt vieler, ohne Rundung je Zeile.
    """
    ergebnis: list[Bereich] = []
    for grafik in sorted(grafiken, key=lambda g: (g.seite, g.oben)):
        vorige = ergebnis[-1] if ergebnis else None
        if (
            vorige is not None
            and vorige.seite == grafik.seite
            and 0 <= grafik.oben - vorige.unten < 8
            and min(vorige.x1, grafik.x1) - max(vorige.x0, grafik.x0)
            > 0.8 * min(vorige.x1 - vorige.x0, grafik.x1 - grafik.x0)
            and not any(z.seite == grafik.seite and vorige.unten < z.grund < grafik.oben for z in fluss)
        ):
            vorige.x0, vorige.x1 = min(vorige.x0, grafik.x0), max(vorige.x1, grafik.x1)
            vorige.unten = max(vorige.unten, grafik.unten)
            continue
        ergebnis.append(grafik)
    return ergebnis


def _grafik_rendern(pdf: Any, bereich: Bereich) -> bytes:
    seite = pdf[bereich.seite]
    breite, hoehe = seite.get_size()
    rand = 0.8
    crop = (
        max(0.0, bereich.x0 - rand),
        max(0.0, hoehe - bereich.unten - rand),
        max(0.0, breite - bereich.x1 - rand),
        max(0.0, bereich.oben - rand),
    )
    bild = seite.render(scale=GRAFIK_SKALA, crop=crop).to_pil().convert("RGB")
    puffer = io.BytesIO()
    # Tabellen und Kästen sind meist schwarz-weiß: dann Graustufen (ein Drittel der Größe); Farbbilder als Palette
    from PIL import ImageChops

    rot, gruen, blau = bild.split()
    if not ImageChops.difference(rot, gruen).getbbox() and not ImageChops.difference(rot, blau).getbbox():
        bild = bild.convert("L")
    elif bild.width * bild.height > 250_000:
        bild = bild.convert("P", palette=1, colors=256)  # 1 = Image.Palette.ADAPTIVE
    bild.save(puffer, format="PNG", optimize=True)
    # Bereich an die gerenderte Kante anpassen
    bereich.x0, bereich.x1 = crop[0], breite - crop[2]
    bereich.oben, bereich.unten = crop[3], hoehe - crop[1]
    return puffer.getvalue()


def _entfernen(raw: Any, seite: Any, hoehe: float, weg_zeilen: list[Zeile], weg_bereiche: list[Bereich]) -> None:
    """Fließtext und Grafiken im Fluss aus einer Seite des Hintergrunds entfernen."""
    zu_entfernen = []
    for obj in seite.get_objects(max_depth=4):
        if obj.type == raw.FPDF_PAGEOBJ_FORM:
            continue
        links_x, b, r, t = obj.get_bounds()
        oben, unten = hoehe - t, hoehe - b
        mx, my = (links_x + r) / 2, (oben + unten) / 2
        weg = False
        if obj.type == raw.FPDF_PAGEOBJ_TEXT:
            for zeile in weg_zeilen:
                if zeile.oben - 1.5 <= my <= zeile.unten + 1.5 and zeile.x0 - 2 <= mx <= zeile.x1 + 2:
                    weg = True
                    break
        if not weg:
            for bereich in weg_bereiche:
                im_bereich = bereich.x0 - 1 <= mx <= bereich.x1 + 1 and bereich.oben - 1 <= my <= bereich.unten + 1
                if im_bereich and (obj.type != raw.FPDF_PAGEOBJ_TEXT or bereich.rolle == "fluss"):
                    weg = True
                    break
        if weg:
            zu_entfernen.append(obj)
    for obj in zu_entfernen:
        eltern = obj.container if hasattr(obj, "container") else None
        if eltern is not None and getattr(eltern, "type", None) == raw.FPDF_PAGEOBJ_FORM:
            raw.FPDFFormObj_RemoveObject(eltern.raw, obj.raw)
        else:
            seite.remove_obj(obj)
    seite.gen_content()


# --- Ablauf --------------------------------------------------------------------------------------------------------


def uebernehmen(daten: bytes) -> Ergebnis:
    """PDF analysieren und in Fließtext (HTML) und Hintergrund (PDF) teilen. Wirft ``NichtGeeignetError``."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    try:
        pdf = pdfium.PdfDocument(daten)
    except pdfium.PdfiumError as exc:
        raise NichtGeeignetError("PDF nicht lesbar") from exc
    try:
        seiten = len(pdf)
        if seiten == 0 or seiten > MAX_SEITEN:
            raise NichtGeeignetError("zu viele Seiten")
        breite, hoehe = pdf[0].get_size()
        unbekannt: Counter[str] = Counter()
        zeilen: list[Zeile] = []
        bereiche: list[Bereich] = []
        for nr in range(seiten):
            seite = pdf[nr]
            w, h = seite.get_size()
            if abs(w - breite) > 2 or abs(h - hoehe) > 2:
                raise NichtGeeignetError("unterschiedliche Seitengrößen")
            zeilen += _zeilen_der_seite(raw, seite, nr, hoehe, unbekannt)
            bereiche += _bereiche_der_seite(raw, seite, nr, hoehe)
        if sum(len(z.text) for z in zeilen) < 20:
            raise NichtGeeignetError("kein Text")

        _fett_aus_breiten(zeilen)
        _unterstreichungen(zeilen, bereiche)
        seitenzahl = _kopf_und_fuss(zeilen, seiten, hoehe)
        kandidaten = [z for z in zeilen if z.rolle == "fluss"]
        links, rechts = _spalte(kandidaten, breite)
        gruppen = _gruppen(bereiche)
        _klassifizieren(zeilen, gruppen, links, rechts, breite)
        # Zweiter Durchgang mit dem bereinigten Fluss
        fluss = [z for z in zeilen if z.rolle == "fluss"]
        links, rechts = _spalte(fluss, breite)
        im_fluss = _zusammenfassen([g for g in gruppen if g.rolle == "fluss"], fluss)
        tabellen = _tabellen_erkennen(fluss, links, rechts)
        absaetze = _absaetze(fluss, links, rechts, im_fluss, tabellen)
        # Zeichenbreiten auf Pixel gerundet (Word im Kompatibilitätsmodus): Breite je Zeile über den Zeichenabstand
        pixel = _pixelgerundet(fluss)
        for absatz in absaetze:
            if absatz.art == "grafik" and absatz.bereich is not None:
                absatz.bild = _grafik_rendern(pdf, absatz.bereich)

        # Grundschrift: häufigste Schrift und Größe im Fließtext
        zaehler: Counter[tuple[str, float]] = Counter()
        for zeile in fluss:
            for lauf in zeile.laeufe:
                zaehler[(lauf.schrift, round(lauf.groesse, 2))] += len(lauf.text)
        (basis_schrift, basis_groesse), _ = zaehler.most_common(1)[0]
        # Zeilenhöhe je Schrift und Größe aus mehrzeiligen Absätzen (für einzeilige Absätze)
        hoehen: dict[tuple[str, float], list[float]] = {}
        for absatz in absaetze:
            if absatz.art != "text":
                continue
            haupt = absatz.zeilen[0].haupt()
            for a, b in pairwise(absatz.zeilen):
                if a.seite == b.seite:
                    hoehen.setdefault((haupt.schrift, round(haupt.groesse, 1)), []).append(b.grund - a.grund)
        zeilenhoehen = {k: statistics.median(v) for k, v in hoehen.items()}

        teile: list[str] = []
        vorher: Absatz | None = None
        unten_grenze = _untere_grenze(absaetze, zeilenhoehen)
        for absatz in absaetze:
            if vorher is not None and _erzwungener_umbruch(vorher, absatz, unten_grenze, zeilenhoehen):
                teile.append('<div data-page-break="true"></div>')
            if absatz.art == "grafik":
                teile.append(_grafik_html(absatz, vorher, links, zeilenhoehen))
            elif absatz.art == "tabelle":
                teile.append(
                    _tabelle_html(absatz, vorher, links, rechts, basis_schrift, basis_groesse, zeilenhoehen, pixel)
                )
            else:
                teile.append(
                    _absatz_html(absatz, vorher, links, rechts, basis_schrift, basis_groesse, zeilenhoehen, pixel)
                )
            vorher = absatz

        # Satzspiegel: oben je erste und Folgeseiten, unten aus der tiefsten Zeile
        oben_erste, oben_folge, unten_max = _satzspiegel(absaetze, zeilenhoehen, seiten, hoehe)
        raender = {
            "oben": oben_erste / PT_JE_MM,
            "oben_folge": oben_folge / PT_JE_MM,
            "unten": (hoehe - unten_max) / PT_JE_MM,
            "unten_folge": (hoehe - unten_max) / PT_JE_MM,
            "links": links / PT_JE_MM,
            "rechts": (breite - rechts) / PT_JE_MM,
        }
        if seitenzahl is not None:
            mitte = (links + rechts) / 2
            x_mitte = seitenzahl["mitte_mm"] * PT_JE_MM
            if abs(x_mitte - (breite / 2)) < 6 or abs(x_mitte - mitte) < 6:
                seitenzahl["ausrichtung"] = "mitte"
            elif abs(seitenzahl["rechts_mm"] * PT_JE_MM - rechts) < 6 or seitenzahl["x_mm"] * PT_JE_MM > breite / 2:
                seitenzahl["ausrichtung"] = "rechts"
            else:
                seitenzahl["ausrichtung"] = "links"

        # Hintergrund: Originalseiten ohne Fließtext und ohne Grafiken im Fluss
        weg_zeilen_je_seite: dict[int, list[Zeile]] = {}
        for zeile in zeilen:
            if zeile.rolle in ("fluss", "grafik", "seitenzahl", "tabelle"):
                weg_zeilen_je_seite.setdefault(zeile.seite, []).append(zeile)
        weg_bereiche_je_seite: dict[int, list[Bereich]] = {}
        for bereich in im_fluss:
            weg_bereiche_je_seite.setdefault(bereich.seite, []).append(bereich)
        for bereich in bereiche:
            if bereich.rolle == "unterstreichung":
                weg_bereiche_je_seite.setdefault(bereich.seite, []).append(
                    Bereich(
                        bereich.seite, bereich.x0, bereich.oben, bereich.x1, bereich.unten, "pfad", "unterstreichung"
                    )
                )
        hintergrund = pdfium.PdfDocument(daten)
        try:
            for nr in range(seiten):
                _entfernen(
                    raw, hintergrund[nr], hoehe, weg_zeilen_je_seite.get(nr, []), weg_bereiche_je_seite.get(nr, [])
                )
            puffer = io.BytesIO()
            hintergrund.save(puffer)
            hintergrund_pdf = puffer.getvalue()
        finally:
            hintergrund.close()

        hinweise = []
        if unbekannt:
            namen = ", ".join(sorted(unbekannt)[:3])
            hinweise.append(f"Nicht enthaltene Schriften ({namen}) wurden durch Arimo ersetzt.")
        return Ergebnis(
            html="\n".join(teile),
            hintergrund_pdf=hintergrund_pdf,
            raender={k: round(v, 3) for k, v in raender.items()},
            schrift=basis_schrift,
            groesse_pt=float(basis_groesse),
            seiten=seiten,
            hintergrund_seiten=list(range(seiten)),
            seitenzahl=seitenzahl,
            seitenbreite_mm=round(breite / PT_JE_MM, 2),
            seitenhoehe_mm=round(hoehe / PT_JE_MM, 2),
            hinweise=hinweise,
            unterschneidung=not pixel and _mit_unterschneidung(fluss),
        )
    finally:
        pdf.close()


def _pixelgerundet(zeilen: list[Zeile]) -> bool:
    """
    Hat das Original die Zeichen auf ganze Bildschirmpixel (96 dpi, 0,75 pt) gesetzt? Word im Kompatibilitätsmodus
    rundet jede Zeichenbreite so; dann stehen fast alle Zeichen im Abstand eines Vielfachen von 0,75 pt.
    """
    schritte = treffer = 0
    for zeile in zeilen:
        for a, b in pairwise(zeile.zeichen):
            if not a[0].strip() or not b[0].strip():
                continue
            schritt = (b[1] - a[1]) / 0.75
            schritte += 1
            if abs(schritt - round(schritt)) < 0.04:
                treffer += 1
    return schritte > 100 and treffer / schritte > 0.8


def _mit_unterschneidung(zeilen: list[Zeile]) -> bool:
    """
    Setzt das Original mit Unterschneidung? Ohne sie folgt jedes Zeichen eines Worts genau auf das Ende des vorigen;
    mit ihr rücken Paare wie „To“ oder „09“ zusammen. Ab einem kleinen Anteil verschobener Paare gilt das Dokument als
    unterschnitten.
    """
    paare = verschoben = 0
    for zeile in zeilen:
        zeichen = zeile.zeichen
        for a, b in pairwise(zeichen):
            if not a[0].strip() or not b[0].strip():
                continue
            paare += 1
            if abs(b[1] - a[2]) > 0.05:
                verschoben += 1
    return paare > 50 and verschoben / paare > 0.02


def _erste_seite(absatz: Absatz) -> int:
    if absatz.art == "grafik" and absatz.bereich is not None:
        return absatz.bereich.seite
    if absatz.art == "tabelle" and absatz.tabelle is not None:
        return absatz.tabelle.seite
    return absatz.zeilen[0].seite


def _untere_grenze(absaetze: list[Absatz], zeilenhoehen: dict[tuple[str, float], float]) -> float:
    """Tiefste Unterkante des Fließtexts im Original (Näherung der unteren Satzspiegelkante)."""
    tiefste = 0.0
    for absatz in absaetze:
        if absatz.art == "text":
            tiefste = max(tiefste, _unterkante(absatz, zeilenhoehen))
        elif absatz.art == "grafik" and absatz.bereich is not None:
            tiefste = max(tiefste, absatz.bereich.unten)
    return tiefste


def _erzwungener_umbruch(
    vorher: Absatz, absatz: Absatz, unten_grenze: float, zeilenhoehen: dict[tuple[str, float], float]
) -> bool:
    """
    Beginnt ``absatz`` im Original auf einer neuen Seite, obwohl auf der vorigen noch Platz war? Dann kam der Umbruch
    nicht aus Platzmangel (manueller Seitenumbruch, „Absätze nicht trennen“, Tabellenzeile zusammenhalten); er wird
    als Seitenumbruch übernommen. Ein Umbruch aus Platzmangel ergibt sich im Satz von selbst.
    """
    if absatz.art == "text" and any(z.seite != absatz.zeilen[0].seite for z in absatz.zeilen):
        return False
    if _erste_seite(absatz) <= _letzte_seite(vorher):
        return False
    platz = unten_grenze - _unterkante(vorher, zeilenhoehen)
    if absatz.art == "text":
        haupt = absatz.zeilen[0].haupt()
        takt = zeilenhoehen.get((haupt.schrift, round(haupt.groesse, 1)), haupt.groesse * 1.2)
        teilbar = len(absatz.zeilen) >= 4
        bedarf = takt * 2 if teilbar else takt * len(absatz.zeilen)
    elif absatz.art == "grafik" and absatz.bereich is not None:
        bedarf = absatz.bereich.unten - absatz.bereich.oben
    elif absatz.art == "tabelle" and absatz.tabelle is not None:
        erste = absatz.tabelle.reihen[0]
        zeilen_der_reihe = max(len(zelle) for zelle in erste) or 1
        bedarf = zeilen_der_reihe * _erste_zeile(absatz.tabelle).groesse * 1.2
    else:
        return False
    return platz > bedarf + 2


def _satzspiegel(
    absaetze: list[Absatz], zeilenhoehen: dict[tuple[str, float], float], seiten: int, hoehe: float
) -> tuple[float, float, float]:
    oben_je_seite: dict[int, float] = {}
    unten_max = 0.0
    for absatz in absaetze:
        if absatz.art == "grafik" and absatz.bereich is not None:
            b = absatz.bereich
            oben_je_seite.setdefault(b.seite, b.oben)
            unten_max = max(unten_max, b.unten)
            continue
        if absatz.art == "tabelle" and absatz.tabelle is not None:
            t = absatz.tabelle
            if t.seite not in oben_je_seite or t.oben < oben_je_seite[t.seite]:
                oben_je_seite[t.seite] = t.oben
            unten_max = max(unten_max, t.unten)
            continue
        haupt = absatz.zeilen[0].haupt()
        abstaende = [b.grund - a.grund for a, b in pairwise(absatz.zeilen) if a.seite == b.seite]
        zh = (
            statistics.median(abstaende)
            if abstaende
            else zeilenhoehen.get((haupt.schrift, round(haupt.groesse, 1)), haupt.groesse * 1.15)
        )
        versatz = _grundlinie_im_kasten(zh, haupt.schrift, haupt.groesse)
        for zeile in absatz.zeilen:
            oben = zeile.grund - versatz
            if zeile.seite not in oben_je_seite or oben < oben_je_seite[zeile.seite]:
                oben_je_seite[zeile.seite] = oben
            unten_max = max(unten_max, oben + zh)
    oben_erste = oben_je_seite.get(0, 72.0)
    folge = [v for k, v in oben_je_seite.items() if k > 0]
    oben_folge = min(folge) if folge else oben_erste
    return oben_erste, oben_folge, unten_max + 0.2

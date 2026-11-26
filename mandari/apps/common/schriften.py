# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schriften für Dokumente (Issue #1584): ein fester Satz freier Schriften, die mandari selbst ausliefert.

Aufgenommen werden nur Schriften ohne reservierten Namen (OFL „Reserved Font Name“), weil mandari sie als Untermenge
ausliefert; eine Ausnahme mit Originaldateien ist Carlito (``unveraendert``).

Editor, Seitenansicht, PDF und die Office-Exporte nutzen dieselben Dateien (``static/vendor/schriften/``). Nur so
brechen Zeilen und Seiten überall an derselben Stelle um. Die Dateien entstehen reproduzierbar mit
``scripts/schriften_bauen.py`` aus einem festen Stand von github.com/google/fonts (Lateinisch, ohne Hinting, woff2).

Fünf Schriften sind metrisch kompatibel zu verbreiteten Office-Schriften (gleiche Zeichenbreiten): Arimo zu Arial,
Tinos zu Times New Roman, Carlito zu Calibri, Caladea zu Cambria und Cousine zu Courier New. In Word- und ODT-Exporten
steht deshalb der Office-Name (``office_name``); Word setzt den Text dann mit dem Original fast genau wie das PDF.

Das Modul hat keine Django-Abhängigkeiten: Das Bauskript lädt es direkt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Schriftgrößen der Auswahl im Editor (Punkt)
GROESSEN: tuple[float, ...] = (8, 9, 10, 10.5, 11, 12, 13, 14, 16, 18, 20, 24, 28, 32, 36)
#: Kleinste und größte zulässige Schriftgröße im Text (Punkt), auch für eingefügte oder importierte Angaben
GROESSE_MIN = 6.0
GROESSE_MAX = 96.0

#: Schrift, wenn ein Dokument oder Briefpapier keine bekannte Schrift nennt (früherer Vorgabewert: Arial)
STANDARD = "arimo"


@dataclass(frozen=True)
class Schnitt:
    """Ein Schnitt einer Schrift (Gewicht, kursiv) und die Quelldatei im Verzeichnis von google/fonts."""

    gewicht: int
    kursiv: bool
    quelle: str
    #: Feste Werte der Achsen einer variablen Quelldatei (Gewicht kommt aus ``gewicht``)
    achsen: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class Schrift:
    schluessel: str
    #: Familienname in CSS (``@font-face``) und in der Auswahl
    name: str
    #: serifenlos, serif oder dicktengleich
    art: str
    #: Kurzer Hinweis in der Auswahl
    hinweis: str
    #: Verzeichnis in github.com/google/fonts
    verzeichnis: str
    #: SPDX-Lizenz der Schriftdateien
    lizenz: str
    #: Urheberangabe für REUSE (aus der Lizenzdatei der Schrift)
    urheber: str
    schnitte: tuple[Schnitt, ...]
    #: Name in Word- und ODT-Exporten (bei metrisch kompatiblen Schriften die Office-Schrift)
    office_name: str = ""
    #: Weitere Namen, die auf diese Schrift abgebildet werden (Briefpapier, eingefügter Text)
    ersetzt: tuple[str, ...] = ()
    #: Originaldateien ausliefern (TrueType, keine Untermenge): Pflicht bei einem reservierten Schriftnamen der OFL,
    #: denn eine veränderte Datei dürfte den Namen nicht mehr tragen
    unveraendert: bool = False

    @property
    def css_stapel(self) -> str:
        """``font-family``-Wert mit Rückfall auf ähnliche Systemschriften."""
        rueckfall = {
            "serifenlos": "Arial, Helvetica, sans-serif",
            "serif": '"Times New Roman", Times, serif',
            "dicktengleich": '"Courier New", Courier, monospace',
        }[self.art]
        return f'"{self.name}", {rueckfall}'

    @property
    def fuer_office(self) -> str:
        return self.office_name or self.name

    def datei(self, schnitt: Schnitt) -> str:
        """Pfad der Schriftdatei unterhalb von ``static/``."""
        stil = "-kursiv" if schnitt.kursiv else ""
        endung = "ttf" if self.unveraendert else "woff2"
        return f"vendor/schriften/{self.schluessel}/{self.schluessel}-{schnitt.gewicht}{stil}.{endung}"


def _vier(regular: str, bold: str, italic: str, bold_italic: str) -> tuple[Schnitt, ...]:
    """Vier statische Quelldateien (normal, fett, kursiv, fett kursiv)."""
    return (
        Schnitt(400, False, regular),
        Schnitt(700, False, bold),
        Schnitt(400, True, italic),
        Schnitt(700, True, bold_italic),
    )


def _variabel(aufrecht: str, kursiv: str, **achsen: float) -> tuple[Schnitt, ...]:
    """Variable Quelldateien, je Schnitt auf feste Achsenwerte gesetzt."""
    feste = tuple(sorted(achsen.items()))
    return (
        Schnitt(400, False, aufrecht, feste),
        Schnitt(700, False, aufrecht, feste),
        Schnitt(400, True, kursiv, feste),
        Schnitt(700, True, kursiv, feste),
    )


SCHRIFTEN: tuple[Schrift, ...] = (
    Schrift(
        "arimo",
        "Arimo",
        "serifenlos",
        "wie Arial",
        "ofl/arimo",
        "OFL-1.1",
        "2026 The Arimo Project Authors (https://github.com/googlefonts/arimo)",
        _variabel("Arimo[wght].ttf", "Arimo-Italic[wght].ttf"),
        office_name="Arial",
        ersetzt=("Arial", "Helvetica", "Helvetica Neue", "Liberation Sans", "Arial MT"),
    ),
    Schrift(
        "carlito",
        "Carlito",
        "serifenlos",
        "wie Calibri",
        "ofl/carlito",
        "OFL-1.1",
        "2013 The Carlito Project Authors (https://github.com/googlefonts/carlito)",
        _vier("Carlito-Regular.ttf", "Carlito-Bold.ttf", "Carlito-Italic.ttf", "Carlito-BoldItalic.ttf"),
        office_name="Calibri",
        ersetzt=("Calibri", "Calibri Light"),
        # Reservierter Name „Carlito“ (OFL): nur die Originaldateien ausliefern, keine Untermenge
        unveraendert=True,
    ),
    Schrift(
        "open-sans",
        "Open Sans",
        "serifenlos",
        "freundlich, offen",
        "ofl/opensans",
        "OFL-1.1",
        "2020 The Open Sans Project Authors (https://github.com/googlefonts/opensans)",
        _variabel("OpenSans[wdth,wght].ttf", "OpenSans-Italic[wdth,wght].ttf", wdth=100),
    ),
    Schrift(
        "roboto",
        "Roboto",
        "serifenlos",
        "sachlich",
        "ofl/roboto",
        "OFL-1.1",
        "2011 The Roboto Project Authors (https://github.com/googlefonts/roboto-classic)",
        _variabel("Roboto[wdth,wght].ttf", "Roboto-Italic[wdth,wght].ttf", wdth=100),
    ),
    Schrift(
        "inter",
        "Inter",
        "serifenlos",
        "modern, klar",
        "ofl/inter",
        "OFL-1.1",
        "2020 The Inter Project Authors (https://github.com/rsms/inter)",
        _variabel("Inter[opsz,wght].ttf", "Inter-Italic[opsz,wght].ttf", opsz=14),
    ),
    Schrift(
        "atkinson-hyperlegible",
        "Atkinson Hyperlegible",
        "serifenlos",
        "besonders gut lesbar",
        "ofl/atkinsonhyperlegible",
        "OFL-1.1",
        "2020 Braille Institute of America, Inc.",
        _vier(
            "AtkinsonHyperlegible-Regular.ttf",
            "AtkinsonHyperlegible-Bold.ttf",
            "AtkinsonHyperlegible-Italic.ttf",
            "AtkinsonHyperlegible-BoldItalic.ttf",
        ),
    ),
    Schrift(
        "tinos",
        "Tinos",
        "serif",
        "wie Times New Roman",
        "ofl/tinos",
        "OFL-1.1",
        "2026 The Tinos Project Authors (https://github.com/googlefonts/tinos)",
        _vier("Tinos-Regular.ttf", "Tinos-Bold.ttf", "Tinos-Italic.ttf", "Tinos-BoldItalic.ttf"),
        office_name="Times New Roman",
        ersetzt=("Times New Roman", "Times", "Liberation Serif", "TimesNewRomanPSMT"),
    ),
    Schrift(
        "caladea",
        "Caladea",
        "serif",
        "wie Cambria",
        "ofl/caladea",
        "OFL-1.1",
        "2012 The Caladea Project Authors (https://github.com/huertatipografica/Caladea)",
        _vier("Caladea-Regular.ttf", "Caladea-Bold.ttf", "Caladea-Italic.ttf", "Caladea-BoldItalic.ttf"),
        office_name="Cambria",
        ersetzt=("Cambria",),
    ),
    Schrift(
        "noto-serif",
        "Noto Serif",
        "serif",
        "klassisch, ruhig",
        "ofl/notoserif",
        "OFL-1.1",
        "2022 The Noto Project Authors (https://github.com/notofonts/latin-greek-cyrillic)",
        _variabel("NotoSerif[wdth,wght].ttf", "NotoSerif-Italic[wdth,wght].ttf", wdth=100),
        ersetzt=("Georgia",),
    ),
    Schrift(
        "literata",
        "Literata",
        "serif",
        "für lange Texte",
        "ofl/literata",
        "OFL-1.1",
        "2017 The Literata Project Authors (https://github.com/googlefonts/literata)",
        _variabel("Literata[opsz,wght].ttf", "Literata-Italic[opsz,wght].ttf", opsz=12),
    ),
    Schrift(
        "cousine",
        "Cousine",
        "dicktengleich",
        "wie Courier New",
        "ofl/cousine",
        "OFL-1.1",
        "2026 The Cousine Project Authors (https://github.com/googlefonts/cousine)",
        _vier("Cousine-Regular.ttf", "Cousine-Bold.ttf", "Cousine-Italic.ttf", "Cousine-BoldItalic.ttf"),
        office_name="Courier New",
        ersetzt=("Courier New", "Courier", "Liberation Mono"),
    ),
)

_NACH_SCHLUESSEL = {schrift.schluessel: schrift for schrift in SCHRIFTEN}
_NACH_NAME: dict[str, Schrift] = {}
for _schrift in SCHRIFTEN:
    for _name in (_schrift.schluessel, _schrift.name, _schrift.office_name, *_schrift.ersetzt):
        if _name:
            _NACH_NAME.setdefault(_name.casefold(), _schrift)

_ANFUEHRUNG_RE = re.compile(r"""^\s*(["']?)(.{1,64}?)\1\s*$""")


def schrift(schluessel: str) -> Schrift | None:
    """Schrift zu ihrem Schlüssel (``arimo``) oder ``None``."""
    return _NACH_SCHLUESSEL.get(schluessel)


def finden(name: str | None) -> Schrift | None:
    """
    Schrift zu einem Namen aus Briefpapier, Editor oder eingefügtem Text: Schlüssel, Familienname, Office-Name oder
    ein ersetzter Name, ohne Rücksicht auf Groß- und Kleinschreibung. Bei einem Stapel (``"Arial", sans-serif``)
    zählt die erste Angabe. ``None``, wenn der Name keine Schrift des Satzes meint.
    """
    if not name:
        return None
    erste = name.split(",", 1)[0]
    treffer = _ANFUEHRUNG_RE.match(erste)
    if treffer is None:
        return None
    return _NACH_NAME.get(treffer.group(2).strip().casefold())


def aufloesen(name: str | None) -> Schrift:
    """Wie ``finden``, aber mit der Standardschrift, wenn der Name unbekannt ist."""
    return finden(name) or _NACH_SCHLUESSEL[STANDARD]


def office_name(wert: str | None) -> str:
    """
    Schriftname für Word und den bisherigen PDF-Weg: Eine Schrift des Satzes (Familienname oder Schlüssel) wird zu
    ihrem Office-Namen (Tinos → Times New Roman); ein früher gespeicherter Office-Name (Georgia, Calibri) bleibt, wie er
    ist. Ohne Angabe die Standardschrift.
    """
    schrift = finden(wert)
    if schrift is None:
        return (wert or "").strip() or aufloesen(None).fuer_office
    eingabe = (wert or "").split(",", 1)[0].strip().strip("\"'").casefold()
    if eingabe in (schrift.name.casefold(), schrift.schluessel):
        return schrift.fuer_office
    return (wert or "").split(",", 1)[0].strip().strip("\"'")


#: Familien, die xhtml2pdf ohne eingebettete Datei kennt, je Art der Schrift (bisheriger PDF-Weg)
_XHTML2PDF = {"serifenlos": "Helvetica", "serif": "Times New Roman", "dicktengleich": "Courier"}


def fuer_xhtml2pdf(wert: str | None) -> str:
    """Familie für den bisherigen PDF-Weg: nach der Art der Schrift (Georgia, Tinos → Times New Roman)."""
    return _XHTML2PDF[aufloesen(wert).art]


def groesse_zulaessig(punkt: float) -> bool:
    return GROESSE_MIN <= punkt <= GROESSE_MAX


def fuer_editor() -> list[dict[str, str]]:
    """Auswahlliste für den Editor (JSON): Schlüssel, Familienname, CSS-Stapel, Art und Hinweis."""
    return [
        {
            "schluessel": s.schluessel,
            "name": s.name,
            "stapel": s.css_stapel,
            "art": s.art,
            "hinweis": s.hinweis,
        }
        for s in SCHRIFTEN
    ]

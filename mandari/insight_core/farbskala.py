# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Farbskala einer Akzentfarbe (Issue #783, Stufe 1).

Tailwind liest ``primary-*`` aus CSS-Variablen im Kanalformat (``--primary-600: 23 112 63``), damit
Deckkraft-Klassen wie ``bg-primary-900/20`` weiter funktionieren. Work und Session behalten Indigo
(``:root``), das Bürgerportal setzt Grün (``[data-portal="insight"]``, ``static/css/input.css``).

Ein Bürgerportal einer Körperschaft (#317) hat eine eigene Akzentfarbe. Daraus entsteht hier serverseitig
eine ganze Skala 50–950. Die Stufen erfüllen die Kontraste, für die die Templates sie verwenden:

- 600 trägt weißen Text (Hauptknopf) und steht als Text auf Weiß: mindestens 4,5 : 1;
- 700 steht als Text auf der hellen Fläche 50: mindestens 4,5 : 1;
- 400 steht im dunklen Modus als Text auf ``gray-900``: mindestens 4,5 : 1.

Erfüllt die Akzentfarbe das nicht, wird sie für die jeweilige Stufe abgedunkelt bzw. aufgehellt; der
Farbton bleibt erhalten.
"""

from __future__ import annotations

from functools import lru_cache

STUFEN = (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)

#: Anteil Weiß (positiv) bzw. Schwarz (negativ), mit dem die Grundfarbe (Stufe 600) je Stufe gemischt wird
_MISCHUNG = {
    50: 0.94,
    100: 0.87,
    200: 0.74,
    300: 0.56,
    400: 0.36,
    500: 0.14,
    600: 0.0,
    700: -0.2,
    800: -0.36,
    900: -0.5,
    950: -0.68,
}

WEISS = (255, 255, 255)
#: Hintergrund des dunklen Modus (Tailwind gray-900)
DUNKEL = (17, 24, 39)
MINDESTKONTRAST = 4.5

RGB = tuple[int, int, int]


def hex_zu_rgb(farbe: str) -> RGB:
    """``#rrggbb`` → (r, g, b); andere Formate sind ein Programmierfehler (die Farbe ist vorher geprüft)."""
    wert = farbe.strip().lstrip("#")
    if len(wert) != 6:
        raise ValueError("Farbe im Format #rrggbb erwartet")
    return (int(wert[0:2], 16), int(wert[2:4], 16), int(wert[4:6], 16))


def _kanal_linear(wert: int) -> float:
    c = wert / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def leuchtdichte(farbe: RGB) -> float:
    """Relative Leuchtdichte nach WCAG 2.2."""
    r, g, b = (_kanal_linear(k) for k in farbe)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def kontrast(a: RGB, b: RGB) -> float:
    """Kontrastverhältnis nach WCAG 2.2 (1 bis 21)."""
    hell, dunkel = sorted((leuchtdichte(a), leuchtdichte(b)), reverse=True)
    return (hell + 0.05) / (dunkel + 0.05)


def mischen(farbe: RGB, anteil: float) -> RGB:
    """Mit Weiß (``anteil`` > 0) oder Schwarz (``anteil`` < 0) mischen."""
    ziel = WEISS if anteil > 0 else (0, 0, 0)
    t = abs(anteil)
    r, g, b = (round(k + (z - k) * t) for k, z in zip(farbe, ziel, strict=True))
    return (r, g, b)


def _bis_kontrast(farbe: RGB, gegen: RGB, richtung: float) -> RGB:
    """In kleinen Schritten zu Schwarz (``richtung`` < 0) oder Weiß mischen, bis der Kontrast reicht."""
    ergebnis = farbe
    schritt = 0.0
    while kontrast(ergebnis, gegen) < MINDESTKONTRAST and schritt < 1.0:
        schritt += 0.02
        ergebnis = mischen(farbe, richtung * schritt)
    return ergebnis


@lru_cache(maxsize=64)
def skala(farbe: str) -> tuple[tuple[int, str], ...]:
    """
    Skala 50–950 zur Akzentfarbe als ``((stufe, "r g b"), …)`` für CSS-Variablen im Kanalformat.

    Die Akzentfarbe ist die Stufe 600, falls nötig so weit abgedunkelt, dass weißer Text darauf lesbar ist.
    """
    grund = _bis_kontrast(hex_zu_rgb(farbe), WEISS, -1.0)
    stufen = {stufe: mischen(grund, anteil) for stufe, anteil in _MISCHUNG.items()}
    stufen[700] = _bis_kontrast(stufen[700], stufen[50], -1.0)
    stufen[400] = _bis_kontrast(stufen[400], DUNKEL, 1.0)
    return tuple((stufe, " ".join(str(k) for k in stufen[stufe])) for stufe in STUFEN)

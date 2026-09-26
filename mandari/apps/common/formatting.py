# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kleinsthelfer für Datum und Dateigröße, die vorher mehrfach kopiert waren.

Die Namen sind fest deutsch und bewusst nicht aus ``django.utils.dates`` übersetzt: Viele Aufrufer
laufen in Hintergrundaufgaben oder Befehlen, und die Ausgabe soll unabhängig von der aktiven Sprache
dieselbe bleiben.
"""

from datetime import date

#: Monatsnamen, Index = Monatsnummer (Index 0 bleibt leer)
MONTH_NAMES = [
    "",
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]

#: Kurze Monatsnamen, Index 0 = Januar (Diagramme, Kalender)
MONTH_ABBREVIATIONS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]

#: Wochentage als Auswahl, 0 = Montag wie ``date.weekday()``
WEEKDAY_CHOICES = [
    (0, "Montag"),
    (1, "Dienstag"),
    (2, "Mittwoch"),
    (3, "Donnerstag"),
    (4, "Freitag"),
    (5, "Samstag"),
    (6, "Sonntag"),
]


def parse_iso_date(value: str | None) -> date | None:
    """Datum im Format JJJJ-MM-TT aus einer Formulareingabe; leer oder ungültig ergibt ``None``."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def human_size(size: float, *, whole_bytes: bool = False) -> str:
    """
    Dateigröße lesbar, z. B. „1.5 MB“.

    Mit ``whole_bytes`` stehen Größen unter 1 KB ohne Nachkommastelle („512 B“ statt „512.0 B“).
    """
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size} {unit}" if whole_bytes and unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

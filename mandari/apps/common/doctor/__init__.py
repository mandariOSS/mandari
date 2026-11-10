# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``manage.py doctor`` (Issue #964): prüft Konfiguration und Integrationen, ohne etwas zu ändern.

- ``kern``: Befund, Register, Lauf mit Zeitgrenze, zweites Netz gegen Geheimnisse in der Ausgabe.
- ``umgebung``: Pflichtvariablen und Abgleich mit der ``.env``.
- ``dienste``: Datenbank, Cache, Mailversand, Aufgaben-Backend, Worker.

Weitere Prüfungen bringt jede App in ihrem Modul ``doctor`` mit (``insight_core/doctor.py``: Objektspeicher); das
Register lädt sie wie die Zeitpläne aus ``schedules.py``. So bleibt die Schichtung gewahrt: Die Plattform kennt
die Dokumentablage nicht. Eine neue Prüfung:

    from apps.common.doctor import Befund, Kontext, ok, pruefung, uebersprungen

    @pruefung("name", "Titel in der Ausgabe", rang=70)
    def pruefe_etwas(kontext: Kontext) -> Befund:
        ...

Regeln für jede Prüfung: nur lesen; was nicht eingestellt ist, ist ``uebersprungen``; Verbindungen bekommen
``kontext.zeitgrenze``; Meldungen sind feste Texte ohne den Text fremder Ausnahmen und ohne Zugangsdaten.
"""

from .kern import (
    ZEITGRENZE,
    Befund,
    Ergebnis,
    Kontext,
    Pruefung,
    Status,
    ausfuehren,
    fehler,
    gesamtstatus,
    ok,
    pruefung,
    registrierte,
    uebersprungen,
    warnung,
)

__all__ = [
    "ZEITGRENZE",
    "Befund",
    "Ergebnis",
    "Kontext",
    "Pruefung",
    "Status",
    "ausfuehren",
    "fehler",
    "gesamtstatus",
    "ok",
    "pruefung",
    "registrierte",
    "uebersprungen",
    "warnung",
]

# Die eigenen Prüfungen der Plattform; der Import trägt sie ins Register ein
from . import dienste, umgebung  # noqa: E402, F401

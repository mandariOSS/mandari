# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsvorbereitung im neuen Design (#856): Auswahl der Ansicht und feste Werte der Seite.

Die neue Seite (``work/meetings/vorbereitung/seite.html``) erscheint nur, wenn die Organisation das neue
Erscheinungsbild eingeschaltet hat (``apps.work.rahmen.neues_design``, #852). Mit ``?ansicht=bisher`` bleibt die
bisherige Seite erreichbar (dieselben Daten, kein Datenbankfeld). Positionen für Leiste, Blatt und Legende kommen
aus ``AgendaItemPosition.POSITION_CHOICES``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict

from apps.work.rahmen import neues_design

from .models import AgendaItemPosition

#: Seite im neuen Design: Tagesordnung, Unterlagen groß, Arbeit der Fraktion, Leiste unten
NEUE_VORLAGE = "work/meetings/vorbereitung/seite.html"
#: Abfrageparameter für die bisherige Ansicht bei eingeschaltetem neuen Design (dieselben Daten)
BISHERIGE_ANSICHT = "bisher"
#: Vier gleichrangige Positionen in der Leiste (in dieser Reihenfolge); alle übrigen stehen unter „Andere …“
HAUPT_POSITIONEN = ("for", "against", "abstain", "open")
#: Farbklasse des Positionspunkts (static/css/work-vorbereitung.css), immer zusammen mit dem Text
POSITIONS_KLASSEN = {"for": "zustimmung", "against": "ablehnung", "abstain": "enthaltung", "open": "offen"}


class Positionen(TypedDict):
    """Positionen der neuen Vorbereitung als (Code, Beschriftung[, Farbklasse])."""

    positionen_haupt: list[tuple[str, str, str]]
    positionen_andere: list[tuple[str, str]]
    positionen_alle: list[tuple[str, str, str]]


def neue_ansicht(organization: Any, query: Mapping[str, Any]) -> bool:
    """Neue Seite nur mit Schalter der Organisation und ohne ausdrücklichen Wunsch nach der bisherigen Ansicht."""
    return neues_design(organization) and query.get("ansicht") != BISHERIGE_ANSICHT


def positionen_fuer_leiste() -> Positionen:
    """Positionen für Leiste, Blatt und Legende der neuen Vorbereitung (Werte aus ``POSITION_CHOICES``)."""
    labels = {code: str(label) for code, label in AgendaItemPosition.POSITION_CHOICES}
    andere = [code for code in labels if code not in HAUPT_POSITIONEN]
    return {
        "positionen_haupt": [(code, labels[code], POSITIONS_KLASSEN[code]) for code in HAUPT_POSITIONEN],
        "positionen_andere": [(code, labels[code]) for code in andere],
        "positionen_alle": [
            (code, labels[code], POSITIONS_KLASSEN.get(code, "andere")) for code in (*HAUPT_POSITIONEN, *andere)
        ],
    }

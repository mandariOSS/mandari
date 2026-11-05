# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die gemeinsame OpenAPI-3.1-Beschreibung aller Bereiche der Schnittstelle.

- ``registry``: Bereiche melden sich mit ihrer Beschreibung an (``register_area``); Fachmodule tun das beim
  Start, die Drehscheibe meldet ihren Aggregator selbst an.
- ``build``: fügt den Einstieg und alle Bereiche zu einer Beschreibung zusammen (``build_description``).
- ``oparl``: Schemas der OParl-Typen und Pfade einer OParl-Ausgabe, für Aggregator und Session-Mandanten.
"""

from .aggregator import AREA as AGGREGATOR_AREA
from .build import COMMAND_EXTENSION, build_description, commands_with_endpoint, operations
from .registry import Area, Description, areas, register_area

register_area(AGGREGATOR_AREA)

__all__ = [
    "AGGREGATOR_AREA",
    "COMMAND_EXTENSION",
    "Area",
    "Description",
    "areas",
    "build_description",
    "commands_with_endpoint",
    "operations",
    "register_area",
]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bereiche der Schnittstelle: Jeder Bereich meldet sich mit seiner Beschreibung an.

Die Drehscheibe kennt die Fachmodule nicht (A1). Ein Fachmodul, das einen Bereich der Schnittstelle
anbietet, meldet ihn deshalb beim Start an (``AppConfig.ready``)::

    from hub.api.openapi import Area, register_area

    register_area(Area(key="session-v1", title="Session-API v1", …, openapi=beschreibung))

``openapi`` liefert die Beschreibung des Bereichs als OpenAPI 3.1 (``paths`` und ``components``); sie wird
erst gebaut, wenn jemand die Gesamtbeschreibung abruft. ``hub.api.openapi.build`` fügt alle Bereiche zu
einer Beschreibung zusammen, der Einstieg ``/api/`` nennt sie.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

#: Kennung eines Bereichs: Kleinbuchstaben, Ziffern, Bindestrich
_KEY = re.compile(r"[a-z][a-z0-9-]{0,39}")

#: Beschreibung eines Bereichs als OpenAPI 3.1 (``paths``, ``components``)
Description = Mapping[str, Any]


@dataclass(frozen=True)
class Area:
    """Ein Bereich der Schnittstelle."""

    #: Kennung, z. B. ``oparl`` oder ``session-v1``
    key: str
    #: Name; zugleich das Schlagwort (``tag``) seiner Endpunkte in der Beschreibung
    title: str
    description: str
    #: Einstieg als Pfad; ``{…}`` steht für einen Teil der Adresse (URI-Vorlage nach RFC 6570)
    entry: str
    #: Beschreibung des Bereichs, erst bei Bedarf gebaut
    openapi: Callable[[], Description]
    #: Reihenfolge im Einstieg und in der Beschreibung (aufsteigend, dann nach Kennung)
    order: int = 100

    def __post_init__(self) -> None:
        if not _KEY.fullmatch(self.key):
            raise ValueError(f"Ungültige Kennung eines Bereichs: {self.key!r}")
        if not self.entry.startswith("/"):
            raise ValueError(f"Der Einstieg des Bereichs {self.key} muss ein Pfad sein: {self.entry!r}")


_AREAS: Final[dict[str, Area]] = {}


def register_area(area: Area) -> None:
    """Bereich anmelden; eine erneute Anmeldung unter derselben Kennung ersetzt ihn."""
    _AREAS[area.key] = area


def areas() -> list[Area]:
    """Alle angemeldeten Bereiche in ihrer Reihenfolge."""
    return sorted(_AREAS.values(), key=lambda area: (area.order, area.key))

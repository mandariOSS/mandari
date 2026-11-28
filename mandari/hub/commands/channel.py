# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weg, auf dem ein Befehl ankommt: eigene Oberfläche, Schnittstelle oder ein anderes Modul.

Fachliche Prüfungen und Wirkung sind auf allen Wegen gleich. Nur den Zugang über Anwendungen darf ein Modul an ein
eigenes Recht knüpfen (Session: „API-Zugang“), damit der Mandant entscheidet, ob seine Leute über Anwendungen arbeiten.
Das Prüfprotokoll des Eigentümers nennt den Weg mit, bei der Schnittstelle auch die Anwendung (Client-ID des
OpenID-Connect-Anbieters), damit sich nachvollziehen lässt, welche Anwendung im Namen einer Person gehandelt hat:

    with channel(Channel(SCHNITTSTELLE, client_id=access.client_id)):
        dispatch(command)

    current_channel().audit_details()   # {"weg": "schnittstelle", "anwendung": "…"}
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

OBERFLAECHE: Final = "oberflaeche"
SCHNITTSTELLE: Final = "schnittstelle"
INTERN: Final = "intern"


@dataclass(frozen=True)
class Channel:
    """Weg eines Befehls; ``client_id`` nur bei der Schnittstelle mit Access-Token."""

    kind: str
    client_id: str | None = None

    def audit_details(self) -> dict[str, str]:
        """Angaben für das Prüfprotokoll: Weg und gegebenenfalls Anwendung, nie Inhalte."""
        details = {"weg": self.kind}
        if self.client_id:
            details["anwendung"] = self.client_id
        return details


_current: ContextVar[Channel | None] = ContextVar("hub_commands_channel", default=None)


@contextmanager
def channel(value: Channel) -> Iterator[Channel]:
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)


def current_channel() -> Channel:
    """Weg des laufenden Befehls; ohne Angabe ein anderes Modul (``intern``)."""
    return _current.get() or Channel(INTERN)

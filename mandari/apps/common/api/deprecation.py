# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abkündigung einer Schnittstelle mit den Headern ``Deprecation`` (RFC 9745) und ``Sunset`` (RFC 8594).

Fristen stehen in ``docs/RELEASE_POLITIK.md`` (Abschnitt 6). Eine abgekündigte Antwort trägt

- ``Deprecation``: Zeitpunkt der Abkündigung als Structured-Field-Date (RFC 9651), also „@“ und die
  Unix-Sekunden,
- ``Sunset``: Zeitpunkt des Wegfalls als HTTP-Datum (RFC 9110, IMF-fixdate),
- auf Wunsch ``Link: <…>; rel="successor-version"`` mit der Adresse des Nachfolgers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime

from django.http.response import HttpResponseBase


def deprecation_value(at: datetime) -> str:
    """Wert des Headers ``Deprecation`` (RFC 9745): ``@<Unix-Sekunden>``."""
    return f"@{int(at.timestamp())}"


def sunset_value(at: datetime) -> str:
    """Wert des Headers ``Sunset`` (RFC 8594): HTTP-Datum in GMT, z. B. ``Mon, 31 May 2027 00:00:00 GMT``."""
    return format_datetime(at.astimezone(UTC), usegmt=True)


@dataclass(frozen=True)
class Deprecation:
    """Abkündigung einer Schnittstelle: abgekündigt seit ``deprecated_at``, entfällt am ``sunset``."""

    deprecated_at: datetime
    sunset: datetime

    def __post_init__(self) -> None:
        if self.deprecated_at.tzinfo is None or self.sunset.tzinfo is None:
            raise ValueError("Zeitpunkte einer Abkündigung brauchen eine Zeitzone")
        if self.sunset <= self.deprecated_at:
            raise ValueError("Der Wegfall muss nach der Abkündigung liegen")

    @property
    def deprecation_header(self) -> str:
        return deprecation_value(self.deprecated_at)

    @property
    def sunset_header(self) -> str:
        return sunset_value(self.sunset)

    def apply(self, response: HttpResponseBase, *, successor: str | None = None) -> HttpResponseBase:
        """Header der Abkündigung setzen; ``successor`` ist die Adresse des Nachfolgers."""
        response["Deprecation"] = self.deprecation_header
        response["Sunset"] = self.sunset_header
        if successor:
            response["Link"] = f'<{successor}>; rel="successor-version"'
        return response

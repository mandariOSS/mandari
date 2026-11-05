# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Der Header ``Idempotency-Key`` schreibender Aufrufe (IETF-Entwurf „The Idempotency-Key HTTP Header Field“).

Befehle tragen einen Idempotenzschlüssel (``docs/adr/20260929-befehle-synchron.md``): Wiederholt ein
Aufrufer dieselbe Anfrage mit demselben Schlüssel, erhält er dieselbe Antwort, ohne dass sie erneut wirkt.

- ``parse_idempotency_key``: Wert des Headers. Der Entwurf beschreibt ihn als String nach RFC 8941 (in
  Anführungszeichen); ein Token ohne Anführungszeichen wird ebenso angenommen.
- ``valid_idempotency_key``: 1 bis 255 sichtbare ASCII-Zeichen ohne Anführungszeichen und Rückstrich.
- ``KEY_MISSING``: feste Meldung für einen fehlenden oder ungültigen Schlüssel (400,
  ``idempotenzschluessel-fehlt``).

Speichern und Wiederholen erledigt ``apps.events.idempotency``.
"""

from __future__ import annotations

from typing import Final

IDEMPOTENCY_HEADER: Final = "Idempotency-Key"

#: Höchstlänge eines Schlüssels (Spalte im Idempotenzspeicher)
MAX_KEY_LENGTH: Final = 255

#: Art des Problems bei fehlendem oder ungültigem Schlüssel
KEY_MISSING_KIND: Final = "idempotenzschluessel-fehlt"

KEY_MISSING: Final = (
    "Idempotenzschlüssel fehlt oder ist ungültig (1 bis 255 sichtbare ASCII-Zeichen, ohne Anführungszeichen)."
)


def parse_idempotency_key(raw: str | None) -> str | None:
    """Wert des Headers; ein String nach RFC 8941 kommt in Anführungszeichen, ein Token ohne. Leer: ``None``."""
    if raw is None:
        return None
    value = raw.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value or None


def valid_idempotency_key(key: object) -> bool:
    """1 bis 255 sichtbare ASCII-Zeichen ohne Anführungszeichen und Rückstrich (IETF-Entwurf: String)."""
    return (
        isinstance(key, str)
        and 0 < len(key) <= MAX_KEY_LENGTH
        and all(0x21 <= ord(char) <= 0x7E and char not in '"\\' for char in key)
    )

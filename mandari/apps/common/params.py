# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anfrageparameter robust einlesen.

Ungültige Werte aus Adresszeile, Formular oder JSON-Körper (``?page=x``, ``?term=kaputt``,
kein JSON) dürfen nie zu HTTP 500 führen. Die Helfer liefern für ungültige Eingaben einen
Rückfallwert bzw. ``None``; die View entscheidet, ob sie den Filter ignoriert, nichts findet
oder mit 400 antwortet.

Nullbytes (``%00``) in Suchbegriffen und Filtern: PostgreSQL lehnt Zeichenketten mit Nullbyte ab,
die Abfrage endet dann mit HTTP 500 (SQLite nimmt sie an, deshalb fällt es in Tests nicht auf).
:func:`text_param` und :func:`without_nul` entfernen sie, bevor ein Wert die Datenbank erreicht.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from django.http import HttpRequest, QueryDict

NUL = "\x00"


def uuid_param(value: Any) -> str | None:
    """Gültige UUID als Text, sonst ``None``."""
    if value is None or value == "":
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (TypeError, ValueError, AttributeError):
        return None


def date_param(value: Any) -> date | None:
    """ISO-Datum (``JJJJ-MM-TT``), sonst ``None``."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def int_param(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """Ganze Zahl innerhalb der Grenzen, sonst ``default``."""
    try:
        number = int(str(value).strip()) if value not in (None, "") else default
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def text_param(value: Any, *, max_length: int | None = None) -> str:
    """Freitext (Suchbegriff, Filterwert) ohne Nullbytes und ohne Leerraum an den Rändern, höchstens ``max_length``."""
    if value is None:
        return ""
    text = str(value).replace(NUL, "").strip()
    return text[:max_length] if max_length is not None else text


def without_nul(query: QueryDict) -> QueryDict:
    """Anfrageparameter ohne Nullbytes in Namen und Werten; ohne Nullbyte unverändert dasselbe Objekt."""
    if not any(NUL in key or any(NUL in value for value in values) for key, values in query.lists()):
        return query
    cleaned = QueryDict(mutable=True)
    for key, values in query.lists():
        cleaned.setlist(key.replace(NUL, ""), [value.replace(NUL, "") for value in values])
    cleaned._mutable = False
    return cleaned


def json_body(request: HttpRequest) -> dict[str, Any] | None:
    """JSON-Objekt aus dem Anfragekörper, sonst ``None`` (kein JSON, kaputt, kein Objekt)."""
    try:
        data = json.loads(request.body or b"")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None

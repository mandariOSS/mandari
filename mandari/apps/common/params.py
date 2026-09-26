# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anfrageparameter robust einlesen.

Ungültige Werte aus Adresszeile, Formular oder JSON-Körper (``?page=x``, ``?term=kaputt``,
kein JSON) dürfen nie zu HTTP 500 führen. Die Helfer liefern für ungültige Eingaben einen
Rückfallwert bzw. ``None``; die View entscheidet, ob sie den Filter ignoriert, nichts findet
oder mit 400 antwortet.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from django.http import HttpRequest


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


def json_body(request: HttpRequest) -> dict[str, Any] | None:
    """JSON-Objekt aus dem Anfragekörper, sonst ``None`` (kein JSON, kaputt, kein Objekt)."""
    try:
        data = json.loads(request.body or b"")
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None

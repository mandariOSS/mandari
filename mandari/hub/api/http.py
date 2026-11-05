# SPDX-License-Identifier: AGPL-3.0-or-later
"""
HTTP-Hülle der offenen Schnittstelle: eine für alle Endpunkte beider Ausgaben.

- ``json_response``: JSON mit offenem CORS, ``ETag`` über den Inhalt und ``Cache-Control: no-cache``
- ``conditional``: ``If-None-Match`` ergibt ``304 Not Modified`` (``apps.common.api.conditional``)
- ``error_response``: Fehler als JSON (auch 404 und 429 – Abnehmer erwarten kein HTML)
- ``problem_response``: Fehler nach RFC 9457, wo eine Absage einen Problemtyp braucht
- ``endpoint``: Dekorator jedes Endpunkts – nur lesende Methoden, Ratenbegrenzung je Adresse (429 mit
  ``Retry-After``),
  ``BadRequestError`` als 400, bedingte Anfragen

Die Schnittstelle ist anonym und rein lesend. Meldungen sind feste Texte oder nennen, was der Abnehmer
geschickt hat; nie den Text einer Ausnahme.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from functools import wraps
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase

from apps.common.api.conditional import conditional, etag_for
from apps.common.api.problems import PROBLEM_CONTENT_TYPE, Problem

#: Ein Endpunkt: Anfrage und URL-Parameter hinein, Antwort heraus
View = Callable[..., HttpResponseBase]


class BadRequestError(Exception):
    """Fehler des Abnehmers (400) mit einer Meldung, die ihm weiterhilft."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# =============================================================================
# Zeitstempel aus der Anfrage
# =============================================================================


#: Uhrzeit, Leerzeichen, Offset am Ende – das ``+`` von ``+01:00`` wird im Query-String zum Leerzeichen
_LOST_PLUS = re.compile(r"(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?) (\d{2}(?::?\d{2})?)$")


def restore_offset_plus(value: str) -> str:
    """
    Stellt das ``+`` eines positiven Offsets wieder her, das die URL-Dekodierung zum Leerzeichen gemacht hat.

    Eindeutig: Zwischen Uhrzeit und Offset steht in ISO 8601 nie ein Leerzeichen.
    """
    return _LOST_PLUS.sub(r"\1+\2", value)


def parse_client_datetime(value: str, param: str) -> datetime:
    """
    ISO-8601-Zeitstempel aus einem Query-Parameter.

    Die Zeitzone ist Pflicht: Zeitstempel ohne Zeitzone sind mehrdeutig und werden mit einer klaren
    400-Meldung abgelehnt (viele kommunale Server machen genau das falsch, wir nicht). Ein unkodiertes
    ``+`` vor dem Offset (``…T00:00:00+01:00`` von Hand in die Adresszeile getippt) wird toleriert.
    """
    value = restore_offset_plus(value)
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise BadRequestError(
            f"Parameter '{param}': '{value}' ist kein gültiger ISO-8601-Zeitstempel "
            "(erwartet z. B. 2024-01-01T00:00:00+01:00)."
        ) from None
    if dt.tzinfo is None:
        raise BadRequestError(
            f"Parameter '{param}': Zeitstempel muss eine explizite Zeitzone enthalten "
            "(z. B. 2024-01-01T00:00:00+01:00 oder 2024-01-01T00:00:00Z). "
            "Naive Zeitstempel ohne Zeitzone werden abgelehnt."
        )
    return dt


# =============================================================================
# Antworten
# =============================================================================


def json_response(data: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> HttpResponse:
    """
    JSON-Antwort mit offenem CORS (lesende, anonyme Schnittstelle).

    Erfolgreiche Antworten tragen einen ``ETag`` über ihren Inhalt und ``Cache-Control: no-cache``:
    Abnehmer dürfen die Antwort aufbewahren, fragen vor der Wiederverwendung aber mit
    ``If-None-Match`` nach (``conditional``) – so wirkt eine Rücknahme sofort.
    """
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    response = HttpResponse(payload, status=status, content_type="application/json; charset=utf-8")
    response["Access-Control-Allow-Origin"] = "*"
    if status == 200:
        response["ETag"] = etag_for(payload)
        response["Cache-Control"] = "no-cache"
        # Browser-Clients anderer Herkunft dürfen ETag und Blätter-Links lesen
        response["Access-Control-Expose-Headers"] = "ETag, Link"
    for key, value in (headers or {}).items():
        response[key] = value
    return response


def error_response(status: int, message: str) -> HttpResponse:
    """Fehler als JSON (auch 404 und 429 – Abnehmer erwarten kein HTML)."""
    return json_response({"error": message, "status": status}, status=status)


def problem_response(request: HttpRequest, problem: Problem) -> HttpResponse:
    """
    Fehler nach RFC 9457 (``apps.common.api.problems``) mit offenem CORS wie jede Antwort der Schnittstelle;
    nie zwischengespeichert. Für Absagen, die einen Problemtyp brauchen (Änderungsfeed, Datenkatalog).
    """
    response = json_response(problem.to_dict(instance=request.path), status=problem.status)
    response["Content-Type"] = PROBLEM_CONTENT_TYPE
    response["Cache-Control"] = "no-store"
    return response


# =============================================================================
# Ratenbegrenzung und Dekorator der Endpunkte
# =============================================================================


def client_ip(request: HttpRequest) -> str:
    """Adresse des Clients (erster Eintrag von ``X-Forwarded-For``, sonst ``REMOTE_ADDR``)."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return str(forwarded).split(",")[0].strip()
    return str(request.META.get("REMOTE_ADDR", "unknown"))


def rate_limit() -> int:
    """Anfragen je Minute und Adresse (``OPARL_API_RATE_LIMIT``, 0 = aus)."""
    return int(getattr(settings, "OPARL_API_RATE_LIMIT", 120))


def _rate_limited(request: HttpRequest) -> bool:
    """Zähler je Adresse und Minute im Django-Cache (festes Fenster)."""
    limit = rate_limit()
    if not limit:
        return False
    window = int(time.time() // 60)
    key = f"oparl_api:rl:{client_ip(request)}:{window}"
    try:
        count = cache.incr(key)
    except ValueError:
        # Schlüssel gibt es noch nicht – anlegen (add ist atomar genug für eine weiche Grenze)
        cache.add(key, 1, timeout=120)
        count = 1
    return int(count) > limit


def endpoint(view: View) -> View:
    """Dekorator jedes Endpunkts: nur GET/HEAD, Ratenbegrenzung, 400 für ``BadRequestError``, CORS, ETag/304."""

    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
            response["Access-Control-Allow-Origin"] = "*"
            response["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
            response["Access-Control-Allow-Headers"] = "*"
            return response
        if request.method not in ("GET", "HEAD"):
            return error_response(405, "Diese API ist rein lesend — nur GET ist erlaubt.")
        if _rate_limited(request):
            response = error_response(
                429,
                f"Rate-Limit überschritten (max. {rate_limit()} Anfragen pro Minute und IP). "
                "Bitte Anfragen drosseln — für inkrementelle Syncs modified_since verwenden.",
            )
            # Das Zählfenster ist die laufende Minute; danach lohnt der nächste Versuch
            response["Retry-After"] = str(60 - int(time.time()) % 60)
            return response
        try:
            return conditional(request, view(request, *args, **kwargs))
        except BadRequestError as exc:
            return error_response(400, exc.message)

    return wrapper

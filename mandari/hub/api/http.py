# SPDX-License-Identifier: AGPL-3.0-or-later
"""
HTTP-Hülle der offenen Schnittstelle: eine für alle Endpunkte beider Ausgaben.

- ``json_response``: JSON mit offenem CORS, ``ETag`` über den Inhalt und ``Cache-Control: no-cache``
- ``conditional``: ``If-None-Match`` ergibt ``304 Not Modified``
- ``error_response``: Fehler als JSON (auch 404 und 429 – Abnehmer erwarten kein HTML)
- ``endpoint``: Dekorator jedes Endpunkts – nur lesende Methoden, Ratenbegrenzung je Adresse (429 mit
  ``Retry-After``),
  ``BadRequestError`` als 400, bedingte Anfragen

Die Schnittstelle ist anonym und rein lesend. Meldungen sind feste Texte oder nennen, was der Abnehmer
geschickt hat; nie den Text einer Ausnahme.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from functools import wraps
from typing import Any, cast

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.utils.cache import get_conditional_response

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
        response["ETag"] = f'"{hashlib.sha256(payload).hexdigest()[:32]}"'
        response["Cache-Control"] = "no-cache"
        # Browser-Clients anderer Herkunft dürfen ETag und Blätter-Links lesen
        response["Access-Control-Expose-Headers"] = "ETag, Link"
    for key, value in (headers or {}).items():
        response[key] = value
    return response


def conditional(request: HttpRequest, response: HttpResponseBase) -> HttpResponseBase:
    """
    Bedingte Anfrage beantworten: ``304 Not Modified`` ohne Inhalt, wenn ``If-None-Match`` zum ``ETag``
    der Antwort passt – sonst die Antwort unverändert.
    """
    etag = response.get("ETag")
    if response.status_code != 200 or not etag:
        return response
    # Auch gestreamte Antworten (Datei-Abruf) laufen hier durch; Django liest nur Status und Header
    not_modified = get_conditional_response(request, etag=etag, response=cast("HttpResponse", response))
    if not_modified is None or not_modified is response:
        return response
    # Django übernimmt nur die Cache-Header; CORS und Blätter-Links gehören auch zur 304
    for header in ("Access-Control-Allow-Origin", "Access-Control-Expose-Headers", "Link"):
        if header in response:
            not_modified[header] = response[header]
    return not_modified


def error_response(status: int, message: str) -> HttpResponse:
    """Fehler als JSON (auch 404 und 429 – Abnehmer erwarten kein HTML)."""
    return json_response({"error": message, "status": status}, status=status)


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

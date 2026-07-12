# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hilfsfunktionen der OParl-API: URL-Bau, JSON-Antworten, Zeitstempel-Parsing,
Rate-Limiting.

Alle Objekt-IDs der API werden aus ``settings.OPARL_BASE_URL`` gebaut
(host-unabhängig, konfigurierbar per Umgebungsvariable ``OPARL_BASE_URL``).
"""

import json
import time
from datetime import UTC, datetime
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils import timezone

# Schema-Basis der OParl-1.1-Spezifikation
SCHEMA_BASE = "https://schema.oparl.org/1.1"

# Objekttyp (URL-Segment) -> Schema-Name
TYPE_SCHEMA = {
    "system": "System",
    "body": "Body",
    "organization": "Organization",
    "person": "Person",
    "membership": "Membership",
    "meeting": "Meeting",
    "agendaitem": "AgendaItem",
    "paper": "Paper",
    "consultation": "Consultation",
    "file": "File",
    "location": "Location",
    "legislativeterm": "LegislativeTerm",
}


class OParlBadRequestError(Exception):
    """Client-Fehler (400) mit klarer Fehlermeldung."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


# =============================================================================
# URL-Bau
# =============================================================================


def api_base():
    """Basis-URL der OParl-API (ohne abschließenden Slash)."""
    return settings.OPARL_BASE_URL.rstrip("/")


def site_url():
    """Basis-URL des Insight-Portals (für web-Links und File-Proxy)."""
    return settings.SITE_URL.rstrip("/")


def system_url():
    return f"{api_base()}/v1/system"


def body_list_url():
    return f"{api_base()}/v1/bodies"


def obj_url(kind, pk):
    """Kanonische URL eines Objekts in unserer API."""
    return f"{api_base()}/v1/{kind}/{pk}"


def sub_list_url(body_id, segment):
    """URL einer externen Objektliste einer Kommune."""
    return f"{api_base()}/v1/body/{body_id}/{segment}"


def schema_type(kind):
    """Schema-URL des Objekttyps (Wert des type-Felds)."""
    return f"{SCHEMA_BASE}/{TYPE_SCHEMA[kind]}"


# =============================================================================
# Zeitstempel
# =============================================================================


def iso(dt):
    """Datetime -> ISO 8601 mit Zeitzone (None-sicher)."""
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def iso_date(d):
    """Date -> ISO 8601 (None-sicher)."""
    return d.isoformat() if d else None


def parse_client_datetime(value, param):
    """Parst einen ISO-8601-Zeitstempel aus Query-Parametern.

    Zeitzonen-Angabe ist Pflicht: Naive Zeitstempel sind mehrdeutig und
    werden mit einer klaren 400-Fehlermeldung abgelehnt (siehe Issue #20 —
    viele kommunale Server machen genau das falsch, wir nicht).
    """
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise OParlBadRequestError(
            f"Parameter '{param}': '{value}' ist kein gültiger ISO-8601-Zeitstempel "
            "(erwartet z. B. 2024-01-01T00:00:00+01:00)."
        ) from None
    if dt.tzinfo is None:
        raise OParlBadRequestError(
            f"Parameter '{param}': Zeitstempel muss eine explizite Zeitzone enthalten "
            "(z. B. 2024-01-01T00:00:00+01:00 oder 2024-01-01T00:00:00Z). "
            "Naive Zeitstempel ohne Zeitzone werden abgelehnt."
        )
    return dt


# =============================================================================
# Antworten
# =============================================================================


def json_response(data, status=200, headers=None):
    """JSON-Antwort mit offenem CORS (lesende, anonyme API)."""
    payload = json.dumps(data, ensure_ascii=False)
    response = HttpResponse(payload, status=status, content_type="application/json; charset=utf-8")
    response["Access-Control-Allow-Origin"] = "*"
    for key, value in (headers or {}).items():
        response[key] = value
    return response


def error_response(status, message):
    """Fehler als JSON (auch 404/429 — Clients erwarten kein HTML)."""
    return json_response({"error": message, "status": status}, status=status)


# =============================================================================
# Rate-Limiting + Endpoint-Dekorator
# =============================================================================


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _rate_limited(request):
    """Fixed-Window-Zähler je IP und Minute im Django-Cache."""
    limit = getattr(settings, "OPARL_API_RATE_LIMIT", 120)
    if not limit:
        return False
    window = int(time.time() // 60)
    key = f"oparl_api:rl:{_client_ip(request)}:{window}"
    try:
        count = cache.incr(key)
    except ValueError:
        # Key existiert noch nicht — anlegen (add ist atomar genug für ein Soft-Limit)
        cache.add(key, 1, timeout=120)
        count = 1
    return count > limit


def oparl_endpoint(view):
    """Dekorator für alle OParl-Views: GET-only, Rate-Limit, 400-Handling, CORS."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method == "OPTIONS":
            response = HttpResponse(status=204)
            response["Access-Control-Allow-Origin"] = "*"
            response["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
            response["Access-Control-Allow-Headers"] = "*"
            return response
        if request.method not in ("GET", "HEAD"):
            return error_response(405, "Diese API ist rein lesend — nur GET ist erlaubt.")
        if _rate_limited(request):
            limit = getattr(settings, "OPARL_API_RATE_LIMIT", 120)
            return error_response(
                429,
                f"Rate-Limit überschritten (max. {limit} Anfragen pro Minute und IP). "
                "Bitte Anfragen drosseln — für inkrementelle Syncs modified_since verwenden.",
            )
        try:
            return view(request, *args, **kwargs)
        except OParlBadRequestError as exc:
            return error_response(400, exc.message)

    return wrapper

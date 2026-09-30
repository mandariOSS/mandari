# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hilfsfunktionen der OParl-Ausgaben (Aggregator und Session-Schnittstelle): URL-Bau, JSON-Antworten,
Listen-Hülle mit Blättern, Zeitstempel-Parsing, Rate-Limiting, bedingte Anfragen (ETag/304).

Die Bausteine des Modells selbst (Typ-URLs, Datums- und Zeitformate, Werteliste von
``organizationType``) liegen in ``hub.ris.canonical`` und werden hier nur weitergereicht.

Alle Objekt-IDs der API werden aus ``settings.OPARL_BASE_URL`` gebaut
(host-unabhängig, konfigurierbar per Umgebungsvariable ``OPARL_BASE_URL``).
"""

import hashlib
import json
import time
from datetime import datetime
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.cache import get_conditional_response

from hub.ris.canonical import (  # noqa: F401 – Bausteine des Modells, für die Ausgaben weitergereicht
    ORGANIZATION_TYPES,
    SCHEMA_BASE,
    TYPE_SCHEMA,
    iso,
    iso_date,
    iso_day,
    schema_type,
)
from hub.ris.mapping.session import ORGANIZATION_TYPES as SESSION_ORGANIZATION_TYPES

# Verbreitete Angaben fremder Quellen, die keiner der sieben Werte sind, aber eindeutig dazugehören.
# Manche RIS ordnen nach dem Kommunalrecht: Hauptorgan (Rat, Kreistag) und Hilfsorgan (Ausschüsse,
# Beiräte) sind Gremien; Amt, Dienststelle und Organisationseinheit gehören zur Verwaltung.
_ORGANIZATION_TYPE_SYNONYMS = {
    "ausschuss": "Gremium",
    "ausschüsse": "Gremium",
    "rat": "Gremium",
    "beirat": "Gremium",
    "beiräte": "Gremium",
    "kommission": "Gremium",
    "kommissionen": "Gremium",
    "gremien": "Gremium",
    "hauptorgan": "Gremium",
    "hauptorgane": "Gremium",
    "hilfsorgan": "Gremium",
    "hilfsorgane": "Gremium",
    "fraktionen": "Fraktion",
    "parteien": "Partei",
    "institutionen": "Institution",
    "amt": "Verwaltungsbereich",
    "ämter": "Verwaltungsbereich",
    "fachbereich": "Verwaltungsbereich",
    "fachbereiche": "Verwaltungsbereich",
    "dezernat": "Verwaltungsbereich",
    "dezernate": "Verwaltungsbereich",
    "dienststelle": "Verwaltungsbereich",
    "dienststellen": "Verwaltungsbereich",
    "organisationseinheit": "Verwaltungsbereich",
    "organisationseinheiten": "Verwaltungsbereich",
    "verwaltung": "Verwaltungsbereich",
}

_ORGANIZATION_TYPE_LOOKUP = {
    **_ORGANIZATION_TYPE_SYNONYMS,
    **SESSION_ORGANIZATION_TYPES,
    **{value.casefold(): value for value in ORGANIZATION_TYPES},
}


def organization_type(value: object) -> str | None:
    """
    ``organizationType`` als Wert der Spezifikation (oder ``None`` ohne Angabe).

    Werte der Spezifikation bleiben (Schreibweise vereinheitlicht), Schlüssel des Session-RIS und
    verbreitete Angaben fremder Quellen werden zugeordnet, alles andere gilt als „Sonstiges“.
    """
    text = str(value or "").strip()
    if not text:
        return None
    return _ORGANIZATION_TYPE_LOOKUP.get(text.casefold(), "Sonstiges")


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


# =============================================================================
# Zeitstempel
# =============================================================================


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
    """
    JSON-Antwort mit offenem CORS (lesende, anonyme API).

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


def conditional(request, response):
    """
    Bedingte Anfrage beantworten: ``304 Not Modified`` ohne Inhalt, wenn ``If-None-Match`` zum ``ETag``
    der Antwort passt – sonst die Antwort unverändert.
    """
    etag = response.get("ETag")
    if response.status_code != 200 or not etag:
        return response
    not_modified = get_conditional_response(request, etag=etag, response=response)
    if not_modified is response:
        return response
    # Django übernimmt nur die Cache-Header; CORS und Blätter-Links gehören auch zur 304
    for header in ("Access-Control-Allow-Origin", "Access-Control-Expose-Headers", "Link"):
        if header in response:
            not_modified[header] = response[header]
    return not_modified


def error_response(status, message):
    """Fehler als JSON (auch 404/429 — Clients erwarten kein HTML)."""
    return json_response({"error": message, "status": status}, status=status)


# =============================================================================
# Externe Listen: Seitennummer und Hülle (data/pagination/links)
# =============================================================================


def page_number(request):
    """Seitennummer aus ``?page=`` (ab 1); alles andere ergibt eine 400 mit klarer Meldung."""
    raw = request.GET.get("page", "1")
    try:
        number = int(raw)
    except ValueError:
        raise OParlBadRequestError(f"Parameter 'page': '{raw}' ist keine gültige Seitennummer.") from None
    if number < 1:
        raise OParlBadRequestError("Parameter 'page': Seitennummern beginnen bei 1.")
    return number


def page_size():
    """Objekte je Listen-Seite (``OPARL_API_PAGE_SIZE``)."""
    return getattr(settings, "OPARL_API_PAGE_SIZE", 100)


def list_envelope(base_url, filters, paginator, page, data):
    """
    Hülle einer externen Objektliste samt ``Link``-Header: ``(envelope, headers)``.

    ``filters`` sind die Zeitfilter der Anfrage, wie der Client sie geschickt hat; sie bleiben in den
    Blätter-Links erhalten. Seite 1 hat keinen ``page``-Parameter (eine kanonische Schreibweise je URL).
    """

    def page_link(number):
        params = dict(filters)
        if number > 1:
            params["page"] = number
        return f"{base_url}?{urlencode(params)}" if params else base_url

    links = {"first": page_link(1), "self": page_link(page.number)}
    if page.has_previous():
        links["prev"] = page_link(page.number - 1)
    if page.has_next():
        links["next"] = page_link(page.number + 1)
    links["last"] = page_link(paginator.num_pages)

    envelope = {
        "data": data,
        "pagination": {
            "totalElements": paginator.count,
            "elementsPerPage": paginator.per_page,
            "currentPage": page.number,
            "totalPages": paginator.num_pages,
        },
        "links": links,
    }
    headers = {"Link": ", ".join(f'<{url}>; rel="{rel}"' for rel, url in links.items() if rel != "self")}
    return envelope, headers


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
    """Dekorator für alle OParl-Views: GET-only, Rate-Limit, 400-Handling, CORS, ETag/304."""

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
            return conditional(request, view(request, *args, **kwargs))
        except OParlBadRequestError as exc:
            return error_response(400, exc.message)

    return wrapper

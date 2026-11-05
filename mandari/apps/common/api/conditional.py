# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bedingte Abrufe: ``ETag`` über den Inhalt einer Antwort, ``If-None-Match`` ergibt ``304 Not Modified``.

Abnehmer dürfen eine Antwort aufbewahren und fragen vor der Wiederverwendung mit ``If-None-Match`` nach.
Der ``ETag`` hängt nur vom Inhalt ab (SHA-256, gekürzt): Gleicher Inhalt ergibt denselben Wert, auf jedem
Server einer Installation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Final, cast

from django.http import HttpRequest, HttpResponse
from django.http.response import HttpResponseBase
from django.utils.cache import get_conditional_response

#: Header einer Antwort, die auch eine ``304`` tragen muss (CORS, Blätter-Links und Nachfolger, Abkündigung).
CARRIED_HEADERS: Final = (
    "Access-Control-Allow-Origin",
    "Access-Control-Expose-Headers",
    "Link",
    "Deprecation",
    "Sunset",
)


def etag_for(content: bytes) -> str:
    """Starker ``ETag`` über den Inhalt (in Anführungszeichen, 32 Hexadezimalzeichen)."""
    return f'"{hashlib.sha256(content).hexdigest()[:32]}"'


def conditional(
    request: HttpRequest, response: HttpResponseBase, *, carried: Iterable[str] = CARRIED_HEADERS
) -> HttpResponseBase:
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
    for header in carried:
        if header in response:
            not_modified[header] = response[header]
    return not_modified

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weiterleitungen der offenen Schnittstelle: abweichende Schreibweisen führen zur gültigen Adresse.

Jedes Objekt hat genau eine Adresse – sie ist zugleich seine Kennung (``id``). Die beiden Ausgaben
schreiben ihre Adressen unterschiedlich: der Aggregator ohne Schrägstrich am Ende
(``/oparl/v1/system``), die Session-Schnittstelle mit (``/session/<slug>/api/oparl/body/``). Wer die
jeweils andere Schreibweise abruft, wird dauerhaft weitergeleitet, statt eine Fehlerseite zu bekommen:

- Aggregator: Adresse mit Schrägstrich am Ende → ohne (``without_trailing_slash``, Routen in
  ``hub.api.urls``),
- Session-Schnittstelle: Adresse ohne Schrägstrich → mit (Djangos ``APPEND_SLASH``).

Die Weiterleitung nennt einen Pfad, keinen Host: Der Abnehmer bleibt auf dem Host, über den er die
Schnittstelle erreicht hat. Parameter der Anfrage bleiben erhalten.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect
from django.utils.encoding import escape_uri_path, iri_to_uri
from django.utils.http import escape_leading_slashes

from hub.api.http import endpoint


@endpoint
def without_trailing_slash(request: HttpRequest, **kwargs: Any) -> HttpResponse:
    """Adresse mit Schrägstrich am Ende dauerhaft auf die Adresse ohne weiterleiten (301)."""
    target = escape_leading_slashes(escape_uri_path(request.path.rstrip("/")))
    query = request.META.get("QUERY_STRING", "")
    return HttpResponsePermanentRedirect(f"{target}?{iri_to_uri(query)}" if query else target)

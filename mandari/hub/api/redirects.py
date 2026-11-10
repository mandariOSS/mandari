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

Dazu die Kurzadresse einer Kommune (Issue #1309): ``/oparl/v1/body/muenster/papers`` nennt den Namen der Kommune
(ihr ``slug``) statt ihrer Kennung und führt vorläufig (307) auf die gültige Adresse. Die Kennung der Kommune bleibt
ihre Adresse mit der UUID: Ein Name kann sich ändern, eine Kennung nicht. Deshalb vorläufig und nicht dauerhaft.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponsePermanentRedirect, HttpResponseRedirect
from django.utils.encoding import escape_uri_path, iri_to_uri
from django.utils.http import escape_leading_slashes

from hub.api.http import endpoint, error_response


@endpoint
def without_trailing_slash(request: HttpRequest, **kwargs: Any) -> HttpResponse:
    """Adresse mit Schrägstrich am Ende dauerhaft auf die Adresse ohne weiterleiten (301)."""
    target = escape_leading_slashes(escape_uri_path(request.path.rstrip("/")))
    query = request.META.get("QUERY_STRING", "")
    return HttpResponsePermanentRedirect(f"{target}?{iri_to_uri(query)}" if query else target)


@endpoint
def body_by_slug(request: HttpRequest, slug: str, rest: str = "") -> HttpResponse:
    """
    Kurzadresse einer Kommune vorläufig auf ihre gültige Adresse weiterleiten (307): ``body/<slug>`` auf
    ``body/<uuid>``, mit allem, was danach folgt (``/papers``, ``/kommparl/changes``, Parameter der Anfrage).

    Auch nicht gelistete und gelöschte Kommunen haben eine gültige Adresse; was dort steht, entscheidet der
    Endpunkt der Kennung. Unbekannte Namen: 404 als JSON mit dem Hinweis, wo die Namen stehen.
    """
    from insight_core.models import OParlBody

    pk = OParlBody.objects.filter(slug=slug).values_list("pk", flat=True).first()
    if pk is None:
        return error_response(
            404,
            f"Keine Kommune mit dem Namen '{slug}'. Die Namen stehen im Feld 'slug' der Einträge von 'bodyIndex' "
            "in der Übersicht der Schnittstelle.",
        )
    marke = f"/body/{slug}"
    anfang = request.path[: request.path.index(marke)]
    target = escape_leading_slashes(escape_uri_path(f"{anfang}/body/{pk}" + (f"/{rest.rstrip('/')}" if rest else "")))
    query = request.META.get("QUERY_STRING", "")
    response = HttpResponseRedirect(f"{target}?{iri_to_uri(query)}" if query else target, preserve_request=True)
    # Abrufe aus dem Browser folgen der Weiterleitung nur mit offenem CORS, wie bei jeder Antwort der Schnittstelle
    response["Access-Control-Allow-Origin"] = "*"
    return response

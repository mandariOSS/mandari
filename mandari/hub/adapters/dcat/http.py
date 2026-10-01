# SPDX-License-Identifier: AGPL-3.0-or-later
"""
HTTP-Hülle des Katalogs: Adressen mit Endung, Inhaltsaushandlung, Zwischenspeicher, Fehler.

Jeder Katalog steht unter einer festen Adresse in drei Formen:

- ``…/catalog.ttl`` Turtle (``text/turtle``)
- ``…/catalog.rdf`` RDF/XML (``application/rdf+xml``)
- ``…/catalog.jsonld`` JSON-LD (``application/ld+json``)

``…/catalog`` ohne Endung wählt die Form nach dem ``Accept``-Kopf (Vorgabe Turtle) und nennt die gewählte
Adresse in ``Content-Location``. Jede Antwort verweist im ``Link``-Kopf auf alle drei Formen.

Die Antworten tragen ``ETag`` über ihren Inhalt und ``Cache-Control: no-cache``; ``If-None-Match`` ergibt
``304`` (``hub.api.http.endpoint``, zusammen mit Ratenbegrenzung, CORS und nur lesenden Methoden). Ein
fertiger Katalog liegt ``DCAT_CACHE_SECONDS`` lang im Cache; der Schlüssel nennt alles, wovon er abhängt.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse

from hub.adapters.dcat.katalog import Katalog
from hub.api.http import error_response, json_response
from hub.commands.problems import Problem

#: Version des Cache-Schlüssels; steigt, wenn sich die Ausgabe für dieselben Daten ändert
CACHE_VERSION: Final = 1


@dataclass(frozen=True)
class Format:
    """Eine Form des Katalogs: Endung der Adresse und Medientyp."""

    endung: str
    medientyp: str


TURTLE: Final = Format("ttl", "text/turtle")
RDF_XML: Final = Format("rdf", "application/rdf+xml")
JSON_LD: Final = Format("jsonld", "application/ld+json")
#: Formen nach Endung; die erste ist die Vorgabe der Inhaltsaushandlung
FORMATE: Final[dict[str, Format]] = {f.endung: f for f in (TURTLE, RDF_XML, JSON_LD)}

#: Medientypen im ``Accept``-Kopf -> Form (die ersten drei sind die eigenen, die übrigen gängige Varianten)
_AUSHANDLUNG: Final[dict[str, Format]] = {
    TURTLE.medientyp: TURTLE,
    RDF_XML.medientyp: RDF_XML,
    JSON_LD.medientyp: JSON_LD,
    "application/x-turtle": TURTLE,
    "application/json": JSON_LD,
}


def enabled() -> bool:
    """Der Katalog ist je Installation einzuschalten (``DCAT_ENABLED``)."""
    return bool(getattr(settings, "DCAT_ENABLED", False))


def cache_seconds() -> int:
    return int(getattr(settings, "DCAT_CACHE_SECONDS", 300))


def format_waehlen(request: HttpRequest, endung: str | None) -> tuple[Format | None, bool]:
    """
    Form des Katalogs und ob sie ausgehandelt wurde. Eine Endung gilt unverändert (unbekannt: ``None``);
    ohne Endung entscheidet der ``Accept``-Kopf, ohne passende Angabe gilt Turtle.
    """
    if endung is not None:
        return FORMATE.get(endung), False
    bevorzugt = request.get_preferred_type(list(_AUSHANDLUNG))
    return (_AUSHANDLUNG.get(bevorzugt) if bevorzugt else None) or TURTLE, True


def unbekanntes_format(endung: str) -> HttpResponse:
    return error_response(404, f"Unbekannte Form '{endung}'. Verfügbar: {', '.join(FORMATE)}.")


def problem_antwort(request: HttpRequest, status: int, kind: str, detail: str) -> HttpResponse:
    """Absage als Problem nach RFC 9457 (feste Texte, nie der Text einer Ausnahme)."""
    response = json_response(Problem(status=status, kind=kind, detail=detail).to_dict(instance=request.path), status)
    response["Content-Type"] = "application/problem+json; charset=utf-8"
    response["Cache-Control"] = "no-store"
    return response


def ausgeschaltet() -> HttpResponse:
    """Ausgeschaltet gibt es die Adressen nicht (wie beim Änderungsfeed)."""
    return error_response(404, "Der Datenkatalog ist in dieser Installation nicht eingeschaltet.")


def katalog_antwort(
    request: HttpRequest,
    *,
    adresse: str,
    endung: str | None,
    schluessel: str,
    bauen: Callable[[], Katalog],
) -> HttpResponse:
    """
    Katalog unter ``adresse`` (ohne Endung) ausliefern.

    ``schluessel`` muss alles nennen, wovon der Katalog abhängt (Kommune, Veröffentlichungsstand …); ``bauen``
    läuft nur ohne Treffer im Cache.
    """
    ausgabe, verhandelt = format_waehlen(request, endung)
    if ausgabe is None:
        return unbekanntes_format(endung or "")
    cache_key = f"dcat:v{CACHE_VERSION}:{schluessel}:{ausgabe.endung}"
    nutzlast = cache.get(cache_key) if cache_seconds() else None
    if not isinstance(nutzlast, bytes):
        # rdflib erst hier laden (rund 15 MB), nicht beim Start jedes Prozesses
        from hub.adapters.dcat import rdf

        nutzlast = rdf.serialisieren(bauen(), ausgabe.endung)
        if cache_seconds():
            cache.set(cache_key, nutzlast, cache_seconds())

    response = HttpResponse(nutzlast, content_type=f"{ausgabe.medientyp}; charset=utf-8")
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Expose-Headers"] = "ETag, Link, Content-Location"
    response["ETag"] = f'"{hashlib.sha256(nutzlast).hexdigest()[:32]}"'
    response["Cache-Control"] = "no-cache"
    response["Link"] = ", ".join(
        f'<{adresse}.{form.endung}>; rel="alternate"; type="{form.medientyp}"' for form in FORMATE.values()
    )
    if verhandelt:
        response["Vary"] = "Accept"
        response["Content-Location"] = f"{adresse}.{ausgabe.endung}"
    return response

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstieg der Schnittstelle: ``/api/``, die Beschreibung ``/api/openapi.json`` und ihre Dokumentation ``/api/docs/``.

**Einstieg ``/api/``:** ein JSON-Dokument, das die Schnittstelle der Installation vorstellt. Es ist bewusst
*kein* zweites OParl-``System``-Objekt: In OParl ist die Adresse eines Objekts seine Kennung (A7). Ein
System unter ``/api/`` hätte entweder eine fremde Kennung (zwei Adressen, zwei Fassungen desselben Objekts)
oder eine zweite – beides verwirrt Anwendungen, die Objekte an ihrer ``id`` erkennen. Der Einstieg verweist
deshalb auf das System der Installation (``system``, der Aggregator) und nennt daneben

- ``kommparl:conformsTo``: die Kennungen aus kommParl, deren Anforderungen die Installation vollständig erfüllt
  (kommParl, Kapitel 5; ``hub.api.kommparl.conforms_to``, dieselbe Liste wie am ``System`` beider Ausgaben). Das ist
  die Fassung des Entwurfs, auf der die OParl-Ausgaben beruhen. Die Profile Änderungsfeed und Snapshot kämen hinzu,
  wenn die Installation sie anbietet und sie vollständig erfüllt sind (``PROFILES_FULFILLED``).
- ``openapi`` und ``documentation``: die Beschreibung aller Bereiche und ihre Dokumentationsseite,
- ``areas``: die Bereiche mit ihrem Einstieg (``{…}`` steht für einen Teil der Adresse, RFC 6570).

Alle Adressen gehen von ``SITE_URL`` bzw. ``OPARL_BASE_URL`` aus, nicht vom Host der Anfrage. Wie jede
Antwort der offenen Schnittstelle: anonym, lesend, CORS offen, ``ETag`` mit ``304``, Ratenbegrenzung
(``hub.api.http.endpoint``).

**Beschreibung ``/api/openapi.json``:** ``hub.api.openapi.build_description`` mit dem Einstieg und allen
angemeldeten Bereichen. **Dokumentation ``/api/docs/``:** Swagger UI aus den lokalen Dateien von django-ninja
(kein CDN; die Skripte kommen von der eigenen Adresse und passen zur CSP).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Final

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_safe

from hub.api import changes, kommparl
from hub.api.http import endpoint, json_response
from hub.api.openapi import Area, Description, areas, build_description
from hub.api.openapi.aggregator import system_url
from hub.api.openapi.build import TITLE

#: Fassung von kommParl, nach der sich die OParl-Ausgaben richten (Entwurf 0.1, kommParl Kapitel 5)
KOMMPARL_VERSION: Final = kommparl.VERSION

ENTRY_SCHEMA: Final = "Einstieg"

NAME: Final = "mandari"

DESCRIPTION: Final = (
    "Schnittstelle dieser mandari-Installation. Ratsdaten als OParl 1.1 (Einstieg über system), alle Bereiche "
    "in einer OpenAPI-Beschreibung (openapi)."
)


def conforms_to() -> list[str]:
    """Kennungen aus kommParl, deren Anforderungen die Installation vollständig erfüllt."""
    return kommparl.conforms_to(feed_offered=changes.enabled())


def _site(path: str) -> str:
    return f"{str(settings.SITE_URL).rstrip('/')}{path}"


def _entry(area: Area) -> str:
    """Einstieg eines Bereichs: eigene Adresse (der Aggregator: sein System) oder ``SITE_URL`` mit Pfad."""
    return area.entry_url() if area.entry_url else _site(area.entry)


def entry_document() -> dict[str, Any]:
    """Inhalt des Einstiegs ``/api/``."""
    return {
        "name": NAME,
        "description": DESCRIPTION,
        "oparlVersion": kommparl.OPARL_VERSION,
        "system": system_url(),
        "kommparl:conformsTo": conforms_to(),
        "openapi": _site(reverse("schnittstelle:openapi")),
        "documentation": _site(reverse("schnittstelle:docs")),
        "areas": [
            {"key": area.key, "title": area.title, "description": area.description, "entry": _entry(area)}
            for area in areas()
        ],
    }


@endpoint
def entry_view(request: HttpRequest) -> HttpResponse:
    return json_response(entry_document())


@endpoint
def openapi_view(request: HttpRequest) -> HttpResponse:
    return json_response(description())


@require_safe
def docs_view(request: HttpRequest) -> HttpResponse:
    """Swagger UI mit den lokalen Dateien von django-ninja (Vorlage ``ninja/swagger.html``)."""
    swagger = {"url": reverse("schnittstelle:openapi"), "layout": "BaseLayout", "deepLinking": True}
    context = {
        "swagger_settings": json.dumps(swagger, indent=1),
        "api": SimpleNamespace(title=TITLE),
        "add_csrf": False,
    }
    return render(request, "ninja/swagger.html", context)


# =============================================================================
# Beschreibung
# =============================================================================


def entry_description() -> Description:
    """Beschreibung des Einstiegs selbst (``/api/``, ``/api/openapi.json``, ``/api/docs/``)."""
    url = {"type": "string", "format": "uri"}
    etag = {"ETag": {"description": "Kennung des Inhalts für bedingte Abrufe", "schema": {"type": "string"}}}
    schema = {
        "title": "Einstieg",
        "description": (
            "Einstieg der Schnittstelle. Kein OParl-Objekt: system verweist auf das OParl-System der Installation."
        ),
        "type": "object",
        "required": ["name", "oparlVersion", "system", "kommparl:conformsTo", "openapi", "documentation", "areas"],
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "oparlVersion": url,
            "system": {**url, "description": "OParl-System der Installation (Aggregator)"},
            "kommparl:conformsTo": {
                "type": "array",
                "items": url,
                "description": "Kennungen aus kommParl, deren Anforderungen die Installation vollständig erfüllt",
            },
            "openapi": url,
            "documentation": url,
            "areas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["key", "title", "entry"],
                    "properties": {
                        "key": {"type": "string"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "entry": {"type": "string", "description": "Einstieg; {…} ist ein Teil der Adresse (RFC 6570)"},
                    },
                },
            },
        },
    }
    paths = {
        reverse("schnittstelle:entry"): {
            "get": {
                "operationId": "api_entry",
                "summary": "Einstieg",
                "responses": {
                    "200": {
                        "description": "Einstieg der Schnittstelle",
                        "headers": etag,
                        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ENTRY_SCHEMA}"}}},
                    },
                    "304": {"description": "Unverändert"},
                },
            }
        },
        reverse("schnittstelle:openapi"): {
            "get": {
                "operationId": "api_openapi",
                "summary": "Diese Beschreibung (OpenAPI 3.1)",
                "responses": {
                    "200": {
                        "description": "OpenAPI-Beschreibung aller Bereiche",
                        "headers": etag,
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                    "304": {"description": "Unverändert"},
                },
            }
        },
        reverse("schnittstelle:docs"): {
            "get": {
                "operationId": "api_docs",
                "summary": "Dokumentation (Swagger UI)",
                "responses": {"200": {"description": "Dokumentationsseite", "content": {"text/html": {}}}},
            }
        },
    }
    return {"paths": paths, "components": {"schemas": {ENTRY_SCHEMA: schema}}}


ENTRY_AREA: Final = Area(
    key="einstieg",
    title="Einstieg",
    description="Einstieg, Beschreibung und Dokumentation der Schnittstelle.",
    entry="/api/",
    openapi=entry_description,
    order=0,
)


def description() -> dict[str, Any]:
    """Die Gesamtbeschreibung: Einstieg und alle angemeldeten Bereiche."""
    return build_description(extra=[(ENTRY_AREA, entry_description())])

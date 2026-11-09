# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bereich „OParl-Aggregator“: der RIS-Bestand aller Kommunen unter ``/oparl/`` (``hub.api.aggregator``).

Die Pfade stammen aus den Routen des Aggregators (``hub.api.urls.ROUTES``) und seinen Listen und
Objekttypen; ändert sich dort etwas, ändert sich die Beschreibung mit.
"""

from __future__ import annotations

from typing import Any, Final

from django.conf import settings
from django.urls import reverse

from .oparl import KINDS, REF, component_schemas, oparl_paths
from .registry import Area, Description

KEY: Final = "oparl"

OVERVIEW: Final = "OParlOverview"

BODY_INDEX_ENTRY: Final = "OParlBodyIndexEntry"


def system_url() -> str:
    """Kennung des OParl-Systems der Installation; geht wie alle Adressen des Aggregators von ``OPARL_BASE_URL`` aus."""
    from hub.ris.mapping.bestand import BestandUris

    return BestandUris(settings.OPARL_BASE_URL, settings.SITE_URL).system()


def _route(name: str) -> str:
    """Route des Aggregators in der Schreibweise von OpenAPI (``<uuid:pk>`` -> ``{id}``)."""
    from hub.api.urls import ROUTES

    route = next(route for route, _view, _kwargs, route_name in ROUTES if route_name == name)
    return route.replace("<uuid:pk>", "{id}").replace("<str:kind>", "{kind}").replace("<str:segment>", "{segment}")


def description() -> Description:
    from hub.api import aggregator

    prefix = reverse("oparl_api:root")
    body_list = _route("body_list")
    paths = oparl_paths(
        prefix=prefix,
        key=KEY,
        system=_route("system"),
        bodies=_route("bodies"),
        body=_route("body"),
        lists={body_list.replace("{segment}", segment): spec.kind for segment, spec in aggregator.BODY_LISTS.items()},
        objects=[kind for kind in aggregator.OBJECT_TYPES if kind != "body" and kind in KINDS],
        object_path=_route("object"),
        feed=_route("body_changes"),
        snapshot=_route("body_snapshot"),
        kommparl_feed=_route("body_kommparl_changes"),
        kommparl_snapshot=_route("body_kommparl_snapshot"),
    )
    overview: dict[str, Any] = {
        "description": "Übersicht über die Schnittstelle (kein OParl-Objekt)",
        "content": {"application/json": {"schema": {"$ref": f"{REF}{OVERVIEW}"}}},
    }
    for path, operation_id in ((prefix, f"{KEY}_overview"), (reverse("oparl_api:root_v1"), f"{KEY}_overview_v1")):
        paths[path] = {
            "get": {"operationId": operation_id, "summary": "Übersicht", "responses": {"200": overview}},
        }
    schemas = component_schemas()
    schemas[OVERVIEW] = {
        "title": "Übersicht",
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "oparlVersion": {"type": "string", "format": "uri"},
            "system": {"type": "string", "format": "uri"},
            "bodies": {"type": "string", "format": "uri"},
            "documentation": {"type": "string", "format": "uri"},
            "bodyDirectory": {
                "type": "string",
                "format": "uri",
                "description": "Seite im Bürgerportal mit allen Kommunen und ihren Adressen",
            },
            "specification": {"type": "string", "format": "uri"},
            "objectEndpoints": {"type": "object", "additionalProperties": {"type": "string"}},
            "bodyIndex": {
                "type": "array",
                "description": (
                    "Gelistete Kommunen mit den Adressen ihrer Listen (Felder wie im Body), alphabetisch nach "
                    "displayName"
                ),
                "items": {"$ref": f"{REF}{BODY_INDEX_ENTRY}"},
            },
        },
    }
    uri: dict[str, Any] = {"type": "string", "format": "uri"}
    schemas[BODY_INDEX_ENTRY] = {
        "title": "Kommune im Verzeichnis",
        "type": "object",
        "required": ["name", "body", "paper", "meeting", "organization", "person"],
        "properties": {
            "name": {"type": "string", "description": "Name wie im Body"},
            "displayName": {"type": "string", "description": "Anzeigename im Bürgerportal"},
            "slug": {"type": "string", "description": "Kurzname in den Adressen des Bürgerportals"},
            "ags": {"type": "string", "description": "Amtlicher Gemeindeschlüssel, soweit bekannt"},
            "body": uri,
            "paper": uri,
            "meeting": uri,
            "organization": uri,
            "person": uri,
            "web": uri,
        },
    }
    return {"paths": paths, "components": {"schemas": schemas}}


AREA: Final = Area(
    key=KEY,
    title="OParl-Aggregator",
    description=(
        "Der RIS-Bestand aller angebundenen Kommunen als eine OParl-1.1-Ausgabe: anonym, lesend, mit "
        "Änderungsfeed und Snapshot je Kommune, wenn die Installation sie anbietet."
    ),
    entry="/oparl/v1/system",
    openapi=description,
    order=10,
    # Der Einstieg ist das System selbst, auch wenn OPARL_BASE_URL nicht unter SITE_URL liegt
    entry_url=system_url,
)

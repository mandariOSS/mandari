# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die OParl-Ausgaben in der OpenAPI-Beschreibung: Schemas der Objekttypen und die Pfade beider Ausgaben.

Die Endpunkte selbst bleiben, wie sie sind (``hub.api.aggregator``, ``apps/session/api/oparl.py``); hier
steht nur ihre Beschreibung.

- **Schemas** entstehen aus den Typen von OParl 1.1 in ``hub.api.oparl_types``, derselben Quelle wie die
  Typprüfung der Ausgaben. Ein Objekt hat die Eigenschaften der Spezifikation, eigene Felder nur mit
  Namensraum (``mandari:…``). Gelöschtes ist ein gekürztes Objekt (``OParlDeleted``, OParl 1.1 §2.8).
- **Pfade** beschreibt ``oparl_paths`` für eine Ausgabe: Aggregator (``aggregator_description``) und je
  Session-Mandant (das Fachmodul meldet seinen Bereich mit ``tenant_description`` an). Externe Listen haben
  Seiten (``apps.common.api.pages``) und Zeitfilter, jede Antwort einen ``ETag``; Fehler kommen als JSON
  ``{"error", "status"}``, Absagen von Änderungsfeed und Snapshot als Problem nach RFC 9457.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from apps.common.api.pages import LINKS_SCHEMA, PAGINATION_SCHEMA, page_schema
from hub.api.oparl_types import ALLGEMEIN, FELDER, ORGANIZATION_TYPES, PFLICHT, SCHEMA

#: Objekttyp in Adressen (Kleinbuchstaben) -> Typ in OParl
KINDS: Final[dict[str, str]] = {
    "system": "System",
    "body": "Body",
    "legislativeterm": "LegislativeTerm",
    "organization": "Organization",
    "person": "Person",
    "membership": "Membership",
    "meeting": "Meeting",
    "agendaitem": "AgendaItem",
    "paper": "Paper",
    "consultation": "Consultation",
    "file": "File",
    "location": "Location",
}

#: Eigene Felder eines Herstellers: Namensraum, Doppelpunkt, Name
NAMESPACED: Final = r"^[A-Za-z][A-Za-z0-9_.-]*:.+$"

REF: Final = "#/components/schemas/"
DELETED: Final = "OParlDeleted"
ERROR: Final = "OParlError"
PAGINATION: Final = "OParlPagination"
LINKS: Final = "OParlLinks"
CHANGES: Final = "OParlChangeList"
PROBLEM: Final = "Problem"

_URL: Final[dict[str, Any]] = {"type": "string", "format": "uri"}
_TEXT: Final[dict[str, Any]] = {"type": "string", "minLength": 1}

#: Art einer Eigenschaft (``hub.api.oparl_types``) -> Schema
_ARTEN: Final[dict[str, dict[str, Any]]] = {
    "url": _URL,
    "text": _TEXT,
    "datum": {"type": "string", "format": "date"},
    "zeitpunkt": {"type": "string", "format": "date-time"},
    "bool": {"type": "boolean"},
    "zahl": {"type": "integer"},
    "urls": {"type": "array", "items": _URL},
    "texte": {"type": "array", "items": _TEXT},
    "geojson": {"type": "object", "description": "GeoJSON (RFC 7946)"},
}

SPECIFICATION: Final = "https://oparl.org/spezifikation/"


def schema_name(typ: str) -> str:
    return f"OParl{typ}"


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"{REF}{name}"}


def _property(art: str) -> dict[str, Any]:
    if art.startswith("objekt:"):
        return ref(schema_name(art.split(":", 1)[1]))
    if art.startswith("objekte:"):
        return {"type": "array", "items": ref(schema_name(art.split(":", 1)[1]))}
    return dict(_ARTEN[art])


def type_schema(typ: str) -> dict[str, Any]:
    """Schema eines Objekttyps aus OParl 1.1 (``hub.api.oparl_types``)."""
    properties = {name: _property(art) for name, art in {**ALLGEMEIN, **FELDER[typ]}.items()}
    properties["type"] = {"const": f"{SCHEMA}{typ}"}
    if typ == "Organization":
        properties["organizationType"] = {"type": "string", "enum": sorted(ORGANIZATION_TYPES)}
    return {
        "title": typ,
        "description": f"Objekttyp {typ} nach OParl 1.1 ({SPECIFICATION}). Eigene Felder tragen einen Namensraum.",
        "type": "object",
        "required": list(PFLICHT.get(typ, ("id", "type"))),
        "properties": properties,
        "patternProperties": {NAMESPACED: {}},
        "additionalProperties": False,
    }


def component_schemas() -> dict[str, dict[str, Any]]:
    """Alle Schemas der OParl-Ausgaben, nach Namen."""
    schemas: dict[str, dict[str, Any]] = {schema_name(typ): type_schema(typ) for typ in FELDER}
    schemas[DELETED] = {
        "title": "Gelöschtes Objekt",
        "description": "Gekürztes Objekt für Gelöschtes oder nicht mehr Öffentliches (OParl 1.1 §2.8).",
        "type": "object",
        "required": ["id", "type", "deleted"],
        "properties": {
            "id": _URL,
            "type": {"type": "string", "pattern": "^https://schema\\.oparl\\.org/1\\.1/[A-Za-z]+$"},
            "created": _ARTEN["zeitpunkt"],
            "modified": _ARTEN["zeitpunkt"],
            "deleted": {"const": True},
        },
        "additionalProperties": False,
    }
    schemas[PAGINATION] = dict(PAGINATION_SCHEMA)
    schemas[LINKS] = dict(LINKS_SCHEMA)
    for typ in FELDER:
        if typ != "System":
            schemas[f"{schema_name(typ)}List"] = {
                "title": f"Liste {typ}",
                **page_schema(_object_or_deleted(typ), pagination=f"{REF}{PAGINATION}", links=f"{REF}{LINKS}"),
            }
    schemas[ERROR] = {
        "title": "Fehler",
        "description": "Fehler einer OParl-Ausgabe.",
        "type": "object",
        "required": ["error", "status"],
        "properties": {"error": {"type": "string"}, "status": {"type": "integer"}},
    }
    schemas[CHANGES] = _changes_schema()
    return schemas


def _object_or_deleted(typ: str) -> dict[str, Any]:
    return {"anyOf": [ref(schema_name(typ)), ref(DELETED)]}


def _changes_schema() -> dict[str, Any]:
    entry = {
        "type": "object",
        "required": ["cursor", "operation", "type", "id", "modified"],
        "properties": {
            "cursor": {"type": "string", "description": "Position hinter diesem Eintrag"},
            "operation": {"type": "string", "enum": ["upsert", "delete", "redact"]},
            "type": {"type": "string", "description": "Typ des Objekts (OParl 1.1)"},
            "id": _URL,
            "modified": _ARTEN["zeitpunkt"],
            "reason": {
                "type": "string",
                "enum": ["quelle_geloescht", "zurueckgenommen", "nichtoeffentlich", "datenschutz"],
            },
        },
    }
    when = {"type": ["string", "null"], "format": "date-time"}
    return {
        "title": "Änderungsfeed",
        "description": (
            "Eine Seite des Änderungsfeeds einer Körperschaft (Erweiterung mandari:changes, Entwurf des "
            "kommParl-Profils Änderungsfeed): Einträge ohne Inhalte, Cursor für die nächste Anfrage."
        ),
        "type": "object",
        "required": ["data", "cursor", "links"],
        "properties": {
            "data": {"type": "array", "items": entry},
            "cursor": {"type": "string"},
            "links": {
                "type": "object",
                "properties": {"self": _URL, "next": _URL, "snapshot": _URL},
            },
            "freshness": {
                "type": "object",
                "description": "Letzter vollständiger Abgleich der Quelle (nur bei geernteten Kommunen)",
                "properties": {"synced_at": when, "full_synced_at": when},
            },
        },
    }


# =============================================================================
# Pfade
# =============================================================================

_TIME_FILTERS: Final = (
    ("created_since", "Nur Objekte, die ab diesem Zeitpunkt angelegt wurden"),
    ("created_until", "Nur Objekte, die bis zu diesem Zeitpunkt angelegt wurden"),
    ("modified_since", "Nur Objekte, die ab diesem Zeitpunkt geändert wurden; enthält auch Gelöschtes"),
    ("modified_until", "Nur Objekte, die bis zu diesem Zeitpunkt geändert wurden"),
)

_LIST_PARAMETERS: Final[list[dict[str, Any]]] = [
    *(
        {
            "name": name,
            "in": "query",
            "required": False,
            "description": f"{text} (ISO 8601 mit Zeitzone)",
            "schema": {"type": "string", "format": "date-time"},
        }
        for name, text in _TIME_FILTERS
    ),
    {
        "name": "page",
        "in": "query",
        "required": False,
        "description": "Seite, ab 1",
        "schema": {"type": "integer", "minimum": 1},
    },
]

_HEADERS: Final[dict[str, Any]] = {
    "ETag": {"description": "Kennung des Inhalts für bedingte Abrufe", "schema": {"type": "string"}},
}

_LIST_HEADERS: Final[dict[str, Any]] = {
    **_HEADERS,
    "Link": {"description": "Blätter-Verweise (first, prev, next, last) nach RFC 8288", "schema": {"type": "string"}},
}


def _json(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {"application/json": {"schema": dict(schema)}}


def _error(description: str) -> dict[str, Any]:
    return {"description": description, "content": _json(ref(ERROR))}


def _problem(description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/problem+json": {"schema": ref(PROBLEM)}}}


_NOT_MODIFIED: Final = {"description": "Unverändert (If-None-Match passt zum ETag)"}
_RATE_LIMITED: Final = {
    **_error("Zu viele Anfragen; Retry-After nennt die Wartezeit in Sekunden"),
    "headers": {"Retry-After": {"schema": {"type": "integer"}}},
}


def _operation(
    operation_id: str,
    summary: str,
    ok: dict[str, Any],
    *,
    parameters: Iterable[Mapping[str, Any]] = (),
    errors: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    responses: dict[str, Any] = {"200": ok, "304": _NOT_MODIFIED, "404": _error("Nicht gefunden")}
    responses.update(errors or {})
    responses["429"] = _RATE_LIMITED
    operation: dict[str, Any] = {"operationId": operation_id, "summary": summary, "responses": responses}
    params = [dict(parameter) for parameter in parameters]
    if params:
        operation["parameters"] = params
    return {"get": operation}


def _ok(description: str, schema: Mapping[str, Any], *, listing: bool = False) -> dict[str, Any]:
    return {
        "description": description,
        "headers": dict(_LIST_HEADERS if listing else _HEADERS),
        "content": _json(schema),
    }


def _uuid(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "description": description,
        "schema": {"type": "string", "format": "uuid"},
    }


_BAD_FILTER: Final = {"400": _error("Ungültiger Zeitfilter oder ungültige Seitennummer")}
_PAUSED: Final = {
    "503": {
        **_error("Die Kommune hat die Veröffentlichung vorübergehend abgeschaltet"),
        "headers": {"Retry-After": {"schema": {"type": "integer"}}},
    }
}


def oparl_paths(
    *,
    prefix: str,
    key: str,
    system: str,
    bodies: str,
    body: str,
    lists: Mapping[str, str],
    objects: Iterable[str],
    object_path: str,
    feed: str | None,
    snapshot: str | None,
    parameters: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """
    Pfade einer OParl-Ausgabe.

    ``prefix`` ist der Anfang aller Pfade, ``key`` der Anfang der ``operationId``. ``system``, ``bodies``
    und ``body`` sind die Pfade von System, Körperschaftsliste und Körperschaft, ``lists`` nennt je Pfad einer
    externen Liste ihren Objekttyp (Kleinbuchstaben wie in ``KINDS``), ``objects`` die Objekttypen mit
    eigenem Endpunkt, ``object_path`` dessen Pfad (mit ``{kind}`` und ``{id}``). ``feed`` und ``snapshot``
    sind die Pfade von Änderungsfeed und Snapshot. ``parameters`` gelten für alle Pfade (etwa der Mandant).
    """
    shared = [dict(parameter) for parameter in parameters]
    body_id = [_uuid("id", "Kennung der Körperschaft")] if "{id}" in body else []
    paths: dict[str, Any] = {
        f"{prefix}{system}": _operation(
            f"{key}_system", "System", _ok("System-Objekt (OParl 1.1)", ref(schema_name("System"))), parameters=shared
        ),
        f"{prefix}{bodies}": _operation(
            f"{key}_bodies",
            "Körperschaften",
            _ok("Externe Liste der Körperschaften", ref("OParlBodyList"), listing=True),
            parameters=[*shared, *_LIST_PARAMETERS],
            errors=_BAD_FILTER,
        ),
        f"{prefix}{body}": _operation(
            f"{key}_body",
            "Körperschaft",
            _ok("Körperschaft", _object_or_deleted("Body")),
            parameters=[*shared, *body_id],
            errors=_PAUSED,
        ),
    }
    for path, kind in lists.items():
        typ = KINDS[kind]
        paths[f"{prefix}{path}"] = _operation(
            f"{key}_list_{path.strip('/').rsplit('/', 1)[-1]}",
            f"Externe Liste {typ}",
            _ok(f"Externe Liste {typ}", ref(f"{schema_name(typ)}List"), listing=True),
            parameters=[*shared, *body_id, *_LIST_PARAMETERS],
            errors={**_BAD_FILTER, **_PAUSED},
        )
    for kind in objects:
        typ = KINDS[kind]
        paths[f"{prefix}{object_path.replace('{kind}', kind)}"] = _operation(
            f"{key}_{kind}",
            typ,
            _ok(f"{typ} (gelöscht: gekürztes Objekt)", _object_or_deleted(typ)),
            parameters=[*shared, _uuid("id", f"Kennung {typ}")],
            errors=_PAUSED,
        )
    if feed:
        paths[f"{prefix}{feed}"] = _operation(
            f"{key}_changes",
            "Änderungsfeed",
            _ok("Seite des Änderungsfeeds (Erweiterung mandari:changes, je Installation einzuschalten)", ref(CHANGES)),
            parameters=[
                *shared,
                *body_id,
                {
                    "name": "after",
                    "in": "query",
                    "required": False,
                    "description": "Cursor der vorigen Antwort",
                    "schema": {"type": "string"},
                },
                {
                    "name": "limit",
                    "in": "query",
                    "required": False,
                    "description": "Höchstzahl der Einträge (Standard 100, höchstens 1000)",
                    "schema": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
            ],
            errors={
                "400": _error("Ungültiger Parameter"),
                "410": _problem("Cursor abgelaufen oder Kommune zurückgenommen; neu über den Snapshot einsteigen"),
                **_PAUSED,
            },
        )
    if snapshot:
        paths[f"{prefix}{snapshot}"] = {
            "get": {
                "operationId": f"{key}_snapshot",
                "summary": "Snapshot",
                "description": (
                    "Gesamtstand der Körperschaft als NDJSON: die erste Zeile nennt snapshot_cursor und die Zahl "
                    "der Objekte, danach je Zeile ein Objekt. Fortsetzung über den Änderungsfeed ab snapshot_cursor."
                ),
                "parameters": [*shared, *body_id],
                "responses": {
                    "200": {
                        "description": "Snapshot",
                        "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
                    },
                    "404": _error("Nicht gefunden oder nicht eingeschaltet"),
                    "410": _problem("Kommune zurückgenommen"),
                    "429": _RATE_LIMITED,
                    **_PAUSED,
                },
            }
        }
    return paths

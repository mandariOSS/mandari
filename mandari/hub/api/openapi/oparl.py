# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die OParl-Ausgaben in der OpenAPI-Beschreibung: Schemas der Objekttypen und die Pfade beider Ausgaben.

Die Endpunkte selbst bleiben, wie sie sind (``hub.api.aggregator``, ``apps/session/api/oparl.py``); hier
steht nur ihre Beschreibung.

- **Schemas** stammen aus der kommParl-Spezifikation (``hub.api.kommparl``): die Objekttypen von OParl 1.1 mit den
  Ergänzungen von kommParl, für OpenAPI 3.1 in JSON Schema 2020-12 umgewandelt, und die Schemas der Profile
  Änderungsfeed und Snapshot (``Kommparl…``). Dieselben Schemas prüfen die Ausgaben (``hub.api.tests.konformitaet``,
  auch im OParl-Validator der CI). Gelöschtes ist ein gekürztes Objekt (``OParlDeleted``, OParl 1.1 §2.8).
- **Ergänzungen**, die die Spezifikation nicht als Schema hergibt (``SUPPLEMENTS``, ``COMMON_SUPPLEMENTS``): die
  sieben Werte von ``organizationType`` und ``license`` als URL (beides nur im Text der Spezifikation, Kapitel 6)
  sowie die abgekündigten Eigenschaften ``mandari:changes`` und ``mandari:snapshot`` am Body.
- **Eigene Felder** erlaubt die Beschreibung mit jedem Namensraum (``NAMESPACED``), wie vor der Umstellung auf
  kommParl; ein strengeres Muster wäre für Werkzeuge wie oasdiff eine brechende Änderung der Beschreibung. Dass
  Eigenschaften mit ``kommparl:`` im Schema stehen (``hub.api.kommparl.VENDOR_PROPERTY``), prüft die
  Konformitätsprüfung.
- **Pfade** beschreibt ``oparl_paths`` für eine Ausgabe: Aggregator (``aggregator_description``) und je
  Session-Mandant (das Fachmodul meldet seinen Bereich mit ``tenant_description`` an). Externe Listen haben
  Seiten (``apps.common.api.pages``) und Zeitfilter, jede Antwort einen ``ETag``; Fehler kommen als JSON
  ``{"error", "status"}``. Änderungsfeed und Snapshot gibt es zweimal: nach den Profilen von kommParl
  (``kommparl:changes``, ``kommparl:snapshot``) und in der älteren Form (``mandari:changes``, ``mandari:snapshot``,
  abgekündigt; Absagen dort als Problem nach RFC 9457).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from apps.common.api.pages import LINKS_SCHEMA, PAGINATION_SCHEMA, page_schema
from hub.api import kommparl
from hub.api.changes import MANDARI_FEED_DEPRECATION
from hub.ris.canonical import ORGANIZATION_TYPES

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
KOMMPARL_CHANGES: Final = kommparl.profile_component_name("ChangeList")
KOMMPARL_CURSOR_EXPIRED: Final = kommparl.profile_component_name("CursorExpired")
KOMMPARL_SNAPSHOT_HEADER: Final = kommparl.profile_component_name("SnapshotHeader")
KOMMPARL_SNAPSHOT_END: Final = kommparl.profile_component_name("SnapshotEnd")

SPECIFICATION: Final = "https://oparl.org/spezifikation/"

_URL: Final[dict[str, Any]] = {"type": "string", "format": "uri"}
_DATE_TIME: Final[dict[str, Any]] = {"type": "string", "format": "date-time"}

#: Wegfall der älteren Form von Änderungsfeed und Snapshot (``x-sunset``, Tag)
SUNSET: Final = MANDARI_FEED_DEPRECATION.sunset.date().isoformat()
#: Derselbe Tag im Text der Beschreibung
SUNSET_TEXT: Final = MANDARI_FEED_DEPRECATION.sunset.strftime("%d.%m.%Y")


def _deprecated_extension(successor: str, what: str) -> dict[str, Any]:
    return {
        **_URL,
        "deprecated": True,
        "x-sunset": SUNSET,
        "description": (
            f"Abgekündigt, entfällt frühestens am {SUNSET_TEXT}: Adresse {what} in der älteren Form. "
            f"Nachfolger ist {successor} (Profil von kommParl {kommparl.FASSUNG})."
        ),
    }


#: Eigenschaften aller Objekttypen, die die Spezifikation nicht als Schema hergibt: ``license`` ist eine URL
#: (kommParl Kapitel 6, „Klarstellungen“; die Schemas nennen das Format nur bei System und Body).
COMMON_SUPPLEMENTS: Final[dict[str, dict[str, Any]]] = {"license": _URL}

#: Eigenschaften je Objekttyp, die die Spezifikation nicht als Schema hergibt; sie ergänzen das umgewandelte Schema.
#:
#: - ``organizationType``: Die sieben Werte stehen in OParl 1.1 und kommParl nur im Text (kommParl Kapitel 6,
#:   „Klarstellungen“); die Ausgaben nennen nur diese (``hub.ris.canonical.ORGANIZATION_TYPES``).
#: - ``mandari:changes`` und ``mandari:snapshot``: eigene Eigenschaften, abgekündigt zugunsten von
#:   ``kommparl:changes`` und ``kommparl:snapshot`` (ADR „Eine Schnittstelle“, Regel 2).
SUPPLEMENTS: Final[dict[str, dict[str, dict[str, Any]]]] = {
    "Organization": {
        "organizationType": {"type": "string", "enum": sorted(ORGANIZATION_TYPES)},
    },
    "Body": {
        "mandari:changes": _deprecated_extension("kommparl:changes", "des Änderungsfeeds"),
        "mandari:snapshot": _deprecated_extension("kommparl:snapshot", "des Snapshots"),
    },
}


def schema_name(typ: str) -> str:
    return kommparl.component_name(typ)


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"{REF}{name}"}


def type_schema(typ: str) -> dict[str, Any]:
    """Schema eines Objekttyps: aus kommParl umgewandelt (``hub.api.kommparl.type_schema``) und ergänzt."""
    schema = kommparl.type_schema(typ, vendor_property=NAMESPACED)
    schema["properties"].update({**COMMON_SUPPLEMENTS, **SUPPLEMENTS.get(typ, {})})
    return {
        "title": typ,
        "description": (
            f"Objekttyp {typ} nach OParl 1.1 ({SPECIFICATION}) mit den Ergänzungen von kommParl {kommparl.FASSUNG}. "
            "Eigene Felder tragen einen Namensraum."
        ),
        **{key: value for key, value in schema.items() if key != "title"},
    }


def component_schemas() -> dict[str, dict[str, Any]]:
    """Alle Schemas der OParl-Ausgaben, nach Namen."""
    types = kommparl.object_types()
    schemas: dict[str, dict[str, Any]] = {schema_name(typ): type_schema(typ) for typ in types}
    schemas[DELETED] = {
        "title": "Gelöschtes Objekt",
        "description": "Gekürztes Objekt für Gelöschtes oder nicht mehr Öffentliches (OParl 1.1 §2.8).",
        "type": "object",
        "required": ["id", "type", "deleted"],
        "properties": {
            "id": _URL,
            "type": {"type": "string", "pattern": "^https://schema\\.oparl\\.org/1\\.1/[A-Za-z]+$"},
            "created": _DATE_TIME,
            "modified": _DATE_TIME,
            "deleted": {"const": True},
        },
        "additionalProperties": False,
    }
    schemas[PAGINATION] = dict(PAGINATION_SCHEMA)
    schemas[LINKS] = dict(LINKS_SCHEMA)
    for typ in types:
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
    for profile, name in kommparl.profile_schemas():
        schemas[kommparl.profile_component_name(name)] = kommparl.profile_component(profile, name)
    return schemas


def _object_or_deleted(typ: str) -> dict[str, Any]:
    return {"anyOf": [ref(schema_name(typ)), ref(DELETED)]}


def _changes_schema() -> dict[str, Any]:
    """Seite des Änderungsfeeds in der älteren Form (``mandari:changes``, abgekündigt)."""
    entry = {
        "type": "object",
        "required": ["cursor", "operation", "type", "id", "modified"],
        "properties": {
            "cursor": {"type": "string", "description": "Position hinter diesem Eintrag"},
            "operation": {"type": "string", "enum": ["upsert", "delete", "redact"]},
            "type": {"type": "string", "description": "Typ des Objekts (OParl 1.1)"},
            "id": _URL,
            "modified": _DATE_TIME,
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
            "Eine Seite des Änderungsfeeds einer Körperschaft in der älteren Form (mandari:changes, abgekündigt, "
            f"entfällt frühestens am {SUNSET_TEXT}; Nachfolger: kommparl:changes, Schema {KOMMPARL_CHANGES}): Einträge "
            "ohne Inhalte, Cursor für die nächste Anfrage."
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

_FEED_PARAMETERS: Final[list[dict[str, Any]]] = [
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
]

_HEADERS: Final[dict[str, Any]] = {
    "ETag": {"description": "Kennung des Inhalts für bedingte Abrufe", "schema": {"type": "string"}},
}

_LIST_HEADERS: Final[dict[str, Any]] = {
    **_HEADERS,
    "Link": {"description": "Blätter-Verweise (first, prev, next, last) nach RFC 8288", "schema": {"type": "string"}},
}

_DEPRECATION_HEADERS: Final[dict[str, Any]] = {
    "Deprecation": {
        "description": "Zeitpunkt der Abkündigung nach RFC 9745 (@ und Unix-Sekunden)",
        "schema": {"type": "string"},
    },
    "Sunset": {"description": "Zeitpunkt des Wegfalls nach RFC 8594 (HTTP-Datum)", "schema": {"type": "string"}},
    "Link": {
        "description": 'Nachfolger nach RFC 8288 (rel="successor-version"): die Adresse nach dem kommParl-Profil',
        "schema": {"type": "string"},
    },
}


def _json(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {"application/json": {"schema": dict(schema)}}


def _error(description: str) -> dict[str, Any]:
    return {"description": description, "content": _json(ref(ERROR))}


def _problem(description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/problem+json": {"schema": ref(PROBLEM)}}}


def _oparl_error(description: str) -> dict[str, Any]:
    """Fehlerobjekt nach OParl 1.1, Kapitel „Ausnahmebehandlung“ (Schema des Profils Änderungsfeed)."""
    return {"description": description, "content": _json(ref(KOMMPARL_CURSOR_EXPIRED))}


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


def _deprecate(item: dict[str, Any], successor: str) -> dict[str, Any]:
    """Endpunkt der älteren Form als abgekündigt kennzeichnen: ``deprecated``, ``x-sunset``, Header der Abkündigung."""
    operation = item["get"]
    operation["deprecated"] = True
    operation["x-sunset"] = SUNSET
    operation["description"] = (
        f"Abgekündigt, entfällt frühestens am {SUNSET_TEXT}. Nachfolger: {successor} (Profil von kommParl "
        f"{kommparl.FASSUNG}); die Antworten nennen ihn im Header Link. {operation.get('description', '')}"
    ).strip()
    for code in ("200", "410", "503"):
        response = operation["responses"].get(code)
        if response is not None:
            # Neue Antwort statt Änderung: Antworten wie ``_PAUSED`` werden von mehreren Pfaden geteilt
            headers = {**response.get("headers", {}), **_DEPRECATION_HEADERS}
            operation["responses"][code] = {**response, "headers": headers}
    return item


_BAD_FILTER: Final = {"400": _error("Ungültiger Zeitfilter oder ungültige Seitennummer")}
_PAUSED: Final = {
    "503": {
        **_error("Die Kommune hat die Veröffentlichung vorübergehend abgeschaltet"),
        "headers": {"Retry-After": {"schema": {"type": "integer"}}},
    }
}


def _feed_paths(
    prefix: str,
    key: str,
    feed: str | None,
    snapshot: str | None,
    kommparl_feed: str | None,
    kommparl_snapshot: str | None,
    parameters: list[dict[str, Any]],
) -> dict[str, Any]:
    """Änderungsfeed und Snapshot: nach den Profilen von kommParl und in der älteren, abgekündigten Form."""
    paths: dict[str, Any] = {}
    if kommparl_feed:
        paths[f"{prefix}{kommparl_feed}"] = _operation(
            f"{key}_kommparl_changes",
            "Änderungsfeed (kommParl)",
            _ok(
                f"Seite des Änderungsfeeds nach dem Profil {kommparl.PROFILE_CHANGES} (kommparl:changes am Body, "
                "je Installation einzuschalten); links.next nur, solange weitere Einträge vorliegen",
                ref(KOMMPARL_CHANGES),
            ),
            parameters=[*parameters, *_FEED_PARAMETERS],
            errors={
                "400": _error("Ungültiger Parameter"),
                "410": _oparl_error(
                    "Cursor abgelaufen (mit kommparl:snapshot) oder Kommune zurückgenommen; Bestand neu aufbauen"
                ),
                **_PAUSED,
            },
        )
    if kommparl_snapshot:
        paths[f"{prefix}{kommparl_snapshot}"] = {
            "get": {
                "operationId": f"{key}_kommparl_snapshot",
                "summary": "Snapshot (kommParl)",
                "description": (
                    f"Gesamtstand der Körperschaft nach dem Profil {kommparl.PROFILE_SNAPSHOT} als NDJSON: die "
                    f"Kopfzeile ({KOMMPARL_SNAPSHOT_HEADER}) nennt den Cursor, mit dem der Änderungsfeed fortsetzt, "
                    "danach je Zeile ein Objekt (der Body und jedes öffentliche Objekt der Körperschaft genau "
                    f"einmal, ohne Gelöschtes), zuletzt die Schlusszeile ({KOMMPARL_SNAPSHOT_END}) mit der Zahl der "
                    "Objektzeilen. Fehlt sie oder stimmt die Zahl nicht, ist der Snapshot unvollständig."
                ),
                "parameters": [dict(parameter) for parameter in parameters],
                "responses": {
                    "200": {
                        "description": "Snapshot",
                        "headers": {
                            "Snapshot-Cursor": {
                                "description": "Cursor der Kopfzeile",
                                "schema": {"type": "string"},
                            }
                        },
                        "content": {"application/x-ndjson": {"schema": {"type": "string"}}},
                    },
                    "404": _error("Nicht gefunden oder nicht eingeschaltet"),
                    "410": _oparl_error("Kommune zurückgenommen oder ihr Body gelöscht"),
                    "429": _RATE_LIMITED,
                    **_PAUSED,
                },
            }
        }
    if feed:
        item = _operation(
            f"{key}_changes",
            "Änderungsfeed",
            _ok("Seite des Änderungsfeeds (Erweiterung mandari:changes, je Installation einzuschalten)", ref(CHANGES)),
            parameters=[*parameters, *_FEED_PARAMETERS],
            errors={
                "400": _error("Ungültiger Parameter"),
                "410": _problem("Cursor abgelaufen oder Kommune zurückgenommen; neu über den Snapshot einsteigen"),
                **_PAUSED,
            },
        )
        paths[f"{prefix}{feed}"] = _deprecate(item, "kommparl:changes") if kommparl_feed else item
    if snapshot:
        item = {
            "get": {
                "operationId": f"{key}_snapshot",
                "summary": "Snapshot",
                "description": (
                    "Gesamtstand der Körperschaft als NDJSON: die erste Zeile nennt snapshot_cursor und die Zahl "
                    "der Objekte, danach je Zeile ein Objekt. Fortsetzung über den Änderungsfeed ab snapshot_cursor."
                ),
                "parameters": [dict(parameter) for parameter in parameters],
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
        paths[f"{prefix}{snapshot}"] = _deprecate(item, "kommparl:snapshot") if kommparl_snapshot else item
    return paths


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
    kommparl_feed: str | None = None,
    kommparl_snapshot: str | None = None,
    parameters: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """
    Pfade einer OParl-Ausgabe.

    ``prefix`` ist der Anfang aller Pfade, ``key`` der Anfang der ``operationId``. ``system``, ``bodies``
    und ``body`` sind die Pfade von System, Körperschaftsliste und Körperschaft, ``lists`` nennt je Pfad einer
    externen Liste ihren Objekttyp (Kleinbuchstaben wie in ``KINDS``), ``objects`` die Objekttypen mit
    eigenem Endpunkt, ``object_path`` dessen Pfad (mit ``{kind}`` und ``{id}``). ``feed`` und ``snapshot``
    sind die Pfade von Änderungsfeed und Snapshot in der älteren Form, ``kommparl_feed`` und
    ``kommparl_snapshot`` die nach den Profilen von kommParl; gibt es sie, ist die ältere Form abgekündigt.
    ``parameters`` gelten für alle Pfade (etwa der Mandant).
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
    paths.update(_feed_paths(prefix, key, feed, snapshot, kommparl_feed, kommparl_snapshot, [*shared, *body_id]))
    return paths

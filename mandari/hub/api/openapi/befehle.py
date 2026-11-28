# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschreibung der Befehle in der gemeinsamen Schnittstelle (Issue #1588, ``hub.api.befehle``).

Jeder Befehl mit Freigabe (``Dispatcher.exposures``) bekommt eine Operation ``POST /api/mandari/v1/<bereich>/
{mandant}/befehle/<befehl>/v<version>`` mit ``x-mandari-command``. Der Inhalt ist das Schema des Vertrags aus
``hub/contracts`` (ohne ``$schema`` und ``$id``), die Antwort die Quittung. So entsteht die Beschreibung aus denselben
Quellen wie die Prüfung: Ein neuer Befehl erscheint ohne weiteren Code, sobald sein Handler registriert ist.
"""

from __future__ import annotations

from typing import Any, Final

from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

from .build import COMMAND_EXTENSION
from .registry import Area, Description

#: Platzhalter für den Mandanten in Adressen (ein gültiger Kurzname, der in keiner echten Adresse vorkommt)
_TENANT: Final = "mandant-platzhalter-1588"
SECURITY_SCHEME: Final = "mandariBefehle"
RECEIPT: Final = "Quittung"

RECEIPT_SCHEMA: Final[dict[str, Any]] = {
    "title": "Quittung",
    "description": (
        "Quittung eines Befehls. Eine Wiederholung mit demselben Idempotency-Key erhält dieselbe Quittung. Sie nennt "
        "nur Kennungen, Codes und Zeitpunkte, nie Inhalte."
    ),
    "type": "object",
    "required": ["command", "version", "reference", "received_at", "content_hash", "aggregate_id", "data"],
    "properties": {
        "command": {"type": "string", "description": "Name des Befehls"},
        "version": {"type": "integer", "minimum": 1},
        "reference": {"type": "string", "description": "Eingangsnummer bzw. Kennung beim Eigentümer"},
        "received_at": {"type": "string", "format": "date-time", "description": "Eingangszeit"},
        "content_hash": {
            "type": "string",
            "description": (
                "SHA-256 über das kanonische JSON des Inhalts (RFC 8785), hexadezimal; bei personenbezogenen Befehlen "
                "ein HMAC mit einem Schlüssel der Installation"
            ),
        },
        "aggregate_id": {"type": ["string", "null"], "format": "uuid", "description": "Kennung des Objekts"},
        "data": {"type": "object", "description": "Weitere Kennungen und Codes des Eigentümers"},
    },
}

#: Kopfzeilen jeder Operation
_IDEMPOTENCY: Final[dict[str, Any]] = {
    "name": "Idempotency-Key",
    "in": "header",
    "required": True,
    "description": (
        "Pflicht. Dieselbe Anfrage mit demselben Schlüssel wirkt nur einmal und erhält dieselbe Quittung; derselbe "
        "Schlüssel mit anderem Inhalt ergibt 422. 1 bis 255 sichtbare ASCII-Zeichen, auch in Anführungszeichen."
    ),
    "schema": {"type": "string", "minLength": 1, "maxLength": 257},
}
_CORRELATION: Final[dict[str, Any]] = {
    "name": "X-Correlation-ID",
    "in": "header",
    "required": False,
    "description": "Kennung des Vorgangs (UUID); sie steht im Protokoll und in den Ereignissen des Befehls.",
    "schema": {"type": "string", "format": "uuid"},
}


def _problem(description: str, **headers: str) -> dict[str, Any]:
    response: dict[str, Any] = {
        "description": description,
        "content": {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}},
    }
    if headers:
        response["headers"] = {
            name: {"description": text, "schema": {"type": "string"}} for name, text in headers.items()
        }
    return response


_RESPONSES: Final[dict[str, Any]] = {
    "400": _problem("Idempotency-Key fehlt oder Inhalt kein JSON-Objekt"),
    "401": _problem("Anmeldung fehlt oder Token ungültig", **{"WWW-Authenticate": "Bearer nach RFC 6750"}),
    "403": _problem("Token ohne Person oder ohne passenden Scope, CSRF-Token fehlt, Recht im Mandanten fehlt"),
    "404": _problem("Befehl, Mandant oder Objekt unbekannt bzw. für den Aufrufer nicht sichtbar"),
    "405": _problem("Nur POST", Allow="POST"),
    "409": _problem("Fachlicher Konflikt beim Eigentümer"),
    "413": _problem("Inhalt zu groß"),
    "415": _problem("Kein application/json"),
    "422": _problem("Inhalt passt nicht zum Vertrag (errors mit JSON-Pointer) oder Schlüssel schon verwendet"),
    "429": _problem("Zu viele Anfragen", **{"Retry-After": "Wartezeit in Sekunden"}),
    "500": _problem("Interner Fehler; mit demselben Idempotency-Key wiederholen"),
}


def component_name(name: str, version: int) -> str:
    """Name des Schemas eines Befehls in ``components.schemas``."""
    return f"befehl.{name}.v{version}"


def operation_id(name: str, version: int) -> str:
    return f"befehl_{name.replace('.', '_')}_v{version}"


def command_path(area: str, name: str, version: int) -> str:
    """Adresse der Operation in der Schreibweise von OpenAPI (``{mandant}``)."""
    kwargs = {"area": area, "tenant": _TENANT, "name": name, "version": version}
    return reverse("befehle:befehl", kwargs=kwargs).replace(_TENANT, "{mandant}")


def request_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Schema des Vertrags als Inhalt der Anfrage: ohne ``$schema`` und ``$id`` (die Beschreibung ist 3.1)."""
    return {key: value for key, value in schema.items() if key not in {"$schema", "$id"}}


def description() -> Description:
    """Operationen aller freigegebenen Befehle."""
    from apps.accounts.oidc import catalog
    from hub.commands import get_dispatcher, tenant_area

    # Das Register, gegen das der Dispatcher die Befehle prüft
    registry = get_dispatcher().registry
    paths: dict[str, Any] = {}
    schemas: dict[str, Any] = {RECEIPT: dict(RECEIPT_SCHEMA)}
    scopes: set[str] = set()
    for (name, version), exposure in get_dispatcher().exposures.items():
        tenants = tenant_area(exposure.area)
        if tenants is None:
            raise ImproperlyConfigured(f"{name} v{version}: Mandantenbereich {exposure.area!r} ist nicht angemeldet")
        contract = registry.get(name, version)
        schemas[component_name(name, version)] = request_schema(contract.schema)
        scopes.update(exposure.scopes)
        paths[command_path(exposure.area, name, version)] = {
            "post": {
                "operationId": operation_id(name, version),
                "summary": contract.title,
                "description": (
                    f"{contract.description}\n\nRechte und Mandant prüft der Eigentümer ({contract.owner}) wie in der "
                    "Oberfläche. Anmeldung: Access-Token im Namen einer Person mit einem der Scopes "
                    f"{', '.join(exposure.scopes)}; die eigene Oberfläche nutzt die Browser-Sitzung mit CSRF-Token "
                    "(Header X-CSRFToken)."
                ),
                COMMAND_EXTENSION: name,
                "security": [{SECURITY_SCHEME: [scope]} for scope in exposure.scopes],
                "parameters": [
                    {
                        "name": "mandant",
                        "in": "path",
                        "required": True,
                        "description": tenants.description,
                        "schema": {"type": "string"},
                    },
                    dict(_IDEMPOTENCY),
                    dict(_CORRELATION),
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{component_name(name, version)}"}
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "Quittung",
                        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{RECEIPT}"}}},
                    },
                    **_RESPONSES,
                },
            }
        }
    security = {
        SECURITY_SCHEME: {
            "type": "oauth2",
            "description": (
                "OpenID-Connect-Anbieter der Installation (Discovery unter /.well-known/openid-configuration). Nur "
                "Access-Tokens im Namen einer Person; PKCE mit S256 ist Pflicht. Ein Token darf nie mehr als die "
                "Person selbst."
            ),
            "flows": {
                "authorizationCode": {
                    "authorizationUrl": "/oauth/authorize/",
                    "tokenUrl": "/oauth/token/",
                    "refreshUrl": "/oauth/token/",
                    "scopes": {scope: catalog.DESCRIPTIONS[scope] for scope in sorted(scopes)},
                }
            },
        }
    }
    return {"paths": paths, "components": {"schemas": schemas, "securitySchemes": security}}


AREA: Final = Area(
    key="befehle",
    title="Befehle",
    description=(
        "Jede Änderung als Befehl beim Eigentümer: dieselben Befehle, die die eigene Oberfläche nutzt, mit "
        "Idempotency-Key, Quittung und Fehlern nach RFC 9457. Anmeldung mit Access-Token im Namen einer Person."
    ),
    entry="/api/mandari/v1/{bereich}/{mandant}/befehle/",
    openapi=description,
    order=40,
)

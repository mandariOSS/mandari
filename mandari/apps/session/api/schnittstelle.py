# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bereiche von Session in der gemeinsamen Schnittstelle (``docs/adr/20261105-eine-schnittstelle-api-first.md``).

Die Drehscheibe kennt Session nicht (A1); Session meldet seine Bereiche deshalb beim Start an
(``SessionConfig.ready``):

- **OParl je Mandant** (``/session/<mandant>/api/oparl/``): Pfade aus den Routen und Listen von
  ``apps/session/api/oparl.py``, beschrieben mit den OParl-Bausteinen der Drehscheibe
  (``hub.api.openapi.oparl``). Die Endpunkte selbst bleiben unverändert.
- **OParl je Mandant, nichtöffentlich** (``/session/<mandant>/api/oparl/nichtoeffentlich/``, Issue #565): dieselben
  Pfade nach dem Profil Nichtöffentliches von kommParl, mit Anmeldung (``securitySchemes``), Fehlern nach RFC 9457
  und den Metadaten der geschützten Ressource (RFC 9728); ohne ``ETag``, ohne Änderungsfeed und Snapshot.
- **Profil Einreichung** (``…/api/oparl/body/kommparl/submissions/`` und weitere, Issue #1228): Pfade, Schemas aus
  kommParl und Sicherheitsschema in ``apps/session/api/einreichung_beschreibung.py``; jeder Endpunkt, der einen
  Befehl annimmt, nennt ihn in ``x-mandari-command``.
- **Session-API v1** (``/api/v1/session/``): die Beschreibung, die django-ninja aus den Endpunkten erzeugt –
  dieselbe wie unter ``/api/v1/session/openapi.json``. Erst hier kommt an jedem Endpunkt, der einen Befehl
  annimmt, ``x-mandari-command`` dazu (``SESSION_V1_COMMANDS``); die eigene Beschreibung der API v1 bleibt so
  unverändert.
"""

from __future__ import annotations

import copy
from typing import Any, Final

from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

from apps.accounts.oidc import catalog
from hub.api import kommparl
from hub.api.openapi import COMMAND_EXTENSION, Area, Description, register_area
from hub.api.openapi.oparl import KINDS, METHODS_GET, component_schemas, oparl_paths

#: Endpunkte der Session-API v1, die einen Befehl annehmen: ``operationId`` -> Name des Befehls
SESSION_V1_COMMANDS: Final[dict[str, str]] = {
    "apps_session_api_v1_endpoints_submit_application": "submission.submit",
}

#: Platzhalter für den Mandanten in Adressen (ein gültiger Slug, der in keiner echten Adresse vorkommt)
_TENANT: Final = "mandant-platzhalter-1205"
_TENANT_PARAMETER: Final[dict[str, Any]] = {
    "name": "tenant_slug",
    "in": "path",
    "required": True,
    "description": "Kurzname des Mandanten (Kommune)",
    "schema": {"type": "string"},
}


def _path(name: str, **kwargs: Any) -> str:
    """Adresse einer Route je Mandant in der Schreibweise von OpenAPI (``{tenant_slug}``, ``{id}``, ``{kind}``)."""
    path = reverse(f"session:{name}", kwargs={"tenant_slug": _TENANT, **kwargs})
    return path.replace(_TENANT, "{tenant_slug}")


def tenant_oparl_description() -> Description:
    """Pfade der OParl-Ausgabe je Mandant."""
    from apps.session.api import oparl

    prefix = _path("oparl_system")
    marker = "00000000-0000-0000-0000-000000001205"

    def relative(path: str) -> str:
        return path.removeprefix(prefix).replace(marker, "{id}")

    object_path = relative(_path("oparl_object", kind="KIND", pk=marker)).replace("KIND", "{kind}")
    paths = oparl_paths(
        prefix=prefix,
        key="oparl_mandant",
        system="",
        bodies=relative(_path("oparl_bodies")),
        body=relative(_path("oparl_body")),
        lists={relative(_path("oparl_list", segment=segment)): spec[3] for segment, spec in oparl.LIST_SPECS.items()},
        objects=[kind for kind in [*oparl.OBJECT_SPECS, "location"] if kind in KINDS],
        object_path=object_path,
        feed=relative(_path("oparl_changes")),
        snapshot=relative(_path("oparl_snapshot")),
        kommparl_feed=relative(_path("oparl_kommparl_changes")),
        kommparl_snapshot=relative(_path("oparl_kommparl_snapshot")),
        parameters=[_TENANT_PARAMETER],
    )
    download = _path("oparl_file_download", pk=marker).replace(marker, "{id}")
    paths[download] = {
        "get": {
            "operationId": "oparl_mandant_file_download",
            "summary": "Datei abrufen",
            "description": "Öffentliche Anlage; im Browser nur PDF und Rasterbilder, sonst als Download.",
            "parameters": [
                _TENANT_PARAMETER,
                {
                    "name": "id",
                    "in": "path",
                    "required": True,
                    "description": "Kennung der Datei",
                    "schema": {"type": "string", "format": "uuid"},
                },
                {
                    "name": "download",
                    "in": "query",
                    "required": False,
                    "description": "Gesetzt: immer als Download",
                    "schema": {"type": "string"},
                },
            ],
            "responses": {
                "200": {"description": "Inhalt der Datei", "content": {"*/*": {"schema": {}}}},
                "404": {
                    "description": "Nicht gefunden",
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OParlError"}}},
                },
            },
        }
    }
    return {"paths": paths, "components": {"schemas": component_schemas()}}


#: Anmeldung an der nichtöffentlichen Ausgabe: Code-Ablauf mit PKCE für Personen, Client Credentials mit
#: private_key_jwt für gebundene Dienste (docs/schnittstelle/anmeldung.md)
NONPUBLIC_SECURITY: Final = "kommparlNichtoeffentlich"
_NONPUBLIC_SCHEME: Final[dict[str, Any]] = {
    "type": "oauth2",
    "description": (
        "Access-Token des OpenID-Connect-Anbieters im Header Authorization: Bearer. Personen: Code-Ablauf mit PKCE, "
        f"Scope {catalog.RATSDATEN_NICHTOEFFENTLICH}, zweiter Faktor, höchstens sieben Tage nach der Anmeldung. "
        f"Dienste: Client Credentials mit private_key_jwt (RFC 7523), Scope {catalog.SITZUNG_LESEN}, gebunden an "
        "genau diesen Mandanten."
    ),
    "flows": {
        "authorizationCode": {
            "authorizationUrl": "/oauth/authorize/",
            "tokenUrl": "/oauth/token/",
            "refreshUrl": "/oauth/token/",
            "scopes": {catalog.RATSDATEN_NICHTOEFFENTLICH: catalog.DESCRIPTIONS[catalog.RATSDATEN_NICHTOEFFENTLICH]},
        },
        "clientCredentials": {
            "tokenUrl": "/oauth/token/",
            "scopes": {catalog.SITZUNG_LESEN: catalog.DESCRIPTIONS[catalog.SITZUNG_LESEN]},
        },
    },
}
_PROBLEM_REF: Final = {"$ref": "#/components/schemas/" + kommparl.profile_component_name("NonPublicProblem")}
_PRIVATE_HEADERS: Final[dict[str, Any]] = {
    "Cache-Control": {"description": "Immer private, no-store", "schema": {"type": "string"}},
    "Vary": {"description": "Enthält Authorization", "schema": {"type": "string"}},
}
_WWW_AUTHENTICATE: Final[dict[str, Any]] = {
    "WWW-Authenticate": {
        "description": "Bearer mit resource_metadata (RFC 9728) und error nach RFC 6750",
        "schema": {"type": "string"},
    }
}


def _nonpublic_problem(description: str, *, authenticate: bool = False) -> dict[str, Any]:
    headers = {**_PRIVATE_HEADERS, **(_WWW_AUTHENTICATE if authenticate else {})}
    return {
        "description": description,
        "headers": headers,
        "content": {"application/problem+json": {"schema": _PROBLEM_REF}},
    }


def _nonpublic_operation(operation: dict[str, Any]) -> dict[str, Any]:
    """Eine Operation der öffentlichen Beschreibung für die nichtöffentliche Ausgabe umschreiben."""
    ok = dict(operation["responses"]["200"])
    headers = {key: value for key, value in ok.get("headers", {}).items() if key != "ETag"}
    ok["headers"] = {**headers, **_PRIVATE_HEADERS}
    responses: dict[str, Any] = {"200": ok}
    if "400" in operation["responses"]:
        responses["400"] = _nonpublic_problem("Ungültiger Zeitfilter oder ungültige Seitennummer")
    responses["401"] = _nonpublic_problem("Kein oder kein gültiges Zugriffstoken", authenticate=True)
    responses["403"] = _nonpublic_problem(
        "Scope fehlt, Dienst an einen anderen Mandanten gebunden oder kein Konto im Mandanten", authenticate=True
    )
    responses["404"] = _nonpublic_problem("Gibt es nicht oder darf der Aufrufer nicht sehen")
    responses["429"] = _nonpublic_problem("Zu viele Anfragen")
    responses["503"] = _nonpublic_problem("Der Abruf konnte nicht protokolliert werden; ohne Eintrag keine Inhalte")
    result = {
        **operation,
        "responses": responses,
        "security": [
            {NONPUBLIC_SECURITY: [catalog.RATSDATEN_NICHTOEFFENTLICH]},
            {NONPUBLIC_SECURITY: [catalog.SITZUNG_LESEN]},
        ],
    }
    result["description"] = (
        f"Nichtöffentliche Ausgabe nach dem Profil {kommparl.PROFILE_NONPUBLIC}: alle Objekte, die der Aufrufer sehen "
        "darf; nichtöffentliche tragen kommparl:nonPublic, auch öffentliche kommparl:publicId. Jeder Abruf mit "
        "Nichtöffentlichem steht im Prüfprotokoll des Mandanten."
    )
    return result


def tenant_nonpublic_description() -> Description:
    """Pfade der nichtöffentlichen Ausgabe je Mandant (Profil Nichtöffentliches, Issue #565)."""
    from apps.session.api import nichtoeffentlich

    prefix = _path("oparl_noe_system")
    marker = "00000000-0000-0000-0000-000000000565"

    def relative(path: str) -> str:
        return path.removeprefix(prefix).replace(marker, "{id}")

    object_path = relative(_path("oparl_noe_object", kind="KIND", pk=marker)).replace("KIND", "{kind}")
    paths = oparl_paths(
        prefix=prefix,
        key="oparl_mandant_noe",
        system="",
        bodies=relative(_path("oparl_noe_bodies")),
        body=relative(_path("oparl_noe_body")),
        lists={
            relative(_path("oparl_noe_list", segment=segment)): spec[3]
            for segment, spec in nichtoeffentlich.LIST_SPECS.items()
        },
        objects=[kind for kind in [*nichtoeffentlich.OBJECT_SPECS, "location"] if kind in KINDS],
        object_path=object_path,
        feed=None,
        snapshot=None,
        parameters=[_TENANT_PARAMETER],
    )
    for item in paths.values():
        for method in METHODS_GET:
            if method in item:
                item[method] = _nonpublic_operation(item[method])
    download = _path("oparl_noe_file_download", pk=marker).replace(marker, "{id}")
    paths[download] = {
        "get": _nonpublic_operation(
            {
                "operationId": "oparl_mandant_noe_file_download",
                "summary": "Datei abrufen (nichtöffentlich)",
                "parameters": [
                    _TENANT_PARAMETER,
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "description": "Kennung der Datei",
                        "schema": {"type": "string", "format": "uuid"},
                    },
                    {
                        "name": "download",
                        "in": "query",
                        "required": False,
                        "description": "Gesetzt: immer als Download",
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {"200": {"description": "Inhalt der Datei", "content": {"*/*": {"schema": {}}}}},
            }
        )
    }
    metadata = reverse("oparl_noe_metadata", kwargs={"tenant_slug": _TENANT}).replace(_TENANT, "{tenant_slug}")
    paths[metadata] = {
        "get": {
            "operationId": "oparl_mandant_noe_metadata",
            "summary": "Metadaten der geschützten Ressource (RFC 9728)",
            "description": (
                "Ohne Anmeldung: Kennung der nichtöffentlichen Ausgabe (resource), Aussteller der Tokens, Scopes und "
                "bearer_methods_supported; die Adresse nennt jede Antwort 401 in WWW-Authenticate (resource_metadata)."
            ),
            "parameters": [_TENANT_PARAMETER],
            "responses": {
                "200": {
                    "description": "Metadaten",
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/"
                                + kommparl.profile_component_name("ProtectedResourceMetadata")
                            }
                        }
                    },
                },
                "404": _nonpublic_problem("Nicht eingeschaltet oder Mandant unbekannt"),
            },
        }
    }
    return {
        "paths": paths,
        "components": {"schemas": component_schemas(), "securitySchemes": {NONPUBLIC_SECURITY: _NONPUBLIC_SCHEME}},
    }


def session_v1_description() -> Description:
    """Beschreibung der Session-API v1, wie django-ninja sie erzeugt, mit den Befehlen der Endpunkte."""
    from apps.session.api.v1.api import api

    document: dict[str, Any] = copy.deepcopy(dict(api.get_openapi_schema()))
    found: set[str] = set()
    for item in document.get("paths", {}).values():
        for operation in item.values():
            command = SESSION_V1_COMMANDS.get(operation.get("operationId", "")) if isinstance(operation, dict) else None
            if command:
                operation[COMMAND_EXTENSION] = command
                found.add(operation["operationId"])
    if missing := sorted(set(SESSION_V1_COMMANDS) - found):
        raise ImproperlyConfigured(f"Session-API v1 hat keine Endpunkte {missing} (SESSION_V1_COMMANDS anpassen)")
    return document


TENANT_OPARL: Final = Area(
    key="oparl-mandant",
    title="OParl je Mandant",
    description=(
        "OParl 1.1 je Session-Mandant, sobald die Verwaltung die Schnittstelle freigeschaltet hat: anonym, lesend, "
        "nur Öffentliches; Änderungsfeed und Snapshot, wenn die Installation sie anbietet."
    ),
    entry="/session/{tenant_slug}/api/oparl/",
    openapi=tenant_oparl_description,
    order=20,
)

TENANT_NONPUBLIC: Final = Area(
    key="oparl-mandant-nichtoeffentlich",
    title="OParl je Mandant, nichtöffentlich",
    description=(
        "Nichtöffentliche Ausgabe je Session-Mandant nach dem Profil Nichtöffentliches von kommParl: Personen mit "
        "Scope ratsdaten:nichtoeffentlich lesen, was sie selbst sehen dürfen, gebundene Dienste mit sitzung:lesen "
        "alles ihres Mandanten. Je Installation einzuschalten."
    ),
    entry="/session/{tenant_slug}/api/oparl/nichtoeffentlich/",
    openapi=tenant_nonpublic_description,
    # Direkt nach der öffentlichen Ausgabe, vor dem Profil Einreichung (25)
    order=22,
)

SESSION_V1: Final = Area(
    key="session-v1",
    title="Session-API v1",
    description=(
        "Session-RIS je Mandant mit Anmeldung: Sitzungen, Vorlagen, Anträge und Einreichung (API-Token oder "
        "Sitzung); Nichtöffentliches nur mit Recht."
    ),
    entry="/api/v1/session/{tenant_slug}/",
    openapi=session_v1_description,
    order=30,
)


def register() -> None:
    """Bereiche von Session anmelden (aus ``SessionConfig.ready``)."""
    from .einreichung_beschreibung import EINREICHUNG

    register_area(TENANT_OPARL)
    register_area(TENANT_NONPUBLIC)
    register_area(EINREICHUNG)
    register_area(SESSION_V1)

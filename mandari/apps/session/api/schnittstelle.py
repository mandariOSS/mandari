# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bereiche von Session in der gemeinsamen Schnittstelle (``docs/adr/20261105-eine-schnittstelle-api-first.md``).

Die Drehscheibe kennt Session nicht (A1); Session meldet seine Bereiche deshalb beim Start an
(``SessionConfig.ready``):

- **OParl je Mandant** (``/session/<mandant>/api/oparl/``): Pfade aus den Routen und Listen von
  ``apps/session/api/oparl.py``, beschrieben mit den OParl-Bausteinen der Drehscheibe
  (``hub.api.openapi.oparl``). Die Endpunkte selbst bleiben unverändert.
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

from hub.api.openapi import COMMAND_EXTENSION, Area, Description, register_area
from hub.api.openapi.oparl import KINDS, component_schemas, oparl_paths

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
    register_area(EINREICHUNG)
    register_area(SESSION_V1)

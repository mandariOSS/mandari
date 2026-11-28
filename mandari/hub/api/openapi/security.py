# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anmeldung und Scopes in der gemeinsamen Beschreibung (Issue #1591, ADR ``20261105-eine-schnittstelle-api-first``).

- :func:`scheme`: ein Sicherheitsschema ``mandariOidc`` für den OpenID-Connect-Anbieter der Plattform. Es nennt alle
  Scopes des Katalogs (``apps.accounts.oidc.catalog``), die Personen (Code-Ablauf) bzw. Dienste (Client Credentials)
  erhalten können, außer denen des Profils Nichtöffentliches (eigenes Schema der nichtöffentlichen Ausgabe). Jeder
  Bereich, der Access-Tokens annimmt, verweist auf dieses eine Schema (:func:`components`), damit es in der
  Gesamtbeschreibung überall gleich ist.
- :func:`requirement`: Scopes einer Operation. Jeder genannte Scope genügt für sich (in OpenAPI sind mehrere
  Anforderungen Alternativen); die Ressource prüft zur Laufzeit dieselben Scopes.
- :func:`scope_problems`: Prüfung einer Beschreibung, aufgerufen von ``hub/api/tests/test_schnittstelle.py``. Jede
  Operation mit Anmeldung nennt OAuth-Scopes aus dem Katalog, die ihr Schema kennt; eine Operation, die einen Befehl
  annimmt (``x-mandari-command``), verlangt in jeder OAuth-Anforderung einen Scope zum Schreiben
  (``catalog.WRITES``). Operationen mit Anmeldung ohne OAuth (nur API-Token der Session-API v1) stehen in
  ``operationen_ohne_scope.json``; die Liste darf nur kürzer werden (``scripts/check_schnittstelle.py``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from apps.accounts.oidc import catalog

from .build import COMMAND_EXTENSION, operations

#: Name des Sicherheitsschemas des OpenID-Connect-Anbieters
SCHEME: Final = "mandariOidc"

_DESCRIPTION: Final = (
    "OpenID-Connect-Anbieter der Installation (Discovery unter /.well-known/openid-configuration). Access-Token im "
    "Header Authorization: Bearer. Im Namen einer Person: Code-Ablauf mit PKCE (S256 Pflicht); das Token darf nie "
    "mehr als die Person selbst. Dienste: Client Credentials. Welche Scopes eine Operation verlangt, steht an ihr; "
    "jeder dort genannte Scope genügt. Über die Grenze der Anwendung je Minute hinaus antwortet die Schnittstelle mit "
    "429."
)


def _listed(scope: catalog.Scope) -> bool:
    return not scope.protected and not scope.bound


def scheme() -> dict[str, Any]:
    """Das Sicherheitsschema ``mandariOidc`` mit den Scopes des Katalogs je Ablauf."""
    return {
        "type": "oauth2",
        "description": _DESCRIPTION,
        "flows": {
            "authorizationCode": {
                "authorizationUrl": "/oauth/authorize/",
                "tokenUrl": "/oauth/token/",
                "refreshUrl": "/oauth/token/",
                "scopes": {
                    scope.name: scope.description for scope in catalog.CATALOG if scope.person and _listed(scope)
                },
            },
            "clientCredentials": {
                "tokenUrl": "/oauth/token/",
                "scopes": {
                    scope.name: scope.description for scope in catalog.CATALOG if scope.service and _listed(scope)
                },
            },
        },
    }


def components() -> dict[str, Any]:
    """``components`` mit dem Sicherheitsschema, für die Beschreibung eines Bereichs."""
    return {"securitySchemes": {SCHEME: scheme()}}


def requirement(*scopes: str) -> list[dict[str, list[str]]]:
    """Anforderungen einer Operation: jeder Scope für sich genügt."""
    unknown = [scope for scope in scopes if catalog.get(scope) is None]
    if unknown:
        raise ValueError(f"Unbekannte Scopes: {', '.join(unknown)}")
    return [{SCHEME: [scope]} for scope in scopes]


def _flow_scopes(definition: Mapping[str, Any]) -> set[str]:
    return {str(name) for flow in definition.get("flows", {}).values() for name in flow.get("scopes", {})}


def scope_problems(document: Mapping[str, Any], exempt: Iterable[str]) -> list[str]:
    """Verstöße gegen die Regeln für Anmeldung und Scopes; leer, wenn alles passt."""
    problems: list[str] = []
    schemes: Mapping[str, Any] = document.get("components", {}).get("securitySchemes", {})
    oauth: dict[str, set[str]] = {
        name: _flow_scopes(definition) for name, definition in schemes.items() if definition.get("type") == "oauth2"
    }
    for name, scopes in sorted(oauth.items()):
        for scope in sorted(scopes):
            if catalog.get(scope) is None:
                problems.append(f"Sicherheitsschema {name} nennt {scope}, den es im Katalog nicht gibt.")
    listed = list(exempt)
    without_oauth: set[str] = set()
    seen: set[str] = set()
    for method, path, operation in operations(document):
        label = f"{method.upper()} {path}"
        operation_id = str(operation.get("operationId", ""))
        seen.add(operation_id)
        requirements = operation.get("security")
        if not requirements:
            continue
        oauth_requirements = [item for item in requirements if any(key in oauth for key in item)]
        if not oauth_requirements:
            without_oauth.add(operation_id)
            if operation_id not in listed:
                problems.append(
                    f"{label} verlangt eine Anmeldung, nennt aber keine OAuth-Scopes. Scopes angeben; neue Ausnahmen "
                    "sind nicht erlaubt."
                )
            continue
        for item in oauth_requirements:
            for key, scopes in item.items():
                if key not in oauth:
                    continue
                if not scopes:
                    problems.append(f"{label}: Anforderung mit {key} ohne Scope.")
                for scope in scopes:
                    if scope not in oauth[key]:
                        problems.append(f"{label}: Scope {scope} steht nicht im Sicherheitsschema {key}.")
                if operation.get(COMMAND_EXTENSION) and not set(scopes) & catalog.WRITES:
                    problems.append(
                        f"{label} nimmt den Befehl {operation[COMMAND_EXTENSION]} an, verlangt aber keinen Scope zum "
                        f"Schreiben ({', '.join(sorted(catalog.WRITES))})."
                    )
    if listed != sorted(set(listed)):
        problems.append("operationen_ohne_scope.json: sortiert und ohne Doppelte")
    for operation_id in sorted(set(listed) - without_oauth):
        reason = "gibt es nicht mehr" if operation_id not in seen else "nennt jetzt OAuth-Scopes oder keine Anmeldung"
        problems.append(f"operationen_ohne_scope.json: {operation_id} {reason}, bitte streichen.")
    return problems

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Scopes an den Operationen der gemeinsamen Schnittstelle (Issue #1591, ``hub/api/openapi/security.py``).

- Die eingecheckte Beschreibung hält die Regeln ein: Jede Operation mit Anmeldung nennt OAuth-Scopes aus dem Katalog,
  Operationen mit Befehl verlangen einen Scope zum Schreiben, Ausnahmen stehen in ``operationen_ohne_scope.json``.
- Die Prüfung erkennt Verstöße: unbekannter Scope, Befehl ohne Scope zum Schreiben, Anmeldung ohne Scopes, veraltete
  Ausnahme.
- Die Session-API v1 nennt in der Beschreibung genau die Scopes, die sie zur Laufzeit prüft (``auth.SCOPES``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.accounts.oidc import catalog
from apps.session.api.schnittstelle import SESSION_V1_OPERATION_PREFIX, session_v1_description
from apps.session.api.v1 import auth as session_auth
from hub.api.entry import description
from hub.api.openapi import operations
from hub.api.openapi import security as oidc_security

EXEMPT = Path(__file__).resolve().parent.parent / "openapi" / "operationen_ohne_scope.json"


def _ausnahmen() -> list[str]:
    return list(json.loads(EXEMPT.read_text(encoding="utf-8"))["operationen"])


def _dokument(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "paths": {"/x/": {"post": {"operationId": "x", "responses": {}, **operation}}},
        "components": {"securitySchemes": {oidc_security.SCHEME: oidc_security.scheme()}},
    }


class TestEingecheckteBeschreibung:
    def test_haelt_die_regeln_ein(self) -> None:
        assert oidc_security.scope_problems(description(), _ausnahmen()) == []

    def test_schema_nennt_scopes_fuer_session_und_work(self) -> None:
        schema = oidc_security.scheme()
        person = schema["flows"]["authorizationCode"]["scopes"]
        for scope in (catalog.SESSION_LESEN, catalog.WORK_LESEN, catalog.WORK_SCHREIBEN):
            assert scope in person
            assert scope not in schema["flows"]["clientCredentials"]["scopes"]
        # Nichtöffentliches steht nur im Schema der nichtöffentlichen Ausgabe
        assert catalog.RATSDATEN_NICHTOEFFENTLICH not in person

    def test_session_api_v1_nennt_die_scopes_der_laufzeit(self) -> None:
        beschreibung = session_v1_description()
        gefunden: dict[str, set[str]] = {}
        for _method, _path, operation in operations(beschreibung):
            name = str(operation["operationId"]).removeprefix(SESSION_V1_OPERATION_PREFIX)
            gefunden[name] = {
                scope
                for anforderung in operation.get("security", [])
                for scope in anforderung.get(oidc_security.SCHEME, [])
            }
        for name, scopes in session_auth.SCOPES.items():
            assert gefunden[name] == set(scopes), name

    def test_scopes_ohne_endpunkt_fallen_auf(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(session_auth.SCOPES, "gibt_es_nicht", (catalog.SESSION_LESEN,))
        with pytest.raises(ImproperlyConfigured, match="gibt_es_nicht"):
            session_v1_description()


class TestPruefung:
    def test_unbekannter_scope(self) -> None:
        dokument = _dokument({"security": [{oidc_security.SCHEME: ["gibt:es-nicht"]}]})
        assert any("gibt:es-nicht" in problem for problem in oidc_security.scope_problems(dokument, []))

    def test_befehl_braucht_scope_zum_schreiben(self) -> None:
        dokument = _dokument(
            {"x-mandari-command": "submission.submit", "security": oidc_security.requirement(catalog.SESSION_LESEN)}
        )
        assert any("zum Schreiben" in problem for problem in oidc_security.scope_problems(dokument, []))
        dokument = _dokument(
            {"x-mandari-command": "submission.submit", "security": oidc_security.requirement(catalog.WORK_SCHREIBEN)}
        )
        assert oidc_security.scope_problems(dokument, []) == []

    def test_anmeldung_ohne_scopes_nur_als_ausnahme(self) -> None:
        dokument = _dokument({"security": [{"BearerOrSession": []}]})
        dokument["components"]["securitySchemes"]["BearerOrSession"] = {"type": "http", "scheme": "bearer"}
        assert any("keine OAuth-Scopes" in problem for problem in oidc_security.scope_problems(dokument, []))
        assert oidc_security.scope_problems(dokument, ["x"]) == []

    def test_veraltete_ausnahme(self) -> None:
        dokument = _dokument({"security": oidc_security.requirement(catalog.RATSDATEN_LESEN)})
        assert any("bitte streichen" in problem for problem in oidc_security.scope_problems(dokument, ["x"]))
        assert any("bitte streichen" in problem for problem in oidc_security.scope_problems(dokument, ["weg"]))

    def test_ohne_anmeldung_keine_regel(self) -> None:
        assert oidc_security.scope_problems(_dokument({}), []) == []

    def test_anforderung_nur_mit_bekannten_scopes(self) -> None:
        with pytest.raises(ValueError, match="gibt:es-nicht"):
            oidc_security.requirement("gibt:es-nicht")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session-API v1 auf den gemeinsamen Bausteinen (Issue #1205): Die Fehlerantworten bleiben, wie sie waren.

Seit #1205 kommen Format und Handler aus ``apps.common.api``. Geprüft werden Felder, Reihenfolge, Typ, Titel,
Meldung und Header der Probleme, wie die API v1 sie seit #163 ausliefert.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.test import Client, RequestFactory
from django.utils import timezone
from ninja.errors import HttpError

from apps.session.api.v1.api import api as session_api_v1
from apps.session.api.v1.endpoints import command_problem
from apps.session.models import SessionTenant
from hub.commands import CommandError

pytestmark = pytest.mark.django_db

BASE = "/api/v1/session"
TYPE = "https://docs.mandari.de/api/probleme/"


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now())


def _problem(response: Any, status: int) -> dict[str, Any]:
    assert response.status_code == status
    assert response["Content-Type"] == "application/problem+json; charset=utf-8"
    body: dict[str, Any] = json.loads(response.content)
    return body


def test_unbekannter_mandant(client: Client) -> None:
    body = _problem(client.get(f"{BASE}/gibt-es-nicht/?x=1"), 404)
    assert list(body) == ["type", "title", "status", "detail", "instance", "request_id"]
    assert body["type"] == f"{TYPE}nicht-gefunden"
    assert body["title"] == "Nicht gefunden"
    assert body["detail"] == "Mandant „gibt-es-nicht“ nicht gefunden."
    # instance mit Query-String wie bisher
    assert body["instance"] == f"{BASE}/gibt-es-nicht/?x=1"


def test_ungueltige_anfrage(client: Client, tenant: SessionTenant) -> None:
    body = _problem(client.get(f"{BASE}/{tenant.slug}/meetings/?limit=0"), 422)
    assert list(body) == ["type", "title", "status", "detail", "instance", "request_id", "errors"]
    assert body["type"] == f"{TYPE}validierung"
    assert body["title"] == "Validierung fehlgeschlagen"
    assert body["detail"] == "Die Anfrage enthält ungültige oder fehlende Felder."
    assert body["errors"][0]["loc"] == ["query", "limit"]


def test_ungueltiges_token(client: Client, tenant: SessionTenant) -> None:
    response = client.get(f"{BASE}/{tenant.slug}/meetings/", headers={"Authorization": "Bearer zu-kurz"})
    body = _problem(response, 401)
    assert body["type"] == f"{TYPE}nicht-authentifiziert"
    assert body["detail"] == "Das API-Token hat ein ungültiges Format."
    assert "WWW-Authenticate" not in response


def test_anfrage_ohne_recht(client: Client, tenant: SessionTenant) -> None:
    body = _problem(client.get(f"{BASE}/{tenant.slug}/applications/"), 401)
    assert body["title"] == "Nicht authentifiziert"
    assert body["type"] == f"{TYPE}nicht-authentifiziert"


def test_ungueltiger_inhalt_beim_einreichen(client: Client, tenant: SessionTenant) -> None:
    response = client.post(f"{BASE}/{tenant.slug}/applications/submit/", data={}, content_type="application/json")
    body = _problem(response, 422)
    assert body["instance"] == f"{BASE}/{tenant.slug}/applications/submit/"
    assert {tuple(error["loc"]) for error in body["errors"]} >= {("body", "payload", "title")}


@pytest.mark.parametrize(
    ("status", "titel"),
    [(404, "Nicht gefunden"), (410, "Fehler"), (413, "Fehler"), (501, "Fehler"), (503, "Fehler"), (504, "Fehler")],
)
def test_titel_wie_bisher(status: int, titel: str) -> None:
    """
    Die API v1 behält ihre Titel aus #163, auch für Status, die die gemeinsame Tabelle kennt. Der Befehl
    ``submission.submit`` meldet etwa 503 (Anlagen nicht abgelegt) und 501 (Befehl nicht verfügbar).
    """
    request = RequestFactory().post(f"{BASE}/musterstadt/applications/submit/")
    fehler = command_problem(CommandError.of(status, "beispiel", "Meldung des Befehls."))
    body = _problem(session_api_v1.on_exception(request, fehler), status)
    assert list(body) == ["type", "title", "status", "detail", "instance", "request_id"]
    assert body["title"] == titel
    assert body["type"] == f"{TYPE}beispiel"
    assert body["detail"] == "Meldung des Befehls."


def test_titel_ohne_art() -> None:
    body = _problem(session_api_v1.on_exception(RequestFactory().get(f"{BASE}/x/"), HttpError(503, "Weg.")), 503)
    assert body["type"] == "about:blank"
    assert body["title"] == "Fehler"


def test_eigene_beschreibung_bleibt(client: Client) -> None:
    body = client.get(f"{BASE}/openapi.json").json()
    assert body["info"]["title"] == "mandari Session-API"
    submit = body["paths"][f"{BASE}/{{tenant_slug}}/applications/submit/"]["post"]
    # Der Befehl steht nur in der gemeinsamen Beschreibung (/api/openapi.json), nicht in der eigenen
    assert not any(key.startswith("x-") for key in submit)

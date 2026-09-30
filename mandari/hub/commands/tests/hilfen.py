# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bausteine für Tests der Befehle: Register mit Testverträgen, Anmeldung und HTTP-Transport."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.http import HttpRequest
from django.test import Client

from hub.commands import Principal
from hub.contracts import Registry, load_registry
from hub.contracts.tests.hilfen import ablegen, befehl_schema, ereignis_schema

TENANT = "session:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
ACTOR = "system:work"
TOKEN = "test-token-fuer-befehle"
TOKEN_NUR_ABSAGEN = "test-token-nur-absagen"
DOCUMENT = "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"
BASE_URL = "http://testserver/befehle"
#: Eigentümer der Testverträge: die Handler liegen in den Testmodulen dieses Pakets.
OWNER = "hub.commands.tests"


def register_mit_testvertraegen(wurzel: Path) -> Registry:
    """Register mit zwei Befehlen und einem Ereignis, Eigentümer ``hub.commands.tests``."""
    einreichen = befehl_schema(
        "submission.submit",
        **{
            "x-owner": OWNER,
            "x-visibility": "intern",
            "required": ["document", "title"],
            "properties": {
                "document": {"type": "string", "format": "uuid"},
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "urgent": {"type": "boolean"},
            },
            "examples": [{"document": DOCUMENT, "title": "Mehr Bänke im Park"}],
        },
    )
    ablegen(wurzel, "submission.submit", 1, einreichen)
    zuruecknehmen = befehl_schema(
        "submission.withdraw",
        **{
            "x-owner": OWNER,
            "required": ["document"],
            "properties": {"document": {"type": "string", "format": "uuid"}},
            "examples": [{"document": DOCUMENT}],
        },
    )
    ablegen(wurzel, "submission.withdraw", 1, zuruecknehmen)
    ablegen(wurzel, "ris.paper.released", 1, ereignis_schema(**{"x-owner": OWNER}))
    return load_registry(wurzel)


def anmelden(request: HttpRequest) -> Principal | None:
    """Testanmeldung: ein Token für alle Befehle, eines nur für ``submission.withdraw``."""
    authorization = request.headers.get("Authorization", "")
    if authorization == f"Bearer {TOKEN}":
        return Principal(tenant_ref=TENANT, actor_ref=ACTOR, commands={"*"})
    if authorization == f"Bearer {TOKEN_NUR_ABSAGEN}":
        return Principal(tenant_ref=TENANT, actor_ref=ACTOR, commands={"submission.withdraw"})
    return None


class DjangoTransport(httpx.BaseTransport):
    """httpx-Transport, der Anfragen über den Django-Testclient stellt (URL-Auflösung, Middleware, View)."""

    def __init__(self, client: Client | None = None) -> None:
        self.client = client or Client()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in {"host", "content-length", "content-type"}
        }
        response = self.client.generic(
            request.method,
            request.url.raw_path.decode("ascii"),
            data=request.read(),
            content_type=request.headers.get("content-type", ""),
            headers=headers,
        )
        return httpx.Response(
            status_code=response.status_code,
            headers=[(name, value) for name, value in response.items()],
            content=response.content,
        )


def json_body(**felder: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"document": DOCUMENT, "title": "Mehr Bänke im Park"}
    body.update(felder)
    return body


def protokolltext(caplog: pytest.LogCaptureFixture) -> str:
    """
    Alles, was ein Log-Handler ausgeben kann: Meldung, Text der Ausnahme und jedes Feld des Eintrags
    (der JSON-Formatter schreibt auch ``extra``-Felder, ``caplog.text`` zeigt sie nicht).
    """
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    teile = [caplog.text]
    for record in caplog.records:
        teile.append(formatter.format(record))
        teile.extend(repr(wert) for wert in vars(record).values())
    return "\n".join(teile)

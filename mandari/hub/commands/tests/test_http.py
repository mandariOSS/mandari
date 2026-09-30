# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP-Weg und ``HttpClient`` (Issue #539): Anmeldung, Header, Fehlerformat, Ausfall der Gegenseite."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.test import Client

from hub.commands import Command, CommandError, Dispatcher, HandlerResult, HttpClient
from hub.commands import dispatcher as dispatcher_modul
from hub.commands.clients import BAD_RESPONSE, UNREACHABLE
from hub.commands.tests.hilfen import (
    ACTOR,
    BASE_URL,
    TENANT,
    TOKEN,
    TOKEN_NUR_ABSAGEN,
    json_body,
    register_mit_testvertraegen,
)

pytestmark = [pytest.mark.django_db, pytest.mark.urls("hub.commands.tests.urls")]

URL = "/befehle/submission.submit/v1"
BEFEHLE: list[Command] = []


def einreichen(command: Command) -> HandlerResult:
    BEFEHLE.append(command)
    return HandlerResult(reference=f"A/2026/{len(BEFEHLE)}")


@pytest.fixture
def dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dispatcher:
    BEFEHLE.clear()
    dispatcher = Dispatcher(register_mit_testvertraegen(tmp_path))
    dispatcher.register("submission.submit", 1, einreichen)
    monkeypatch.setattr(dispatcher_modul, "_DEFAULT", dispatcher)
    return dispatcher


def _post(url: str = URL, body: Any = None, *, token: str | None = TOKEN, **headers: str) -> Any:
    kopf = {"Idempotency-Key": '"schluessel-1"', **headers}
    if token is not None:
        kopf["Authorization"] = f"Bearer {token}"
    daten = json.dumps(json_body() if body is None else body)
    return Client().post(url, data=daten, content_type="application/json", headers=kopf)


def _problem(response: Any, status: int, kind: str) -> dict[str, Any]:
    assert response.status_code == status, response.content
    assert response["Content-Type"] == "application/problem+json; charset=utf-8"
    daten = json.loads(response.content)
    assert daten["type"] == f"https://docs.mandari.de/api/probleme/{kind}"
    assert daten["status"] == status
    assert daten["instance"] == response.wsgi_request.path
    assert "request_id" in daten
    return dict(daten)


def test_erfolg_ist_201_mit_quittung(dispatcher: Dispatcher) -> None:
    response = _post()
    assert response.status_code == 201
    assert response["Content-Type"] == "application/json"
    assert response["Cache-Control"] == "no-store"
    daten = json.loads(response.content)
    assert daten["reference"] == "A/2026/1"
    assert set(daten) == {"command", "version", "reference", "received_at", "content_hash", "aggregate_id", "data"}


def test_mandant_und_ausloeser_stammen_aus_der_anmeldung(dispatcher: Dispatcher) -> None:
    korrelation = uuid.uuid4()
    _post(**{"Mandari-Tenant": TENANT, "X-Correlation-ID": str(korrelation)})
    befehl = BEFEHLE[0]
    assert (befehl.tenant_ref, befehl.actor_ref, befehl.correlation_id) == (TENANT, ACTOR, korrelation)
    assert befehl.idempotency_key == "schluessel-1"


@pytest.mark.parametrize("kopf", ["schluessel-1", '"schluessel-1"', ' "schluessel-1" '])
def test_schluessel_mit_und_ohne_anfuehrungszeichen(dispatcher: Dispatcher, kopf: str) -> None:
    assert _post(**{"Idempotency-Key": kopf}).status_code == 201
    assert _post(**{"Idempotency-Key": "schluessel-1"}).status_code == 201
    assert len(BEFEHLE) == 1


def test_ohne_anmeldung_401(dispatcher: Dispatcher) -> None:
    for token in (None, "falsch"):
        response = _post(token=token)
        _problem(response, 401, "nicht-authentifiziert")
        assert response["WWW-Authenticate"] == 'Bearer realm="mandari"'
    assert BEFEHLE == []


def test_ohne_schluessel_400(dispatcher: Dispatcher) -> None:
    response = Client().post(
        URL, data=json.dumps(json_body()), content_type="application/json", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    _problem(response, 400, "idempotenzschluessel-fehlt")
    _problem(_post(**{"Idempotency-Key": '""'}), 400, "idempotenzschluessel-fehlt")


def test_ohne_berechtigung_fuer_den_befehl_403(dispatcher: Dispatcher) -> None:
    _problem(_post(token=TOKEN_NUR_ABSAGEN), 403, "keine-berechtigung")
    assert BEFEHLE == []


def test_fremder_mandant_403(dispatcher: Dispatcher) -> None:
    _problem(_post(**{"Mandari-Tenant": f"session:{uuid.uuid4()}"}), 403, "keine-berechtigung")


@pytest.mark.parametrize(("daten", "kind"), [("{kaputt", "ungueltiges-json"), ("[1, 2]", "ungueltiges-json")])
def test_kein_json_objekt_400(dispatcher: Dispatcher, daten: str, kind: str) -> None:
    response = Client().post(
        URL,
        data=daten,
        content_type="application/json",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "k"},
    )
    _problem(response, 400, kind)


def test_einzelnes_surrogat_im_json_ergibt_422(dispatcher: Dispatcher) -> None:
    """In JSON schreibbar (Escape), aber kein gültiges Unicode: 422 statt 500, der Handler läuft nicht."""
    rohtext = json.dumps(json_body(title="a\ud800b"))
    assert "\\ud800" in rohtext
    response = Client().post(
        URL,
        data=rohtext,
        content_type="application/json",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "k"},
    )
    daten = _problem(response, 422, "validierung")
    assert daten["errors"][0]["pointer"] == ""
    assert BEFEHLE == []


def test_zu_tief_verschachteltes_json_ergibt_400(dispatcher: Dispatcher) -> None:
    tief = "[" * 100_000 + "]" * 100_000
    response = Client().post(
        URL,
        data='{"title": ' + tief + "}",
        content_type="application/json",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "k"},
    )
    _problem(response, 400, "ungueltiges-json")


def test_anderes_format_415(dispatcher: Dispatcher) -> None:
    response = Client().post(
        URL,
        data="title=x",
        content_type="text/plain",
        headers={"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": "k"},
    )
    _problem(response, 415, "format-nicht-unterstuetzt")


def test_nur_post_405(dispatcher: Dispatcher) -> None:
    response = Client().get(URL, headers={"Authorization": f"Bearer {TOKEN}"})
    _problem(response, 405, "methode-nicht-erlaubt")
    assert response["Allow"] == "POST"


def test_validierungsfehler_mit_json_pointer(dispatcher: Dispatcher) -> None:
    daten = _problem(_post(body={"document": "keine-uuid"}), 422, "validierung")
    assert daten["errors"] == [
        {"pointer": "/document", "detail": "verletzt „format“"},
        {"pointer": "", "detail": "Pflichtfeld fehlt (title)"},
    ]
    assert "keine-uuid" not in json.dumps(daten)


# --- HttpClient: Gegenseite nicht erreichbar oder unerwartete Antwort -----------------------------


def _befehl() -> Command:
    return Command(name="submission.submit", body=json_body(), idempotency_key="k-1", tenant_ref=TENANT)


def test_nicht_erreichbar_ergibt_503_zum_wiederholen() -> None:
    def verbindung_scheitert(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("keine Verbindung", request=request)

    client = HttpClient(BASE_URL, TOKEN, transport=httpx.MockTransport(verbindung_scheitert))
    with pytest.raises(CommandError) as info:
        client.send(_befehl())
    problem = info.value.problem
    assert (problem.status, problem.kind, problem.detail, problem.retryable) == (
        503,
        "eigentuemer-nicht-erreichbar",
        UNREACHABLE,
        True,
    )


@pytest.mark.parametrize(
    ("status", "inhalt"),
    [(201, b"kein json"), (201, b'{"unvollstaendig": true}'), (500, b"<html>Fehler</html>"), (302, b"")],
)
def test_unerwartete_antwort_ergibt_502(status: int, inhalt: bytes) -> None:
    client = HttpClient(
        BASE_URL, TOKEN, transport=httpx.MockTransport(lambda request: httpx.Response(status, content=inhalt))
    )
    with pytest.raises(CommandError) as info:
        client.send(_befehl())
    assert (info.value.problem.status, info.value.problem.detail) == (502, BAD_RESPONSE)


@pytest.mark.parametrize("angabe", ["kaputt", 200, None, [409]])
def test_status_der_antwort_gilt_nicht_die_angabe_im_inhalt(angabe: object) -> None:
    """Ein nicht numerischer oder abweichender ``status`` im Problem ändert nichts am HTTP-Status."""

    def antworten(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"type": "https://docs.mandari.de/api/probleme/konflikt", "status": angabe})

    with pytest.raises(CommandError) as info:
        HttpClient(BASE_URL, TOKEN, transport=httpx.MockTransport(antworten)).send(_befehl())
    assert (info.value.problem.status, info.value.problem.kind) == (409, "konflikt")


def test_client_sendet_keinen_inhalt_ohne_kanonische_darstellung() -> None:
    gesehen: list[httpx.Request] = []

    def antworten(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        return httpx.Response(500)

    befehl = Command(
        name="submission.submit", body=json_body(title="a\ud800b"), idempotency_key="k-1", tenant_ref=TENANT
    )
    with pytest.raises(CommandError) as info:
        HttpClient(BASE_URL, TOKEN, transport=httpx.MockTransport(antworten)).send(befehl)
    assert (info.value.problem.status, info.value.problem.kind) == (422, "validierung")
    assert gesehen == []


def test_client_sendet_schluessel_mandant_und_korrelation() -> None:
    gesehen: list[httpx.Request] = []

    def antworten(request: httpx.Request) -> httpx.Response:
        gesehen.append(request)
        return httpx.Response(
            404, json={"type": "https://docs.mandari.de/api/probleme/befehl-unbekannt", "status": 404}
        )

    befehl = _befehl()
    with pytest.raises(CommandError) as info:
        HttpClient(f"{BASE_URL}/", TOKEN, transport=httpx.MockTransport(antworten)).send(befehl)
    assert info.value.problem.kind == "befehl-unbekannt"
    anfrage = gesehen[0]
    assert str(anfrage.url) == f"{BASE_URL}/submission.submit/v1"
    assert anfrage.headers["Idempotency-Key"] == '"k-1"'
    assert anfrage.headers["Mandari-Tenant"] == TENANT
    assert anfrage.headers["X-Correlation-ID"] == str(befehl.correlation_id)
    assert anfrage.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(anfrage.content) == json_body()

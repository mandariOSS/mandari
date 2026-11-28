# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehle als Operationen der gemeinsamen Schnittstelle (Issue #1588, ``hub.api.befehle``).

Mit einem Testvertrag und einem Testhandler: Freigabe, Anmeldung (Browser-Sitzung mit CSRF, Access-Token im Namen
einer Person mit Scope), Mandant aus der Adresse, Idempotenz, Ratenbegrenzung, Fehler nach RFC 9457 und die
Beschreibung. Dazu die Parität: Derselbe Befehl aus der Oberfläche (``ausfuehren``) und über HTTP erreicht denselben
Handler. Am Ende dieselben Zusagen für jeden Befehl, den die Installation tatsächlich freigibt.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from django.conf import settings
from django.core.cache import cache
from django.middleware.csrf import _get_new_csrf_string
from django.test import Client, RequestFactory

from apps.accounts.models import User
from apps.accounts.oidc import catalog
from apps.accounts.oidc.models import OAuthApplication
from apps.accounts.tests import oidc_hilfen as h
from apps.accounts.tests.test_login_2fa import enable_2fa
from apps.common.tests.factories import UserFactory
from hub.api import befehle as api
from hub.api.openapi import befehle as beschreibung
from hub.api.openapi.build import COMMAND_EXTENSION
from hub.commands import (
    Command,
    Dispatcher,
    Exposure,
    HandlerResult,
    TenantArea,
    ausfuehren,
    get_dispatcher,
    tenant_area,
    tenant_areas,
)
from hub.commands import dispatcher as dispatcher_modul
from hub.commands import exposure as exposure_modul
from hub.commands.channel import OBERFLAECHE, SCHNITTSTELLE, Channel, current_channel
from hub.contracts import Registry, load_registry
from hub.contracts.tests.hilfen import ablegen, befehl_schema

pytestmark = pytest.mark.django_db

BEREICH = "testbereich"
MANDANT_ID = uuid.UUID("3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d")
URL = f"/api/mandari/v1/{BEREICH}/musterstadt/befehle/submission.submit/v1"
SCOPE = catalog.SITZUNG_SCHREIBEN
DOKUMENT = "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"
#: Was der Testhandler je Aufruf gesehen hat: Befehl und Weg
AUFRUFE: list[tuple[Command, Channel]] = []


def einreichen(command: Command) -> HandlerResult:
    AUFRUFE.append((command, current_channel()))
    return HandlerResult(reference=f"E-{len(AUFRUFE)}", aggregate_id=uuid.UUID(command.body["document"]))


def zuruecknehmen(command: Command) -> HandlerResult:
    return HandlerResult(reference="R-1")


def _register(wurzel: Path) -> Registry:
    schema = befehl_schema(
        "submission.submit",
        **{
            "x-owner": "hub.api.tests",
            "x-visibility": "intern",
            "required": ["document", "title"],
            "properties": {
                "document": {"title": "Dokument", "description": "Kennung", "type": "string", "format": "uuid"},
                "title": {"title": "Titel", "description": "Titel", "type": "string", "minLength": 1, "maxLength": 20},
            },
            "examples": [{"document": DOKUMENT, "title": "Bänke im Park"}],
        },
    )
    ablegen(wurzel, "submission.submit", 1, schema)
    ablegen(wurzel, "submission.withdraw", 1, befehl_schema("submission.withdraw", **{"x-owner": "hub.api.tests"}))
    return load_registry(wurzel)


@pytest.fixture
def dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dispatcher]:
    AUFRUFE.clear()
    cache.clear()
    dispatcher = Dispatcher(_register(tmp_path))
    dispatcher.register("submission.submit", 1, einreichen, exposure=Exposure(area=BEREICH, scopes=(SCOPE,)))
    # Ohne Freigabe: nur im selben Prozess erreichbar
    dispatcher.register("submission.withdraw", 1, zuruecknehmen)
    monkeypatch.setattr(dispatcher_modul, "_DEFAULT", dispatcher)
    bereich = TenantArea(
        key=BEREICH,
        kind="session",
        resolve=lambda slug: MANDANT_ID if slug == "musterstadt" else None,
        description="Kurzname im Testbereich",
    )
    monkeypatch.setitem(exposure_modul._TENANT_AREAS, BEREICH, bereich)
    yield dispatcher
    cache.clear()


@pytest.fixture
def person() -> User:
    return cast(User, UserFactory(email="erika@example.org"))  # type: ignore[no-untyped-call]


def _post(
    client: Client | None = None, url: str = URL, body: Any = None, *, key: str | None = "schluessel-1", **headers: str
) -> Any:
    kopf = dict(headers)
    if key is not None:
        kopf["Idempotency-Key"] = key
    daten = json.dumps({"document": DOKUMENT, "title": "Bänke im Park"} if body is None else body)
    return (client or Client()).post(url, data=daten, content_type="application/json", headers=kopf)


def _problem(response: Any, status: int, kind: str) -> dict[str, Any]:
    assert response.status_code == status, response.content
    assert response["Content-Type"] == "application/problem+json; charset=utf-8"
    assert response["Cache-Control"] == "no-store"
    daten = cast(dict[str, Any], response.json())
    assert daten["type"] == f"https://docs.mandari.de/api/probleme/{kind}"
    return daten


def _angemeldet(user: User, *, csrf: bool = False) -> Client:
    client = Client(enforce_csrf_checks=csrf)
    client.force_login(user)
    return client


# =============================================================================
# Browser-Sitzung der eigenen Oberfläche
# =============================================================================


class TestSitzung:
    def test_quittung_mit_person_und_mandant_aus_der_adresse(self, dispatcher: Dispatcher, person: User) -> None:
        response = _post(_angemeldet(person), **{"X-Correlation-ID": "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"})

        assert response.status_code == 201, response.content
        assert response["Cache-Control"] == "no-store"
        quittung = response.json()
        assert quittung["reference"] == "E-1"
        assert quittung["aggregate_id"] == DOKUMENT
        command, weg = AUFRUFE[0]
        assert command.actor_ref == f"user:{person.pk}"
        assert command.tenant_ref == f"session:{MANDANT_ID}"
        assert str(command.correlation_id) == "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
        # Die Browser-Sitzung ist die Oberfläche, auch über die Adresse der Schnittstelle
        assert weg == Channel(OBERFLAECHE)

    def test_ohne_csrf_token_403(self, dispatcher: Dispatcher, person: User) -> None:
        _problem(_post(_angemeldet(person, csrf=True)), 403, "csrf")
        assert AUFRUFE == []

    def test_mit_csrf_token(self, dispatcher: Dispatcher, person: User) -> None:
        client = _angemeldet(person, csrf=True)
        token = _get_new_csrf_string()
        client.cookies[settings.CSRF_COOKIE_NAME] = token
        assert _post(client, **{"X-CSRFToken": token}).status_code == 201

    def test_ohne_anmeldung_401_mit_scope_im_header(self, dispatcher: Dispatcher) -> None:
        response = _post()
        _problem(response, 401, "nicht-authentifiziert")
        assert response["WWW-Authenticate"] == f'Bearer realm="mandari", scope="{SCOPE}"'


# =============================================================================
# Access-Token im Namen einer Person
# =============================================================================


@pytest.fixture
def anbieter() -> Iterator[None]:
    with h.eingeschaltet():
        yield


@pytest.mark.usefixtures("anbieter")
class TestToken:
    def test_person_mit_scope(self, dispatcher: Dispatcher, person: User) -> None:
        enable_2fa(person)
        app = h.anwendung(name="Ratsapp", scopes=["openid", SCOPE])[0]
        token = h.access_token(app, person, scope=f"openid {SCOPE}")

        response = _post(Authorization=f"Bearer {token}")

        assert response.status_code == 201, response.content
        command, weg = AUFRUFE[0]
        assert command.actor_ref == f"user:{person.pk}"
        assert weg == Channel(SCHNITTSTELLE, client_id=app.client_id)

    def test_scope_geschuetzt_ohne_zweiten_faktor_wirkungslos(self, dispatcher: Dispatcher, person: User) -> None:
        app = h.anwendung(name="Ratsapp", scopes=["openid", SCOPE])[0]
        token = h.access_token(app, person, scope=f"openid {SCOPE}")
        response = _post(Authorization=f"Bearer {token}")
        _problem(response, 403, "keine-berechtigung")
        assert 'error="insufficient_scope"' in response["WWW-Authenticate"]

    def test_ohne_scope_403(self, dispatcher: Dispatcher, person: User) -> None:
        app = h.anwendung(name="Leseapp")[0]
        token = h.access_token(app, person, scope=catalog.RATSDATEN_LESEN)
        response = _post(Authorization=f"Bearer {token}")
        _problem(response, 403, "keine-berechtigung")
        assert response["WWW-Authenticate"].endswith('error="insufficient_scope"')
        assert AUFRUFE == []

    def test_dienst_ohne_person_403(self, dispatcher: Dispatcher) -> None:
        app = h.anwendung(
            name="Dienst", ablauf=OAuthApplication.GRANT_CLIENT_CREDENTIALS, scopes=[catalog.EINREICHUNG_SCHREIBEN]
        )[0]
        token = h.access_token(app, None, scope=catalog.EINREICHUNG_SCHREIBEN)
        daten = _problem(_post(Authorization=f"Bearer {token}"), 403, "keine-berechtigung")
        assert daten["detail"] == api.PERSON_REQUIRED

    @pytest.mark.parametrize("kopf", ["Bearer unbekannt", "Bearer mandari_at_falsch", "Basic YTpi"])
    def test_fremde_und_ungueltige_tokens_401(self, dispatcher: Dispatcher, kopf: str) -> None:
        _problem(_post(Authorization=kopf), 401, "nicht-authentifiziert")

    def test_ungueltiges_token_faellt_nicht_auf_die_sitzung_zurueck(self, dispatcher: Dispatcher, person: User) -> None:
        _problem(_post(_angemeldet(person), Authorization="Bearer mandari_at_falsch"), 401, "nicht-authentifiziert")
        assert AUFRUFE == []


# =============================================================================
# Adresse, Inhalt, Idempotenz
# =============================================================================


class TestAdresseUndInhalt:
    @pytest.mark.parametrize(
        "url",
        [
            # ohne Freigabe, unbekannte Version, anderer Mandantenbereich, unbekannter Befehl
            f"/api/mandari/v1/{BEREICH}/musterstadt/befehle/submission.withdraw/v1",
            f"/api/mandari/v1/{BEREICH}/musterstadt/befehle/submission.submit/v2",
            "/api/mandari/v1/anderer/musterstadt/befehle/submission.submit/v1",
            f"/api/mandari/v1/{BEREICH}/musterstadt/befehle/submission.unknown/v1",
        ],
    )
    def test_nicht_freigegeben_404(self, dispatcher: Dispatcher, person: User, url: str) -> None:
        _problem(_post(_angemeldet(person), url), 404, "befehl-unbekannt")

    def test_nur_post(self, dispatcher: Dispatcher, person: User) -> None:
        response = _angemeldet(person).get(URL)
        _problem(response, 405, "methode-nicht-erlaubt")
        assert response["Allow"] == "POST"

    def test_unbekannter_mandant_404(self, dispatcher: Dispatcher, person: User) -> None:
        url = URL.replace("musterstadt", "fremdstadt")
        _problem(_post(_angemeldet(person), url), 404, "mandant-unbekannt")

    def test_ohne_idempotency_key_400(self, dispatcher: Dispatcher, person: User) -> None:
        _problem(_post(_angemeldet(person), key=None), 400, "idempotenzschluessel-fehlt")

    def test_vertrag_verletzt_422_mit_pointer(self, dispatcher: Dispatcher, person: User) -> None:
        daten = _problem(_post(_angemeldet(person), body={"document": DOKUMENT, "title": "x" * 21}), 422, "validierung")
        assert daten["errors"] == [{"pointer": "/title", "detail": "verletzt „maxLength“"}]

    def test_wiederholung_mit_demselben_schluessel(self, dispatcher: Dispatcher, person: User) -> None:
        client = _angemeldet(person)
        erste = _post(client).json()
        zweite = _post(client).json()
        assert erste == zweite
        assert len(AUFRUFE) == 1
        anders = _post(client, body={"document": DOKUMENT, "title": "Anders"})
        _problem(anders, 422, "idempotenzschluessel-wiederverwendet")

    def test_ratenbegrenzung_je_person(
        self, dispatcher: Dispatcher, person: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api, "REQUESTS_PER_MINUTE", 2)
        client = _angemeldet(person)
        for nummer in range(2):
            assert _post(client, key=f"k-{nummer}").status_code == 201
        response = _post(client, key="k-3")
        _problem(response, 429, "ratenlimit")
        assert response["Retry-After"] == "60"


# =============================================================================
# Ein Weg: Oberfläche und Schnittstelle erreichen denselben Handler
# =============================================================================


def test_oberflaeche_und_schnittstelle_laufen_durch_denselben_handler(dispatcher: Dispatcher, person: User) -> None:
    anfrage = RequestFactory().post("/session/musterstadt/x/", {})
    anfrage.user = person
    ergebnis = ausfuehren(
        anfrage, "submission.submit", {"document": DOKUMENT, "title": "Bänke im Park"}, mandant=f"session:{MANDANT_ID}"
    )
    response = _post(_angemeldet(person), key="anderer-schluessel")

    assert ergebnis.ok and response.status_code == 201
    (ui, ui_weg), (http, http_weg) = AUFRUFE
    assert (ui.name, ui.body, ui.tenant_ref, ui.actor_ref) == (http.name, http.body, http.tenant_ref, http.actor_ref)
    assert ui_weg == http_weg == Channel(OBERFLAECHE)
    assert ergebnis.quittung is not None
    assert ergebnis.quittung.content_hash == response.json()["content_hash"]


# =============================================================================
# Beschreibung
# =============================================================================


def test_beschreibung_der_freigegebenen_befehle(dispatcher: Dispatcher) -> None:
    teil = beschreibung.description()

    assert list(teil["paths"]) == [URL.replace("musterstadt", "{mandant}")]
    operation = teil["paths"][URL.replace("musterstadt", "{mandant}")]["post"]
    assert operation[COMMAND_EXTENSION] == "submission.submit"
    assert operation["operationId"] == "befehl_submission_submit_v1"
    assert operation["security"] == [{beschreibung.SECURITY_SCHEME: [SCOPE]}]
    assert {p["name"] for p in operation["parameters"]} == {"mandant", "Idempotency-Key", "X-Correlation-ID"}
    referenz = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema = teil["components"]["schemas"][referenz.rsplit("/", 1)[1]]
    vertrag = dispatcher.registry.get("submission.submit", 1).schema
    assert schema == {key: value for key, value in vertrag.items() if key not in {"$schema", "$id"}}
    assert set(operation["responses"]) >= {"201", "400", "401", "403", "404", "422", "429"}
    flows = teil["components"]["securitySchemes"][beschreibung.SECURITY_SCHEME]["flows"]
    assert flows["authorizationCode"]["scopes"] == {SCOPE: catalog.DESCRIPTIONS[SCOPE]}


# =============================================================================
# Jeder Befehl, den die Installation freigibt
# =============================================================================


def _freigaben() -> list[tuple[str, int]]:
    return list(get_dispatcher().exposures)


@pytest.mark.parametrize(("name", "version"), _freigaben())
def test_jeder_freigegebene_befehl_ist_eine_operation(name: str, version: int) -> None:
    """Freigabe, Mandantenbereich, Vertrag und Operation passen zusammen; die Adresse verlangt Anmeldung."""
    freigabe = get_dispatcher().exposure(name, version)
    assert freigabe is not None
    assert tenant_area(freigabe.area) is not None, f"Mandantenbereich {freigabe.area} nicht angemeldet"
    pfad = beschreibung.command_path(freigabe.area, name, version)
    operation = beschreibung.description()["paths"][pfad]["post"]
    assert operation[COMMAND_EXTENSION] == name

    cache.clear()
    response = Client().post(
        pfad.replace("{mandant}", "irgendwo"),
        data="{}",
        content_type="application/json",
        headers={"Idempotency-Key": "k"},
    )
    _problem(response, 401, "nicht-authentifiziert")


def test_die_installation_gibt_befehle_frei() -> None:
    """Session meldet seinen Mandantenbereich an und gibt seine Befehle frei (Beispiel: Textbausteine)."""
    assert ("session.textblock.create", 1) in get_dispatcher().exposures
    assert "session" in {bereich.key for bereich in tenant_areas()}

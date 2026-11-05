# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einstieg ``/api/`` und die eine OpenAPI-Beschreibung (Issue #1205).

- Einstieg, Beschreibung und Dokumentationsseite sind erreichbar; der Einstieg ist kein zweites OParl-System.
- Die Beschreibung umfasst Einstieg, OParl-Aggregator, OParl je Mandant und Session-API v1, ist gültig (alle
  Verweise auflösbar, Schemas nach JSON Schema 2020-12) und passt zu den echten Antworten der OParl-Ausgaben.
- Die eingecheckte Datei ist aktuell, jeder registrierte Befehl hat einen Endpunkt oder steht in der
  Ausnahmeliste.
- Die bestehenden Adressen unter ``/api/`` führen weiter zu denselben Endpunkten.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings
from django.urls import Resolver404, resolve
from django.utils import timezone
from jsonschema import Draft202012Validator

from apps.session.api.v1.api import api as session_api_v1
from apps.session.models import SessionMeeting, SessionOrganization, SessionPaper, SessionTenant
from hub.api.entry import KOMMPARL_VERSION, description
from hub.api.openapi import Area, commands_with_endpoint, operations, register_area
from hub.api.openapi import registry as area_registry
from hub.api.openapi.build import COMMAND_EXTENSION, build_description, render
from hub.contracts import COMMAND, get_registry
from hub.contracts.management.commands.schnittstelle_beschreibung import DESCRIPTION_PATH, description_file
from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlPaper, OParlSource

SITE = "https://mandari.example"
EXCEPTIONS = Path(__file__).resolve().parent.parent / "openapi" / "befehle_ohne_endpunkt.json"


@pytest.fixture(autouse=True)
def _einstellungen() -> Iterator[None]:
    cache.clear()
    with override_settings(SITE_URL=SITE, OPARL_BASE_URL=f"{SITE}/oparl", OPARL_API_RATE_LIMIT=0):
        yield
    cache.clear()


@pytest.fixture(scope="module")
def beschreibung() -> dict[str, Any]:
    return description()


def _validate(document: dict[str, Any], schema: dict[str, Any], data: Any) -> None:
    Draft202012Validator({**schema, "components": document["components"]}).validate(data)


def _ref(name: str) -> dict[str, Any]:
    return {"$ref": f"#/components/schemas/{name}"}


# =============================================================================
# Einstieg, Beschreibung, Dokumentation
# =============================================================================


@pytest.mark.django_db
class TestEinstieg:
    def test_verweist_auf_system_beschreibung_und_bereiche(self, client: Client) -> None:
        response = client.get("/api/")
        assert response.status_code == 200
        assert response["Content-Type"].startswith("application/json")
        assert response["Access-Control-Allow-Origin"] == "*"
        body = response.json()
        assert body["system"] == f"{SITE}/oparl/v1/system"
        assert body["oparlVersion"] == "https://schema.oparl.org/1.1/"
        assert body["kommparl:conformsTo"] == [KOMMPARL_VERSION]
        assert body["openapi"] == f"{SITE}/api/openapi.json"
        assert body["documentation"] == f"{SITE}/api/docs/"
        bereiche = {bereich["key"]: bereich for bereich in body["areas"]}
        assert list(bereiche) == ["oparl", "oparl-mandant", "session-v1"]
        assert bereiche["oparl"]["entry"] == f"{SITE}/oparl/v1/system"
        assert bereiche["oparl-mandant"]["entry"] == f"{SITE}/session/{{tenant_slug}}/api/oparl/"
        assert bereiche["session-v1"]["entry"] == f"{SITE}/api/v1/session/{{tenant_slug}}/"

    def test_ist_kein_zweites_oparl_system(self, client: Client) -> None:
        body = client.get("/api/").json()
        # Keine eigene Kennung und kein Typ: Das System der Installation bleibt eines (Adresse = Kennung)
        assert "id" not in body
        assert "type" not in body
        assert client.get("/oparl/v1/system").json()["id"] == body["system"]

    def test_passt_zur_beschreibung(self, client: Client, beschreibung: dict[str, Any]) -> None:
        _validate(beschreibung, _ref("Einstieg"), client.get("/api/").json())

    def test_bedingter_abruf_und_nur_lesend(self, client: Client) -> None:
        etag = client.get("/api/")["ETag"]
        assert client.get("/api/", headers={"If-None-Match": etag}).status_code == 304
        assert client.post("/api/").status_code == 405


@pytest.mark.django_db
class TestBeschreibungAbrufen:
    def test_openapi_json(self, client: Client) -> None:
        response = client.get("/api/openapi.json")
        assert response.status_code == 200
        body = response.json()
        assert body["openapi"] == "3.1.0"
        assert body == json.loads(render(description()))
        assert response["ETag"]

    def test_dokumentation_mit_lokalen_dateien(self, client: Client) -> None:
        response = client.get("/api/docs/")
        assert response.status_code == 200
        html = response.content.decode()
        assert "swagger-ui" in html
        assert "/static/ninja/swagger-ui-bundle.js" in html
        assert "/api/openapi.json" in html
        for cdn in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs"):
            assert cdn not in html


class TestBeschreibung:
    def test_alle_bereiche_als_schlagworte(self, beschreibung: dict[str, Any]) -> None:
        tags = [tag["name"] for tag in beschreibung["tags"]]
        assert tags == ["Einstieg", "OParl-Aggregator", "OParl je Mandant", "Session-API v1"]
        for method, path, operation in operations(beschreibung):
            assert len(operation["tags"]) == 1 and operation["tags"][0] in tags, (method, path)

    def test_enthaelt_oparl_und_session(self, beschreibung: dict[str, Any]) -> None:
        paths = beschreibung["paths"]
        for path in (
            "/api/",
            "/oparl/v1/system",
            "/oparl/v1/bodies",
            "/oparl/v1/body/{id}/meetings",
            "/oparl/v1/body/{id}/changes",
            "/oparl/v1/body/{id}/snapshot",
            "/oparl/v1/paper/{id}",
            "/session/{tenant_slug}/api/oparl/",
            "/session/{tenant_slug}/api/oparl/papers/",
            "/session/{tenant_slug}/api/oparl/paper/{id}/",
            "/session/{tenant_slug}/api/oparl/file/{id}/download/",
            "/api/v1/session/{tenant_slug}/applications/submit/",
        ):
            assert path in paths, path

    def test_session_api_v1_unveraendert_enthalten(self, beschreibung: dict[str, Any]) -> None:
        """Pfade und Schemas der Session-API v1 sind dieselben wie in ihrer eigenen Beschreibung (nur das Schlagwort)."""
        eigene = session_api_v1.get_openapi_schema()
        for path, item in eigene["paths"].items():
            gesamt = beschreibung["paths"][path]
            for method, operation in item.items():
                assert {**operation, "tags": None} == {**gesamt[method], "tags": None}
        for name, schema in eigene["components"]["schemas"].items():
            assert beschreibung["components"]["schemas"][name] == schema
        assert eigene["paths"]["/api/v1/session/{tenant_slug}/applications/submit/"]["post"][COMMAND_EXTENSION] == (
            "submission.submit"
        )

    def test_verweise_aufloesbar(self, beschreibung: dict[str, Any]) -> None:
        def refs(node: Any) -> Iterator[str]:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref" and isinstance(value, str):
                        yield value
                    else:
                        yield from refs(value)
            elif isinstance(node, list):
                for value in node:
                    yield from refs(value)

        for ref in set(refs(beschreibung)):
            section, name = ref.removeprefix("#/components/").split("/")
            assert name in beschreibung["components"][section], ref

    def test_schemas_nach_json_schema_2020_12(self, beschreibung: dict[str, Any]) -> None:
        for schema in beschreibung["components"]["schemas"].values():
            Draft202012Validator.check_schema(schema)

    def test_eindeutige_operation_ids_und_pfadparameter(self, beschreibung: dict[str, Any]) -> None:
        ids = [operation["operationId"] for _, _, operation in operations(beschreibung)]
        assert len(ids) == len(set(ids))
        for _method, path, operation in operations(beschreibung):
            im_pfad = {teil[1:-1] for teil in path.split("/") if teil.startswith("{")}
            parameter = {p["name"] for p in operation.get("parameters", []) if p.get("in") == "path"}
            assert im_pfad == parameter, path

    def test_unabhaengig_vom_host(self, beschreibung: dict[str, Any]) -> None:
        text = render(beschreibung)
        assert SITE not in text
        assert "testserver" not in text

    def test_eingecheckte_datei_ist_aktuell(self) -> None:
        eingecheckt = description_file().read_text(encoding="utf-8") if description_file().exists() else ""
        assert eingecheckt == render(description()), (
            f"{DESCRIPTION_PATH.as_posix()} ist veraltet: in mandari/ `python manage.py schnittstelle_beschreibung` "
            "ausführen und die Datei mitcommitten."
        )


class TestBefehlsparitaet:
    def test_jeder_befehl_hat_einen_endpunkt_oder_steht_in_der_ausnahmeliste(
        self, beschreibung: dict[str, Any]
    ) -> None:
        befehle = {contract.name for contract in get_registry().contracts() if contract.kind == COMMAND}
        mit_endpunkt = set(commands_with_endpoint(beschreibung))
        ausnahmen = json.loads(EXCEPTIONS.read_text(encoding="utf-8"))["befehle"]
        assert ausnahmen == sorted(set(ausnahmen)), "Ausnahmeliste: sortiert und ohne Doppelte"
        assert mit_endpunkt <= befehle, f"Endpunkt nennt unbekannte Befehle: {sorted(mit_endpunkt - befehle)}"
        fehlend = befehle - mit_endpunkt - set(ausnahmen)
        assert not fehlend, f"Befehle ohne Endpunkt (Endpunkt mit {COMMAND_EXTENSION} anbieten): {sorted(fehlend)}"
        veraltet = set(ausnahmen) - (befehle - mit_endpunkt)
        assert not veraltet, f"Aus der Ausnahmeliste streichen: {sorted(veraltet)}"

    def test_einreichung_ist_abgedeckt(self, beschreibung: dict[str, Any]) -> None:
        assert commands_with_endpoint(beschreibung)["submission.submit"] == [
            "POST /api/v1/session/{tenant_slug}/applications/submit/"
        ]


class TestZusammenfuegen:
    @pytest.fixture
    def bereiche(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Area]:
        leer: dict[str, Area] = {}
        monkeypatch.setattr(area_registry, "_AREAS", leer)
        return leer

    @staticmethod
    def _bereich(key: str, paths: dict[str, Any], schemas: dict[str, Any] | None = None) -> Area:
        part = {"paths": paths, "components": {"schemas": schemas or {}}}
        return Area(key=key, title=key, description="", entry="/x/", openapi=lambda: part)

    @staticmethod
    def _get(operation_id: str) -> dict[str, Any]:
        return {"get": {"operationId": operation_id, "responses": {"200": {"description": "ok"}}}}

    def test_doppelter_pfad(self, bereiche: dict[str, Area]) -> None:
        register_area(self._bereich("a", {"/x/": self._get("a")}))
        register_area(self._bereich("b", {"/x/": self._get("b")}))
        with pytest.raises(ImproperlyConfigured, match="Pfad /x/"):
            build_description()

    def test_doppelte_operation_id(self, bereiche: dict[str, Area]) -> None:
        register_area(self._bereich("a", {"/a/": self._get("gleich")}))
        register_area(self._bereich("b", {"/b/": self._get("gleich")}))
        with pytest.raises(ImproperlyConfigured, match="operationId gleich"):
            build_description()

    def test_schemas_gleichen_namens(self, bereiche: dict[str, Area]) -> None:
        register_area(self._bereich("a", {"/a/": self._get("a")}, {"S": {"type": "string"}}))
        register_area(self._bereich("b", {"/b/": self._get("b")}, {"S": {"type": "string"}}))
        assert build_description()["components"]["schemas"]["S"] == {"type": "string"}
        register_area(self._bereich("c", {"/c/": self._get("c")}, {"S": {"type": "integer"}}))
        with pytest.raises(ImproperlyConfigured, match="components.schemas.S"):
            build_description()

    def test_ungueltige_kennung(self) -> None:
        with pytest.raises(ValueError):
            Area(key="Ungültig", title="", description="", entry="/x/", openapi=dict)
        with pytest.raises(ValueError):
            Area(key="gut", title="", description="", entry="ohne-schraegstrich", openapi=dict)


# =============================================================================
# Bestehende Adressen unter /api/
# =============================================================================


class TestBestehendeAdressen:
    @pytest.mark.parametrize(
        ("path", "name"),
        [
            ("/api/stats/", "insight_api:stats"),
            ("/api/stats/bodies/", "insight_api:stats_bodies"),
            ("/api/contact/", "insight_api:contact_submit"),
            ("/api/contact/subjects/", "insight_api:contact_subjects"),
            ("/api/v1/session/openapi.json", "session_api_v1:openapi-json"),
            ("/api/v1/session/docs", "session_api_v1:openapi-view"),
            ("/api/v1/session/musterstadt/", "session_api_v1:tenant_root"),
            ("/api/v1/session/musterstadt/meetings/", "session_api_v1:meetings"),
            ("/api/v1/session/musterstadt/applications/submit/", "session_api_v1:submit_application"),
            ("/api/public/v1/openapi.json", "faction_public_api:openapi"),
            ("/api/public/v1/fraktionen/token123/", "faction_public_api:root"),
            ("/api/provisioning/organizations/", "provisioning:organizations"),
            ("/api/provisioning/organizations/fraktion/", "provisioning:organization_detail"),
            ("/api/provisioning/v2/work/", "provisioning_v2:work_list"),
            ("/oparl/", "oparl_api:root"),
            ("/oparl/v1/system", "oparl_api:system"),
            ("/session/musterstadt/api/", "session:api_root"),
            ("/session/musterstadt/api/oparl/", "session:oparl_system"),
            # neu
            ("/api/", "schnittstelle:entry"),
            ("/api/openapi.json", "schnittstelle:openapi"),
            ("/api/docs/", "schnittstelle:docs"),
        ],
    )
    def test_adresse_fuehrt_zum_endpunkt(self, path: str, name: str) -> None:
        assert resolve(path).view_name == name

    @pytest.mark.parametrize("path", ["/api/unbekannt/", "/api/v2/", "/api/docs/extra/", "/api/openapi.yaml"])
    def test_unbekannte_adressen_bleiben_unbekannt(self, path: str) -> None:
        with pytest.raises(Resolver404):
            resolve(path)


# =============================================================================
# Echte Antworten passen zur Beschreibung
# =============================================================================


def _response_schema(document: dict[str, Any], path: str) -> dict[str, Any]:
    content = document["paths"][path]["get"]["responses"]["200"]["content"]
    return dict(content["application/json"]["schema"])


@pytest.mark.django_db
class TestAntwortenPassen:
    def test_aggregator(self, client: Client, beschreibung: dict[str, Any]) -> None:
        ris = "https://ris.example/oparl"
        source = OParlSource.objects.create(name="Musterstadt", url=f"{ris}/system")
        body = OParlBody.objects.create(external_id=f"{ris}/body/1", source=source, name="Musterstadt", slug="m")
        OParlOrganization.objects.create(
            external_id=f"{ris}/organization/1", body=body, name="Rat", organization_type="committee"
        )
        geloescht = OParlOrganization.objects.create(
            external_id=f"{ris}/organization/2", body=body, name="Alt", deleted=True
        )
        OParlMeeting.objects.create(
            external_id=f"{ris}/meeting/1", body=body, name="Rat", start=datetime(2026, 10, 1, 17, tzinfo=UTC)
        )
        vorlage = OParlPaper.objects.create(external_id=f"{ris}/paper/1", body=body, name="Radweg", reference="V/1")
        faelle = {
            "/oparl/v1/system": "/oparl/v1/system",
            "/oparl/v1/bodies": "/oparl/v1/bodies",
            f"/oparl/v1/body/{body.pk}": "/oparl/v1/body/{id}",
            f"/oparl/v1/body/{body.pk}/organizations": "/oparl/v1/body/{id}/organizations",
            f"/oparl/v1/body/{body.pk}/organizations?modified_since=2000-01-01T00:00:00Z": (
                "/oparl/v1/body/{id}/organizations"
            ),
            f"/oparl/v1/body/{body.pk}/meetings": "/oparl/v1/body/{id}/meetings",
            f"/oparl/v1/body/{body.pk}/papers": "/oparl/v1/body/{id}/papers",
            f"/oparl/v1/paper/{vorlage.pk}": "/oparl/v1/paper/{id}",
            f"/oparl/v1/organization/{geloescht.pk}": "/oparl/v1/organization/{id}",
            "/oparl/": "/oparl/",
        }
        for adresse, pfad in faelle.items():
            response = client.get(adresse)
            assert response.status_code == 200, adresse
            _validate(beschreibung, _response_schema(beschreibung, pfad), response.json())
        fehler = client.get("/oparl/v1/paper/00000000-0000-0000-0000-000000000000")
        assert fehler.status_code == 404
        _validate(beschreibung, _ref("OParlError"), fehler.json())

    def test_session_mandant(self, client: Client, beschreibung: dict[str, Any]) -> None:
        tenant = SessionTenant.objects.create(
            name="Stadt Musterstadt", slug="musterstadt", oparl_public_since=timezone.now()
        )
        rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
        sitzung = SessionMeeting.objects.create(
            tenant=tenant,
            name="Ratssitzung",
            organization=rat,
            start=timezone.now(),
            is_public=True,
            location="Rathaus",
        )
        SessionPaper.objects.create(
            tenant=tenant, reference="V/1", name="Radweg", is_public=True, status="approved", date=date(2026, 9, 1)
        )
        basis = "/session/musterstadt/api/oparl/"
        vorlage = "/session/{tenant_slug}/api/oparl/"
        faelle = {
            basis: vorlage,
            f"{basis}bodies/": f"{vorlage}bodies/",
            f"{basis}body/": f"{vorlage}body/",
            f"{basis}organizations/": f"{vorlage}organizations/",
            f"{basis}meetings/": f"{vorlage}meetings/",
            f"{basis}papers/": f"{vorlage}papers/",
            f"{basis}meeting/{sitzung.pk}/": f"{vorlage}meeting/{{id}}/",
        }
        for adresse, pfad in faelle.items():
            response = client.get(adresse)
            assert response.status_code == 200, adresse
            _validate(beschreibung, _response_schema(beschreibung, pfad), response.json())

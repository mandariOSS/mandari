# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Bausteine der Schnittstellen (``apps.common.api``, Issue #1205): Problem nach RFC 9457, ETag und
bedingte Abrufe, Abkündigung mit ``Deprecation``/``Sunset``, ``Idempotency-Key``, Seiten wie in OParl.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.test import RequestFactory
from jsonschema import Draft202012Validator

from apps.common.api.conditional import conditional, etag_for
from apps.common.api.deprecation import Deprecation, deprecation_value, sunset_value
from apps.common.api.idempotency import KEY_MISSING, MAX_KEY_LENGTH, parse_idempotency_key, valid_idempotency_key
from apps.common.api.pages import LINKS_SCHEMA, PAGINATION_SCHEMA, list_envelope, page_schema
from apps.common.api.problems import (
    ABOUT_BLANK,
    PROBLEM_SCHEMA,
    PROBLEM_TYPE_BASE,
    Problem,
    ProblemError,
    problem_response,
)


class TestProblem:
    def test_felder_in_fester_reihenfolge(self) -> None:
        problem = Problem(
            status=422,
            kind="validierung",
            detail="Ungültig.",
            errors=({"pointer": "/a", "detail": "fehlt"},),
            extensions={"snapshot": "https://x.example/s", "status": 999},
        )
        data = problem.to_dict(instance="/pfad", request_id=True)
        assert list(data) == ["type", "title", "status", "detail", "instance", "request_id", "errors", "snapshot"]
        assert data["type"] == f"{PROBLEM_TYPE_BASE}validierung"
        assert data["title"] == "Validierung fehlgeschlagen"
        # Erweiterungen überschreiben die Felder der RFC nicht
        assert data["status"] == 422
        assert data["request_id"] is None

    def test_ohne_art_about_blank(self) -> None:
        assert Problem(status=400, kind=None, detail="x").type == ABOUT_BLANK
        assert Problem(status=418, kind=None, detail="x").title == "Fehler"

    def test_wiederholbar(self) -> None:
        assert Problem(status=503, kind="x", detail="x").retryable
        assert not Problem(status=422, kind="x", detail="x").retryable

    def test_from_dict_nimmt_den_status_der_antwort(self) -> None:
        problem = Problem.from_dict(
            {"type": f"{PROBLEM_TYPE_BASE}konflikt", "status": 200, "detail": "d", "request_id": "r1"}, status=409
        )
        assert (problem.status, problem.kind, problem.detail) == (409, "konflikt", "d")
        assert problem.extensions == {"request_id": "r1"}
        assert Problem.from_dict({"type": "https://fremd.example/x"}, status=500).kind == "unbekannt"

    def test_antwort(self) -> None:
        response = problem_response(
            Problem(status=429, kind="ratenlimit", detail="Zu viele für heute."),
            instance="/a?b=1",
            headers={"Retry-After": "60"},
        )
        assert response.status_code == 429
        assert response["Content-Type"] == "application/problem+json; charset=utf-8"
        assert response["Retry-After"] == "60"
        body = json.loads(response.content)
        assert body["instance"] == "/a?b=1"
        assert "request_id" in body
        # Umlaute als UTF-8, nicht maskiert
        assert "für" in response.content.decode("utf-8")

    def test_ausnahme_traegt_das_problem(self) -> None:
        exc = ProblemError(404, "Fehlt.", kind="nicht-gefunden", headers={"X": "1"}, hinweis="h")
        assert exc.status_code == 404
        assert exc.headers == {"X": "1"}
        assert exc.problem == Problem(status=404, kind="nicht-gefunden", detail="Fehlt.", extensions={"hinweis": "h"})

    def test_schema_ist_gueltig_und_passt(self) -> None:
        Draft202012Validator.check_schema(PROBLEM_SCHEMA)
        data = Problem(status=410, kind="x", detail="d").to_dict(instance="/i", request_id=True)
        Draft202012Validator(PROBLEM_SCHEMA).validate(data)


class TestBedingterAbruf:
    def test_etag_haengt_nur_vom_inhalt_ab(self) -> None:
        assert etag_for(b"a") == etag_for(b"a")
        assert etag_for(b"a") != etag_for(b"b")
        assert etag_for(b"a").startswith('"') and len(etag_for(b"a")) == 34

    def test_passender_etag_ergibt_304_mit_cors_und_link(self) -> None:
        response = HttpResponse(b"{}", content_type="application/json")
        response["ETag"] = etag_for(b"{}")
        response["Access-Control-Allow-Origin"] = "*"
        response["Link"] = '<https://x.example/?page=2>; rel="next"'
        request = RequestFactory().get("/", headers={"If-None-Match": etag_for(b"{}")})
        not_modified = conditional(request, response)
        assert not_modified.status_code == 304
        assert not_modified["Access-Control-Allow-Origin"] == "*"
        assert not_modified["Link"] == response["Link"]

    def test_anderer_etag_oder_fehler_bleiben_unveraendert(self) -> None:
        response = HttpResponse(b"{}")
        response["ETag"] = etag_for(b"{}")
        assert conditional(RequestFactory().get("/", headers={"If-None-Match": '"x"'}), response) is response
        fehler = HttpResponse(status=404)
        fehler["ETag"] = '"x"'
        assert conditional(RequestFactory().get("/", headers={"If-None-Match": '"x"'}), fehler) is fehler


class TestAbkuendigung:
    def test_werte_nach_rfc(self) -> None:
        # Dieselben Werte wie die alte Session-API seit 0.11.0 (apps/session/api/views.py)
        assert deprecation_value(datetime(2026, 9, 27, tzinfo=UTC)) == "@1790467200"
        assert sunset_value(datetime(2027, 5, 31, tzinfo=UTC)) == "Mon, 31 May 2027 00:00:00 GMT"

    def test_header_mit_nachfolger(self) -> None:
        abkuendigung = Deprecation(datetime(2026, 9, 27, tzinfo=UTC), datetime(2027, 5, 31, tzinfo=UTC))
        response = abkuendigung.apply(HttpResponse(), successor="/api/v2/")
        assert response["Deprecation"] == "@1790467200"
        assert response["Sunset"] == "Mon, 31 May 2027 00:00:00 GMT"
        assert response["Link"] == '</api/v2/>; rel="successor-version"'
        assert "Link" not in abkuendigung.apply(HttpResponse())

    @pytest.mark.parametrize(
        ("abgekuendigt", "wegfall"),
        [
            (datetime(2026, 9, 27), datetime(2027, 5, 31, tzinfo=UTC)),
            (datetime(2027, 5, 31, tzinfo=UTC), datetime(2026, 9, 27, tzinfo=UTC)),
        ],
    )
    def test_ungueltige_zeitpunkte(self, abgekuendigt: datetime, wegfall: datetime) -> None:
        with pytest.raises(ValueError):
            Deprecation(abgekuendigt, wegfall)


class TestIdempotenzschluessel:
    @pytest.mark.parametrize(
        ("roh", "wert"),
        [(None, None), ("", None), ("  ", None), ('""', None), ('"abc"', "abc"), (" abc ", "abc"), ('"a', '"a')],
    )
    def test_header_lesen(self, roh: str | None, wert: str | None) -> None:
        assert parse_idempotency_key(roh) == wert

    @pytest.mark.parametrize(
        ("schluessel", "gueltig"),
        [
            ("a", True),
            ("x" * MAX_KEY_LENGTH, True),
            ("x" * (MAX_KEY_LENGTH + 1), False),
            ("", False),
            ("mit leerzeichen", False),
            ('mit"anfuehrung', False),
            ("mit\\rueckstrich", False),
            ("ümlaut", False),
            (None, False),
            (123, False),
        ],
    )
    def test_gueltigkeit(self, schluessel: object, gueltig: bool) -> None:
        assert valid_idempotency_key(schluessel) is gueltig

    def test_feste_meldung(self) -> None:
        assert "Idempotenzschlüssel" in KEY_MISSING


class TestSeiten:
    def _seite(self, nummer: int, filter_: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        paginator: Paginator[int] = Paginator(list(range(25)), 10)
        page = paginator.page(nummer)
        return list_envelope("https://x.example/liste", filter_ or {}, paginator, page, [{"id": str(n)} for n in page])

    def test_mittlere_seite(self) -> None:
        huelle, kopf = self._seite(2, {"modified_since": "2026-01-01T00:00:00+01:00"})
        assert huelle["pagination"] == {"totalElements": 25, "elementsPerPage": 10, "currentPage": 2, "totalPages": 3}
        assert set(huelle["links"]) == {"first", "self", "prev", "next", "last"}
        assert "modified_since=2026-01-01T00%3A00%3A00%2B01%3A00" in huelle["links"]["next"]
        assert 'rel="self"' not in kopf["Link"]
        assert kopf["Link"].count("rel=") == 4

    def test_erste_seite_ohne_page_parameter(self) -> None:
        huelle, _ = self._seite(1)
        assert huelle["links"]["first"] == "https://x.example/liste"
        assert "prev" not in huelle["links"]

    def test_schema_passt_zur_seite(self) -> None:
        schema = {
            **page_schema({"type": "object"}, pagination="#/$defs/p", links="#/$defs/l"),
            "$defs": {"p": PAGINATION_SCHEMA, "l": LINKS_SCHEMA},
        }
        Draft202012Validator.check_schema(schema)
        huelle, _ = self._seite(3)
        Draft202012Validator(schema).validate(huelle)

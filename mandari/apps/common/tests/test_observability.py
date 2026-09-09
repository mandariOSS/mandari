# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request-Kennung, JSON-Logs und OpenTelemetry-Schalter (Issue #162)."""

from __future__ import annotations

import json
import logging

import pytest
from django.http import HttpResponse
from django.test import Client, RequestFactory

from apps.common.observability import (
    REQUEST_ID_HEADER,
    JsonFormatter,
    RequestContextFilter,
    RequestIdMiddleware,
    logging_config,
    request_id_var,
    setup_opentelemetry,
    user_id_var,
)


def _record(msg: str = "hallo", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("apps.test", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestRequestIdMiddleware:
    def test_generates_id_and_returns_header(self) -> None:
        seen: dict[str, str] = {}

        def view(request):  # type: ignore[no-untyped-def]
            seen["in_view"] = request_id_var.get()
            return HttpResponse("ok")

        response = RequestIdMiddleware(view)(RequestFactory().get("/"))
        assert response[REQUEST_ID_HEADER] == seen["in_view"]
        assert len(seen["in_view"]) == 32
        assert request_id_var.get() == "", "Kontext muss nach der Anfrage zurückgesetzt sein"

    def test_accepts_valid_incoming_id(self) -> None:
        response = RequestIdMiddleware(lambda r: HttpResponse())(
            RequestFactory().get("/", HTTP_X_REQUEST_ID="caddy-abc-12345")
        )
        assert response[REQUEST_ID_HEADER] == "caddy-abc-12345"

    @pytest.mark.parametrize("bad", ["", "kurz", "x" * 200, "böse id", "<script>"])
    def test_rejects_invalid_incoming_id(self, bad: str) -> None:
        response = RequestIdMiddleware(lambda r: HttpResponse())(RequestFactory().get("/", HTTP_X_REQUEST_ID=bad))
        assert response[REQUEST_ID_HEADER] != bad
        assert len(response[REQUEST_ID_HEADER]) == 32

    @pytest.mark.django_db
    def test_end_to_end_via_client(self, client: Client) -> None:
        response = client.get("/accounts/login/")
        assert response.status_code == 200
        assert REQUEST_ID_HEADER in response


class TestLogging:
    def test_filter_adds_context_fields(self) -> None:
        token = request_id_var.set("req-1")
        user_token = user_id_var.set("user-1")
        try:
            record = _record()
            assert RequestContextFilter().filter(record) is True
            assert record.__dict__["request_id"] == "req-1"
            assert record.__dict__["user_id"] == "user-1"
            assert record.__dict__["trace_id"] == "-"
        finally:
            request_id_var.reset(token)
            user_id_var.reset(user_token)

    def test_json_formatter_emits_one_json_line(self) -> None:
        record = _record("Antrag %s gespeichert", request_id="req-2", user_id="u", trace_id="-", span_id="-")
        record.args = ("42",)
        record.antrag_id = 42
        line = JsonFormatter().format(record)
        payload = json.loads(line)
        assert payload["msg"] == "Antrag 42 gespeichert"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "apps.test"
        assert payload["request_id"] == "req-2"
        assert payload["antrag_id"] == 42
        assert "\n" not in line

    def test_json_formatter_includes_exception(self) -> None:
        try:
            raise ValueError("kaputt")
        except ValueError:
            import sys

            record = _record("Fehler")
            record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
        assert "ValueError: kaputt" in payload["exception"]

    def test_production_config_has_no_debug_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        config = logging_config(debug=False)
        assert config["handlers"]["console"]["formatter"] == "json"
        assert all(spec["level"] != "DEBUG" for spec in config["loggers"].values())
        assert "request_context" in config["handlers"]["console"]["filters"]

    def test_debug_level_is_capped_in_production(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        config = logging_config(debug=False)
        assert config["loggers"]["apps"]["level"] == "INFO"

    def test_development_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        config = logging_config(debug=True)
        assert config["handlers"]["console"]["formatter"] == "text"
        assert config["loggers"]["apps"]["level"] == "DEBUG"


class TestOpenTelemetry:
    def test_noop_without_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        assert setup_opentelemetry() is False

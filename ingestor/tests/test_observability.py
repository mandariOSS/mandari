# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strukturierte Logs, Trigger-Kontext und Trace-Verkettung mit Django (Issue #162)."""

from __future__ import annotations

import json
import logging

from src.observability import (
    ContextFilter,
    JsonFormatter,
    parent_context,
    request_id_var,
    setup_opentelemetry,
    trace_id_var,
    trigger_context,
)


def _record(msg: str = "hallo") -> logging.LogRecord:
    return logging.LogRecord("src.test", logging.INFO, __file__, 1, msg, None, None)


def test_filter_uses_trigger_context() -> None:
    with trigger_context("sync.trigger", "req-1", None, None):
        record = _record()
        assert ContextFilter().filter(record)
        assert record.__dict__["request_id"] == "req-1"
    assert request_id_var.get() == ""
    assert trace_id_var.get() == ""


def test_json_formatter_line() -> None:
    record = _record("Sync %s")
    record.args = ("ok",)
    record.__dict__["request_id"] = "req-2"
    record.__dict__["trace_id"] = "abc"
    record.__dict__["span_id"] = "-"
    record.__dict__["full"] = True
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "Sync ok"
    assert payload["request_id"] == "req-2"
    assert payload["trace_id"] == "abc"
    assert payload["full"] is True
    assert payload["service"] == "mandari-ingestor"


def test_parent_context_from_django_ids() -> None:
    from opentelemetry import trace

    ctx = parent_context("0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331")
    assert ctx is not None
    span = trace.get_current_span(ctx)
    assert format(span.get_span_context().trace_id, "032x") == "0af7651916cd43dd8448eb211c80319c"
    assert parent_context(None, None) is None
    assert parent_context("kein-hex", "b7ad6b7169203331") is None
    assert parent_context("0" * 32, "0" * 16) is None


def test_trigger_span_continues_django_trace() -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    trace.set_tracer_provider(TracerProvider())
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    with trigger_context("sync.trigger", "req-3", trace_id, "b7ad6b7169203331"):
        current = trace.get_current_span().get_span_context()
        assert format(current.trace_id, "032x") == trace_id
        record = _record()
        ContextFilter().filter(record)
        assert record.__dict__["trace_id"] == trace_id
        assert record.__dict__["request_id"] == "req-3"


def test_otel_noop_without_endpoint(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert setup_opentelemetry() is False

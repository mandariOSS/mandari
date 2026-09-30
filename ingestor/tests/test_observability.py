# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strukturierte Logs, Trigger-Kontext und Trace-Verkettung mit Django (Issue #162)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

from src import observability
from src.observability import (
    ContextFilter,
    JsonFormatter,
    parent_context,
    request_id_var,
    setup_opentelemetry,
    trace_id_var,
    trigger_context,
)

INGESTOR = Path(__file__).resolve().parents[1]


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


class _Wirksam:
    is_instrumented_by_opentelemetry = False

    def instrument(self, **kwargs: object) -> None:
        type(self).is_instrumented_by_opentelemetry = True


class _Abgelehnt:
    """Wie eine Instrumentierung, die die installierte Version ablehnt: kein Fehler, aber auch keine Wirkung."""

    is_instrumented_by_opentelemetry = False

    def instrument(self, **kwargs: object) -> None:
        return None


class _Kaputt:
    is_instrumented_by_opentelemetry = False

    def instrument(self, **kwargs: object) -> None:
        raise RuntimeError("kaputt")


def test_gemeldet_wird_nur_was_tatsaechlich_instrumentiert_ist(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """
    ``instrument()`` ohne Ausnahme heißt nicht, dass Spans entstehen: Lehnt die Instrumentierung die
    installierte Version ab, kehrt sie still zurück. Vorher stand die Bibliothek trotzdem in der Erfolgsmeldung.
    """
    modul = types.ModuleType("instrumentierung_im_test")
    modul.Wirksam = _Wirksam  # type: ignore[attr-defined]
    modul.Abgelehnt = _Abgelehnt  # type: ignore[attr-defined]
    modul.Kaputt = _Kaputt  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "instrumentierung_im_test", modul)
    monkeypatch.setattr(_Wirksam, "is_instrumented_by_opentelemetry", False)
    monkeypatch.setattr(
        observability,
        "_INSTRUMENTATIONS",
        (
            ("wirksam", "instrumentierung_im_test", "Wirksam"),
            ("abgelehnt", "instrumentierung_im_test", "Abgelehnt"),
            ("kaputt", "instrumentierung_im_test", "Kaputt"),
            ("fehlt", "instrumentierung_die_es_nicht_gibt", "Fehlt"),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="src.observability"):
        assert observability.activate_instrumentations(object()) == ["wirksam"]

    assert "nicht aktiv: abgelehnt" in caplog.text
    assert "fehlgeschlagen: kaputt" in caplog.text
    assert "fehlt" not in caplog.text


@pytest.mark.parametrize(
    ("version", "erwartet"),
    [
        ("2.0.54", {"skip_dep_check": True}),
        ("2.1.1", {"skip_dep_check": True}),
        # Nicht selbst geprüft: Die Versionsprüfung der Instrumentierung entscheidet wieder
        ("2.2.0", {}),
        ("3.0.0b1", {}),
    ],
)
def test_versionspruefung_der_sqlalchemy_instrumentierung_entfaellt_nur_bis_zur_geprueften_version(
    monkeypatch: pytest.MonkeyPatch, version: str, erwartet: dict[str, bool]
) -> None:
    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "__version__", version)
    assert observability._instrument_options("sqlalchemy") == erwartet
    assert observability._instrument_options("httpx") == {}


SPAN_PRUEFSKRIPT = """
import logging
import sys

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

import sqlalchemy
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.observability import activate_instrumentations

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
print("AKTIV", ",".join(activate_instrumentations(provider)))

engine = sqlalchemy.create_engine("sqlite://")
with engine.connect() as verbindung:
    verbindung.execute(sqlalchemy.text("select 1"))

spans = exporter.get_finished_spans()
print("SPANS", ",".join(span.name.lower() for span in spans))
print("ANWEISUNGEN", sum("select 1" in (span.attributes or {}).values() for span in spans))
print("VERSION", sqlalchemy.__version__)
"""


def test_sqlalchemy_instrumentierung_erzeugt_spans_mit_der_installierten_version() -> None:
    """
    Nachweis gegen einen In-Memory-Exporter, in einem frischen Interpreter (die Instrumentierung verändert
    die Bibliotheken prozessweit). Mit SQLAlchemy 2.1 lehnte die Instrumentierung die Version ab: eine
    Fehlerzeile bei jedem Start, keine Spans – und trotzdem ``sqlalchemy`` in der Erfolgsmeldung.
    """
    ergebnis = subprocess.run(  # noqa: S603 – fester Aufruf im Test
        [sys.executable, "-c", SPAN_PRUEFSKRIPT],
        cwd=INGESTOR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    ausgabe = ergebnis.stdout + ergebnis.stderr
    assert ergebnis.returncode == 0, ausgabe[-2000:]
    assert "AKTIV httpx,asyncpg,sqlalchemy" in ergebnis.stdout, ausgabe[-2000:]
    assert "DependencyConflict" not in ausgabe, ausgabe[-2000:]
    assert "nicht aktiv" not in ausgabe, ausgabe[-2000:]
    assert "SPANS connect,select" in ergebnis.stdout, ausgabe[-2000:]
    assert "ANWEISUNGEN 1" in ergebnis.stdout, ausgabe[-2000:]

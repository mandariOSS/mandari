# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beobachtbarkeit des Ingestors: strukturierte Logs mit Kennungen und OpenTelemetry (Issue #162).

Gegenstück zu ``apps/common/observability.py`` im Django-Projekt:

- ``setup_logging()`` konfiguriert das Root-Logging (``LOG_FORMAT`` json|text, ``LOG_LEVEL``) und
  hängt ``request_id``/``trace_id``/``span_id`` an jeden Log-Record.
- ``setup_opentelemetry()`` aktiviert Tracing (httpx, asyncpg, SQLAlchemy), sobald
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` gesetzt ist; Dienstname ``OTEL_SERVICE_NAME`` (Standard
  ``mandari-ingestor``).
- ``trigger_context()`` übernimmt ``request_id``/``trace_id``/``span_id`` aus dem Redis-Trigger des
  Django-Admins, sodass ein ausgelöster Sync denselben ``trace_id`` trägt wie die Anfrage.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")

logger = logging.getLogger(__name__)

_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {
    "request_id",
    "trace_id",
    "span_id",
    "message",
    "asctime",
    "taskName",
}


def current_trace_context() -> tuple[str, str]:
    """(trace_id, span_id) des aktiven OpenTelemetry-Spans, sonst leere Strings."""
    try:
        from opentelemetry import trace
    except ImportError:
        return "", ""
    ctx = trace.get_current_span().get_span_context()
    if not ctx or not ctx.is_valid:
        return "", ""
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        trace_id, span_id = current_trace_context()
        record.request_id = getattr(record, "request_id", None) or request_id_var.get() or "-"
        record.trace_id = getattr(record, "trace_id", None) or trace_id or trace_id_var.get() or "-"
        record.span_id = span_id or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Eine JSON-Zeile je Eintrag – gleiches Format wie im Django-Projekt."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": os.environ.get("OTEL_SERVICE_NAME", "mandari-ingestor"),
            "request_id": getattr(record, "request_id", "-"),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_logging_ready = False


def setup_logging(log_format: str | None = None, log_level: str | None = None) -> None:
    """Root-Logging einrichten (idempotent). Text in der Entwicklung, JSON im Betrieb."""
    global _logging_ready
    if _logging_ready:
        return
    fmt = (log_format or os.environ.get("LOG_FORMAT") or "text").lower()
    level = (log_level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(ContextFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "{levelname} {asctime} {name} req={request_id} trace={trace_id} {message}",
                style="{",
            )
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Laute Bibliotheken bleiben auf WARNING, außer im DEBUG-Fall
    if level != "DEBUG":
        for noisy in ("httpx", "httpcore", "apscheduler", "sqlalchemy.engine"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    _logging_ready = True


_otel_ready = False


def setup_opentelemetry() -> bool:
    """Tracing aktivieren, wenn ``OTEL_EXPORTER_OTLP_ENDPOINT`` gesetzt und das SDK installiert ist."""
    global _otel_ready
    if _otel_ready:
        return True
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT gesetzt, aber das OpenTelemetry-SDK fehlt")
        return False

    provider = TracerProvider(
        resource=Resource.create({SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME", "mandari-ingestor")})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    instrumented: list[str] = []
    for name, module_name, class_name in (
        ("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
        ("asyncpg", "opentelemetry.instrumentation.asyncpg", "AsyncPGInstrumentor"),
        ("sqlalchemy", "opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor"),
    ):
        try:
            module = __import__(module_name, fromlist=[class_name])
            getattr(module, class_name)().instrument()
            instrumented.append(name)
        except ImportError:
            continue
        except Exception:  # noqa: BLE001 – fehlende Instrumentierung darf den Start nicht verhindern
            logger.exception("OpenTelemetry-Instrumentierung fehlgeschlagen: %s", name)
    _otel_ready = True
    logger.info("OpenTelemetry aktiv (%s): %s", endpoint, ", ".join(instrumented) or "keine Instrumentierung")
    return True


def parent_context(trace_id: str | None, span_id: str | None) -> Any:
    """OpenTelemetry-Kontext aus den Kennungen des Django-Triggers (None, wenn unbrauchbar)."""
    if not trace_id or not span_id:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

        span_context = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16),
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
    except (ImportError, ValueError):
        return None
    if not span_context.is_valid:
        return None
    return trace.set_span_in_context(NonRecordingSpan(span_context))


@contextmanager
def trigger_context(name: str, request_id: str | None, trace_id: str | None, span_id: str | None) -> Iterator[None]:
    """Log-Kennungen setzen und – wenn Tracing aktiv ist – einen Span unter dem Django-Trace öffnen."""
    tokens = (request_id_var.set(request_id or ""), trace_id_var.set(trace_id or ""))
    try:
        try:
            from opentelemetry import trace
        except ImportError:
            yield
            return
        tracer = trace.get_tracer("mandari-ingestor")
        with tracer.start_as_current_span(
            name,
            context=parent_context(trace_id, span_id),
            attributes={"mandari.request_id": request_id or "", "mandari.trigger": "django-admin"},
        ):
            yield
    finally:
        request_id_var.reset(tokens[0])
        trace_id_var.reset(tokens[1])

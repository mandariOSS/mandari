# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beobachtbarkeit: Request-Kennung, strukturierte Logs und OpenTelemetry (Issue #162).

- ``RequestIdMiddleware`` nimmt ``X-Request-ID`` vom Reverse Proxy an (oder erzeugt eine UUID),
  legt sie zusammen mit der Nutzer-Kennung in Kontextvariablen ab und gibt sie im Response-Header
  zurück. Alle Log-Zeilen innerhalb der Anfrage tragen die Kennung.
- ``RequestContextFilter`` hängt ``request_id``, ``user_id``, ``trace_id`` und ``span_id`` an jeden
  Log-Record; ``JsonFormatter`` schreibt eine JSON-Zeile je Eintrag (ohne personenbezogene Inhalte:
  nur die UUID des Nutzers, keine E-Mail, keine Namen).
- ``setup_opentelemetry()`` aktiviert die Auto-Instrumentierung (Django, psycopg, Redis, requests,
  httpx), sobald ``OTEL_EXPORTER_OTLP_ENDPOINT`` gesetzt ist. Ohne Endpoint passiert nichts; die
  Pakete sind optional.

Umgebungsvariablen: ``LOG_FORMAT`` (json|text), ``LOG_LEVEL``, ``OTEL_EXPORTER_OTLP_ENDPOINT``,
``OTEL_SERVICE_NAME`` (Standard ``mandari-web``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from django.http import HttpRequest, HttpResponse

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request-Kennung
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    return uuid.uuid4().hex


def current_request_id() -> str:
    return request_id_var.get()


def current_trace_context() -> tuple[str, str]:
    """(trace_id, span_id) des aktiven OpenTelemetry-Spans, sonst leere Strings."""
    try:
        from opentelemetry import trace
    except ImportError:
        return "", ""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx or not ctx.is_valid:
        return "", ""
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


class RequestIdMiddleware:
    """Setzt Request- und Nutzer-Kennung für Logs und gibt ``X-Request-ID`` zurück."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else new_request_id()
        request.request_id = request_id  # type: ignore[attr-defined]
        token = request_id_var.set(request_id)
        user_token = user_id_var.set("")
        try:
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            request_id_var.reset(token)
            user_id_var.reset(user_token)

    def process_view(self, request: HttpRequest, view_func: Any, view_args: Any, view_kwargs: Any) -> None:
        """Nach der Auth-Middleware: Nutzer-UUID (kein Name, keine E-Mail) in den Log-Kontext."""
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            user_id_var.set(str(user.pk))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class RequestContextFilter(logging.Filter):
    """Hängt Request-, Nutzer- und Trace-Kennung an jeden Log-Record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"
        record.user_id = user_id_var.get() or "-"
        trace_id, span_id = current_trace_context()
        record.trace_id = trace_id or "-"
        record.span_id = span_id or "-"
        return True


_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {
    "request_id",
    "user_id",
    "trace_id",
    "span_id",
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Eine JSON-Zeile je Log-Eintrag (für Loki, Elastic, CloudWatch …)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": os.environ.get("OTEL_SERVICE_NAME", "mandari-web"),
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
        }
        # Zusätzliche ``extra``-Felder mitnehmen, sofern JSON-fähig
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
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False)


def logging_config(*, debug: bool, log_format: str | None = None, log_level: str | None = None) -> dict[str, Any]:
    """Baut das ``LOGGING``-Dict für ``settings.py``.

    Produktion: JSON, INFO. Entwicklung: Text, DEBUG für die eigenen Apps. Beides per
    ``LOG_FORMAT``/``LOG_LEVEL`` übersteuerbar. Kein ``DEBUG`` in der Produktionskonfiguration.
    """
    fmt = (log_format or os.environ.get("LOG_FORMAT") or ("text" if debug else "json")).lower()
    app_level = (log_level or os.environ.get("LOG_LEVEL") or ("DEBUG" if debug else "INFO")).upper()
    if not debug and app_level == "DEBUG":
        app_level = "INFO"
    formatter = "json" if fmt == "json" else "text"
    app_loggers = ("insight_core", "insight_sync", "insight_search", "insight_ai", "apps")
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"request_context": {"()": "apps.common.observability.RequestContextFilter"}},
        "formatters": {
            "text": {
                "format": "{levelname} {asctime} {name} req={request_id} user={user_id} trace={trace_id} {message}",
                "style": "{",
            },
            "json": {"()": "apps.common.observability.JsonFormatter"},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": formatter,
                "filters": ["request_context"],
            },
        },
        "root": {"handlers": ["console"], "level": "INFO"},
        "loggers": {
            "django": {
                "handlers": ["console"],
                "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
                "propagate": False,
            },
            "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
            **{name: {"handlers": ["console"], "level": app_level, "propagate": False} for name in app_loggers},
        },
    }


# ---------------------------------------------------------------------------
# OpenTelemetry
# ---------------------------------------------------------------------------

_otel_ready = False


def setup_opentelemetry() -> bool:
    """Aktiviert Tracing, wenn ``OTEL_EXPORTER_OTLP_ENDPOINT`` gesetzt und das SDK installiert ist.

    Rückgabe: True, wenn instrumentiert wurde. Idempotent.
    """
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
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT gesetzt, aber das OpenTelemetry-SDK ist nicht installiert")
        return False

    resource = Resource.create({SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME", "mandari-web")})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    instrumented: list[str] = []
    candidates = (
        ("django", "opentelemetry.instrumentation.django", "DjangoInstrumentor"),
        ("psycopg", "opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor"),
        ("redis", "opentelemetry.instrumentation.redis", "RedisInstrumentor"),
        ("requests", "opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
        ("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
    )
    for name, module_name, class_name in candidates:
        try:
            module = __import__(module_name, fromlist=[class_name])
            getattr(module, class_name)().instrument()
            instrumented.append(name)
        except ImportError:
            continue
        except Exception:  # noqa: BLE001 – eine fehlende Instrumentierung darf den Start nicht verhindern
            logger.exception("OpenTelemetry-Instrumentierung fehlgeschlagen: %s", name)
    _otel_ready = True
    logger.info("OpenTelemetry aktiv (%s): %s", endpoint, ", ".join(instrumented) or "keine Instrumentierung")
    return True

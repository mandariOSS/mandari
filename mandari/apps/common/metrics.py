# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwendungsmetriken im Prometheus-Textformat (Issue #231, Teil 2).

- ``RequestMetricsMiddleware`` misst jede Anfrage: Antwortzeit-Histogramm, Anzahl und
  5xx-Fehler, jeweils je **View-Name** (``resolver_match.view_name``, z. B.
  ``session:meeting_detail``) und Statusklasse. Der konkrete Pfad wird bewusst nicht als
  Label verwendet: Pfade mit IDs würden die Kardinalität unbegrenzt wachsen lassen.
- ``metrics_view`` liefert ``/metrics/``. Erreichbar nur aus ``METRICS_ALLOWED_NETWORKS``
  oder mit ``Authorization: Bearer <METRICS_TOKEN>``; sonst 404, damit der Endpunkt von
  außen nicht einmal bestätigt wird.
- Sammler für Datenbankverbindungen (psycopg-Pool bzw. ``pg_stat_activity``), die
  Cache-Trefferquote (Redis ``INFO stats``) und die Warteschlange der Transkription werden
  beim Abruf ausgewertet, nicht laufend.
- Zähler für Mailversand (``apps.common.email``) und PDF-Erzeugung (``apps.common.pdf``).

Alle Werte gelten je Prozess und beginnen beim Start bei null.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import secrets
import time
from collections.abc import Callable, Iterator
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import Http404, HttpRequest, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from apps.accounts.two_factor_policy import IPNetwork, client_ip, ip_in_networks

logger = logging.getLogger(__name__)

METRICS_PATH = "/metrics/"

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

REQUEST_DURATION = Histogram(
    "mandari_http_request_duration_seconds",
    "Antwortzeit je View (Sekunden)",
    ["view", "status_class"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
REQUESTS = Counter("mandari_http_requests_total", "Anfragen je View und Statusklasse", ["view", "status_class"])
REQUEST_ERRORS = Counter("mandari_http_request_errors_total", "Antworten mit Status 5xx je View", ["view"])

# ---------------------------------------------------------------------------
# Hintergrund: Mailversand und Dokumenterzeugung
# ---------------------------------------------------------------------------

EMAILS = Counter("mandari_emails_total", "Versandversuche von E-Mails", ["result"])
PDF_DOCUMENTS = Counter("mandari_pdf_documents_total", "Erzeugte PDF-Dokumente", ["result"])
PDF_DURATION = Histogram(
    "mandari_pdf_generation_seconds",
    "Dauer der PDF-Erzeugung (Sekunden)",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


def view_label(request: HttpRequest) -> str:
    """Namensraum und URL-Name der aufgelösten View; ohne Treffer ``unresolved``."""
    match = getattr(request, "resolver_match", None)
    if match is None:
        return "unresolved"
    return str(match.view_name or "unresolved")


def status_class(status: int) -> str:
    return f"{status // 100}xx"


class RequestMetricsMiddleware:
    """Zählt Anfragen und misst Antwortzeiten je View-Name und Statusklasse."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Der Abruf der Metriken selbst würde bei ruhigen Installationen alle anderen
        # Views überdecken – er bleibt außen vor.
        if request.path == METRICS_PATH:
            return self.get_response(request)
        start = time.perf_counter()
        try:
            response = self.get_response(request)
        except Exception:
            self._beobachten(request, 500, start)
            raise
        self._beobachten(request, response.status_code, start)
        return response

    @staticmethod
    def _beobachten(request: HttpRequest, status: int, start: float) -> None:
        view = view_label(request)
        klasse = status_class(status)
        REQUEST_DURATION.labels(view=view, status_class=klasse).observe(time.perf_counter() - start)
        REQUESTS.labels(view=view, status_class=klasse).inc()
        if status >= 500:
            REQUEST_ERRORS.labels(view=view).inc()


# ---------------------------------------------------------------------------
# Sammler: Datenbank, Cache, Warteschlange
# ---------------------------------------------------------------------------


def db_pool_stats() -> dict[str, Any] | None:
    """Statistik des psycopg-Pools oder None, wenn kein Pool aktiv ist."""
    pool = getattr(connection, "pool", None)
    if pool is None:
        return None
    stats: dict[str, Any] = pool.get_stats()
    return stats


def db_open_connections() -> int | None:
    """Offene Verbindungen zur eigenen Datenbank laut ``pg_stat_activity`` (nur PostgreSQL)."""
    if connection.vendor != "postgresql":
        return None
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        row = cursor.fetchone()
    return int(row[0]) if row else None


class DatabaseCollector(Collector):
    def collect(self) -> Iterator[Metric]:
        try:
            stats = db_pool_stats()
        except Exception:  # noqa: BLE001 – ein Sammler darf den Abruf nie zum Absturz bringen
            logger.debug("Pool-Statistik nicht verfügbar", exc_info=True)
            stats = None
        if stats is not None:
            pool = GaugeMetricFamily(
                "mandari_db_pool_connections", "Verbindungen im psycopg-Pool je Zustand", labels=["state"]
            )
            verfuegbar = int(stats.get("pool_available", 0))
            pool.add_metric(["available"], verfuegbar)
            pool.add_metric(["in_use"], int(stats.get("pool_size", 0)) - verfuegbar)
            pool.add_metric(["min"], int(stats.get("pool_min", 0)))
            pool.add_metric(["max"], int(stats.get("pool_max", 0)))
            yield pool
            yield GaugeMetricFamily(
                "mandari_db_pool_requests_waiting",
                "Anfragen, die gerade auf eine Verbindung aus dem Pool warten",
                value=int(stats.get("requests_waiting", 0)),
            )
            return
        try:
            offen = db_open_connections()
        except Exception:  # noqa: BLE001
            logger.debug("pg_stat_activity nicht abfragbar", exc_info=True)
            offen = None
        if offen is not None:
            yield GaugeMetricFamily(
                "mandari_db_connections_open", "Offene Verbindungen zur Datenbank (pg_stat_activity)", value=offen
            )


def cache_stats() -> dict[str, int] | None:
    """``keyspace_hits``/``keyspace_misses`` des Redis-Servers; None ohne Redis-Backend.

    Die Werte gelten für den ganzen Redis-Server (auch Channel-Layer), nicht nur für den
    Django-Cache – für die Trefferquote als Trend reicht das.
    """
    backend = str(settings.CACHES["default"]["BACKEND"])
    if not backend.endswith("RedisCache"):
        return None
    intern = getattr(cache, "_cache", None)
    if intern is None or not hasattr(intern, "get_client"):
        return None
    info = intern.get_client().info("stats")
    return {"hits": int(info.get("keyspace_hits", 0)), "misses": int(info.get("keyspace_misses", 0))}


class CacheCollector(Collector):
    def collect(self) -> Iterator[Metric]:
        try:
            stats = cache_stats()
        except Exception:  # noqa: BLE001
            logger.debug("Redis-Statistik nicht verfügbar", exc_info=True)
            return
        if stats is None:
            return
        yield CounterMetricFamily(
            "mandari_cache_keyspace_hits", "Cache-Treffer (Redis keyspace_hits)", value=stats["hits"]
        )
        yield CounterMetricFamily(
            "mandari_cache_keyspace_misses", "Cache-Fehlzugriffe (Redis keyspace_misses)", value=stats["misses"]
        )
        gesamt = stats["hits"] + stats["misses"]
        yield GaugeMetricFamily(
            "mandari_cache_hit_ratio",
            "Cache-Trefferquote seit Redis-Start (0–1)",
            value=stats["hits"] / gesamt if gesamt else 0.0,
        )


def transcription_queue() -> dict[str, int]:
    from apps.minutes.models import JobStatus, TranscriptionJob

    return {
        status: TranscriptionJob.objects.filter(status=status).count()
        for status in (JobStatus.QUEUED, JobStatus.RUNNING)
    }


class QueueCollector(Collector):
    def collect(self) -> Iterator[Metric]:
        try:
            zaehler = transcription_queue()
        except Exception:  # noqa: BLE001 – z. B. Tabelle fehlt noch (Migration ausstehend)
            logger.debug("Warteschlange nicht abfragbar", exc_info=True)
            return
        familie = GaugeMetricFamily(
            "mandari_transcription_jobs", "Verarbeitungsaufträge der Transkription je Status", labels=["status"]
        )
        for status, anzahl in zaehler.items():
            familie.add_metric([status], anzahl)
        yield familie


def _register(collector: Collector) -> None:
    # ValueError: bereits registriert (Modul erneut importiert, z. B. in Tests)
    with contextlib.suppress(ValueError):
        REGISTRY.register(collector)


_register(DatabaseCollector())
_register(CacheCollector())
_register(QueueCollector())


# ---------------------------------------------------------------------------
# Endpunkt
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _parse_networks(raw: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(item, strict=False) for item in raw)


def metrics_networks() -> tuple[IPNetwork, ...]:
    return _parse_networks(tuple(getattr(settings, "METRICS_ALLOWED_NETWORKS", ()) or ()))


def access_allowed(request: HttpRequest) -> bool:
    """Bearer-Token (``METRICS_TOKEN``) oder Absender aus ``METRICS_ALLOWED_NETWORKS``."""
    token = str(getattr(settings, "METRICS_TOKEN", "") or "")
    auth = request.headers.get("Authorization", "")
    if token and auth.startswith("Bearer ") and secrets.compare_digest(auth[7:].strip(), token):
        return True
    return ip_in_networks(client_ip(request), metrics_networks())


@never_cache
@require_GET
def metrics_view(request: HttpRequest) -> HttpResponse:
    """Prometheus-Textformat; 404 statt 403, damit der Endpunkt nach außen unsichtbar bleibt."""
    if not access_allowed(request):
        raise Http404
    return HttpResponse(generate_latest(REGISTRY), content_type=CONTENT_TYPE_LATEST)

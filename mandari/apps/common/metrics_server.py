# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Metriken und Lebenszeichen von Prozessen ohne Webserver, etwa dem Worker (Issue #508).

``start_metrics_server(addr, port, health)`` startet einen kleinen HTTP-Server in einem
Hintergrundfaden:

- ``/metrics``: alle Metriken dieses Prozesses im Prometheus-Textformat, wie ``/metrics/`` der
  Anwendung. Zugriff wie dort nur aus ``METRICS_ALLOWED_NETWORKS`` oder mit
  ``Authorization: Bearer <METRICS_TOKEN>``, sonst 404. Ohne Reverse-Proxy davor zählt die Adresse
  der Gegenstelle; ``X-Forwarded-For`` wird nicht ausgewertet.
- ``/health``: 200 mit ``{"status": "ok", …}``, solange ``health()`` gesund meldet, sonst 503.
  Ohne Zugriffsbeschränkung, damit Probes von Kubernetes und Docker nicht an der Netzliste
  scheitern; die Antwort nennt nur Rollen und ob sie arbeiten.

Sammler, die beim Abruf die Datenbank befragen, laufen im Faden der Anfrage. Er gibt seine
Verbindung danach zurück, sonst wäre mit Verbindungspool der Platz verloren (Issue #344).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from wsgiref.types import StartResponse, WSGIEnvironment

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from apps.common.db_connections import releases_db_connections
from apps.common.metrics import access_allowed_from

logger = logging.getLogger(__name__)

#: Rückgabe von ``health()``: gesund? und Einzelheiten für die Antwort
HealthCheck = Callable[[], tuple[bool, Mapping[str, Any]]]


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class _Server6(_Server):
    address_family = socket.AF_INET6


class _StillerHandler(WSGIRequestHandler):
    """Kein Protokoll je Abruf (Prometheus fragt alle paar Sekunden)."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 – Signatur der Basisklasse
        return


@releases_db_connections
def _messen() -> bytes:
    return generate_latest(REGISTRY)


def _antwort(
    start_response: StartResponse, status: str, inhalt: bytes, art: str = "text/plain; charset=utf-8"
) -> Iterable[bytes]:
    start_response(status, [("Content-Type", art), ("Cache-Control", "no-store")])
    return [inhalt]


def metrics_app(health: HealthCheck) -> Callable[[WSGIEnvironment, StartResponse], Iterable[bytes]]:
    """WSGI-Anwendung mit ``/metrics`` und ``/health`` (siehe Moduldokumentation)."""

    def app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        pfad = str(environ.get("PATH_INFO") or "/").rstrip("/")
        if environ.get("REQUEST_METHOD") not in ("GET", "HEAD"):
            return _antwort(start_response, "405 Method Not Allowed", b"Method Not Allowed")
        if pfad == "/health":
            try:
                gesund, einzelheiten = health()
            except Exception:  # noqa: BLE001 – eine gescheiterte Prüfung ist ein Befund, kein Absturz
                logger.exception("Worker: Lebenszeichen nicht prüfbar")
                gesund, einzelheiten = False, {}
            inhalt = json.dumps({"status": "ok" if gesund else "error", **einzelheiten}).encode()
            status = "200 OK" if gesund else "503 Service Unavailable"
            return _antwort(start_response, status, inhalt, "application/json")
        if pfad == "/metrics" and access_allowed_from(
            str(environ.get("REMOTE_ADDR", "")), str(environ.get("HTTP_AUTHORIZATION", ""))
        ):
            return _antwort(start_response, "200 OK", _messen(), CONTENT_TYPE_LATEST)
        # 404 auch ohne Zugriffsrecht: Der Endpunkt soll von außen nicht einmal bestätigt werden
        return _antwort(start_response, "404 Not Found", b"Not Found")

    return app


@dataclass
class MetricsServer:
    """Laufender Server; ``close()`` beendet ihn."""

    server: WSGIServer
    thread: threading.Thread

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_metrics_server(addr: str, port: int, health: HealthCheck) -> MetricsServer:
    """Startet den Server auf ``addr:port`` in einem Hintergrundfaden (Port 0: frei gewählt, für Tests).

    Wirft ``OSError``, wenn der Port belegt ist.
    """
    klasse = _Server6 if ":" in addr else _Server
    server = make_server(addr, port, metrics_app(health), server_class=klasse, handler_class=_StillerHandler)
    faden = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="metrics-http")
    faden.daemon = True
    faden.start()
    return MetricsServer(server, faden)

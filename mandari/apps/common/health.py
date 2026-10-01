# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Getrennte Gesundheitsprüfungen (Issue #231, Teil 1).

- ``/health/live/``  (Liveness): Antwortet der Prozess? Keine Abhängigkeiten. Ein Fehler
  hier heißt „Prozess neu starten“. Einzige Ausnahme ist ein festgefahrener
  Datenbank-Pool (Issue #344): Sind alle Verbindungen seit einer Minute verliehen, ohne
  dass eine zurückkommt, und ist die Datenbank selbst erreichbar, dann ist der *Prozess*
  kaputt, und nur ein Neustart hilft. Ist die Datenbank weg, bleibt die Liveness grün.
- ``/health/ready/`` (Readiness): Kann die Instanz Anfragen bedienen? Prüft Datenbank,
  Cache (Redis), Elasticsearch und den Medienspeicher, jede Prüfung mit eigenem
  Zeitlimit. Ein Fehler hier heißt „keine Anfragen mehr zuteilen“, nicht „neu starten“.
  Ein Ausfall von Redis oder Elasticsearch macht die Readiness rot, die Liveness bleibt
  davon unberührt.
- ``/health/`` bleibt für bestehende Healthchecks (Compose, Statusseite) erhalten.

Über ``HEALTH_READY_OPTIONAL`` (kommagetrennt, z. B. ``elasticsearch``) lassen sich
Prüfungen als optional erklären: Sie werden weiter gemeldet, machen die Antwort aber nicht
zu 503. Das ist für Installationen ohne Bürgerportal gedacht, deren Suche nicht kritisch ist.

Der **Worker** (``manage.py events_worker``, Issue #509) ist immer optional: Fehlt er, obwohl
die Installation ihn braucht (``apps.events.presence.required_roles``), melden ``/health/``
und ``/health/ready/`` „degraded“, die Anwendung bleibt aber in Betrieb. Braucht sie keinen,
ändert sich nichts.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.http import HttpRequest, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from apps.common.db_connections import pool_starved, releases_db_connections

logger = logging.getLogger(__name__)

#: Zeitlimit je Prüfung in Sekunden; die Antwort insgesamt bleibt unter der Probe-Grenze (5 s).
CHECK_TIMEOUT = 2.0


@dataclass(frozen=True)
class Finding:
    """Ergebnis einer Prüfung, die ohne Ausnahme „nicht in Ordnung“ meldet; ``detail`` ist ein fester Text."""

    ok: bool
    detail: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    duration_ms: int


@releases_db_connections  # läuft in einem eigenen Thread, der die Verbindung sonst nie zurückgibt (#344)
def check_database() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return "verbunden"


def check_cache() -> str:
    schluessel = "health:ready:ping"
    cache.set(schluessel, "1", 5)
    if cache.get(schluessel) != "1":
        raise RuntimeError("Wert nicht lesbar")
    return settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1]


def check_elasticsearch() -> str:
    url = str(getattr(settings, "ELASTICSEARCH_URL", "")).rstrip("/")
    if not url:
        return "nicht konfiguriert"
    with urllib.request.urlopen(f"{url}/_cluster/health", timeout=CHECK_TIMEOUT) as antwort:  # noqa: S310 - feste URL aus den Settings
        import json

        daten = json.loads(antwort.read().decode("utf-8"))
    status = str(daten.get("status", "unbekannt"))
    if status == "red":
        raise RuntimeError("Cluster-Status red")
    return f"Cluster {status}"


def check_storage() -> str:
    name = default_storage.save("health/ready-probe.txt", ContentFile(b"ok"))
    try:
        if not default_storage.exists(name):
            raise RuntimeError("Datei nach dem Schreiben nicht vorhanden")
    finally:
        default_storage.delete(name)
    return "beschreibbar"


def worker_finding() -> Finding:
    """Läuft ein Worker für alle Rollen, die die Installation braucht? Ohne Bedarf immer in Ordnung.

    Ohne Bedarf fragt die Prüfung die Datenbank gar nicht erst (``/health/`` wird häufig abgerufen).
    """
    from apps.events.presence import required_roles, worker_status

    bedarf = required_roles()
    if not bedarf:
        return Finding(True, "nicht erforderlich")
    status = worker_status(required=bedarf)
    if status.degraded:
        return Finding(False, f"kein Worker für {status.missing_summary()}")
    return Finding(True, f"{len(status.workers)} Worker")


@releases_db_connections  # läuft in einem eigenen Thread (#344)
def check_worker() -> Finding:
    return worker_finding()


def worker_state() -> str:
    """Für ``/health/``: ``ok``, ``fehlt``, ``nicht_erforderlich`` oder ``unbekannt`` (Tabelle fehlt, Datenbank weg)."""
    try:
        from apps.events.presence import required_roles, worker_status

        bedarf = required_roles()
        if not bedarf:
            return "nicht_erforderlich"  # ohne Datenbankabfrage
        status = worker_status(required=bedarf)
    except Exception:  # noqa: BLE001 – der alte Endpunkt meldet nur, er bricht nicht ab
        logger.debug("Worker-Zustand nicht ermittelbar", exc_info=True)
        return "unbekannt"
    return "fehlt" if status.degraded else "ok"


CHECKS: dict[str, Callable[[], str | Finding]] = {
    "database": check_database,
    "cache": check_cache,
    "elasticsearch": check_elasticsearch,
    "storage": check_storage,
    "worker": check_worker,
}
#: Prüfungen, die nie 503 auslösen (nur „degraded“)
ALWAYS_OPTIONAL = frozenset({"worker"})


def _ergebnis(name: str, start: float, future: Future[str | Finding]) -> CheckResult:
    verbleibend = max(0.0, CHECK_TIMEOUT - (time.monotonic() - start))
    try:
        ergebnis = future.result(timeout=verbleibend)
        ok, detail = (ergebnis.ok, ergebnis.detail) if isinstance(ergebnis, Finding) else (True, ergebnis)
    except FutureTimeoutError:
        ok, detail = False, f"Zeitlimit {CHECK_TIMEOUT:g} s überschritten"
    except Exception as exc:  # jede Ausnahme ist hier ein Prüfergebnis, kein Absturz
        # Nur die Art des Fehlers: Der Endpunkt ist erreichbar, Ausnahmetexte können Adressen oder
        # Zugangsdaten enthalten. Die Einzelheiten stehen im Protokoll.
        logger.warning("Readiness-Prüfung %s fehlgeschlagen", name, exc_info=exc)
        ok, detail = False, f"fehlgeschlagen ({type(exc).__name__})"
    return CheckResult(name, ok, detail, int((time.monotonic() - start) * 1000))


def optional_checks() -> set[str]:
    roh = os.environ.get("HEALTH_READY_OPTIONAL", "")
    return {teil.strip() for teil in roh.split(",") if teil.strip()} | ALWAYS_OPTIONAL


def run_readiness_checks() -> list[CheckResult]:
    """Alle Prüfungen parallel; eine hängende Prüfung verzögert die Antwort höchstens um CHECK_TIMEOUT."""
    executor = ThreadPoolExecutor(max_workers=len(CHECKS), thread_name_prefix="health")
    try:
        gestartet = {name: (time.monotonic(), executor.submit(pruefung)) for name, pruefung in CHECKS.items()}
        return [_ergebnis(name, start, future) for name, (start, future) in gestartet.items()]
    finally:
        # Nicht auf hängende Threads warten – sie laufen im Hintergrund aus.
        executor.shutdown(wait=False, cancel_futures=True)


@never_cache
@require_GET
def live(request: HttpRequest) -> JsonResponse:
    """Liveness: Der Prozess nimmt Anfragen an und sein Datenbank-Pool ist nicht festgefahren."""
    if pool_starved():
        return JsonResponse({"status": "error", "detail": "Datenbank-Pool festgefahren"}, status=503)
    return JsonResponse({"status": "ok"})


@never_cache
@require_GET
def ready(request: HttpRequest) -> JsonResponse:
    """Readiness: Abhängigkeiten geprüft; 503, wenn eine nicht-optionale Prüfung scheitert."""
    ergebnisse = run_readiness_checks()
    optional = optional_checks()
    fehlgeschlagen = [r.name for r in ergebnisse if not r.ok]
    kritisch = [name for name in fehlgeschlagen if name not in optional]
    status = "ok" if not fehlgeschlagen else ("degraded" if not kritisch else "error")
    if kritisch:
        logger.warning("Readiness fehlgeschlagen: %s", ", ".join(kritisch))
    return JsonResponse(
        {
            "status": status,
            "checks": {
                r.name: {"ok": r.ok, "detail": r.detail, "ms": r.duration_ms, "optional": r.name in optional}
                for r in ergebnisse
            },
        },
        status=200 if not kritisch else 503,
    )

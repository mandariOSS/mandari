# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kennzahlen und Statusprüfung des Abrufs der RIS-Dateien (Issue #919, ADR Dokumentkette Abschnitt 10).

- ``mandari_files_fetch_queued``: Dateien, die abgelegt werden und deren Abruf jetzt ansteht (``none``, fällige
  ``retry``/``error``, verfallene Beanspruchung).
- ``mandari_files_fetch_retry_due``: davon fällige Wiederholungen.
- ``mandari_files_fetch_errors_total`` (je Quelle und Fehlercode): Zähler in ``hub.ris.abruf``, je Prozess.

Die beiden Bestandszahlen werden beim Abruf von ``/metrics/`` gemessen und eine Minute im gemeinsamen Cache
gehalten (eine Abfrage über den Index des Zustands, nicht bei jedem Abruf neu).

Prüfung ``dokumentabruf`` in ``/health/worker/``: scheitert, sobald fällige Wiederholungen länger als
``DOCUMENT_FETCH_RETRY_ALERT_HOURS`` (Standard 6) liegen – dann arbeitet ``cache_files`` sie nicht ab (Zeitplan
steht, Platte voll, Rückstau). Quellen in Schonung zählen nicht mit; mit abgeschaltetem Zeitplan
``befehl:cache_files`` gibt es nichts zu prüfen.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any, Final

from django.conf import settings
from django.core.cache import cache
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from apps.events.status import Check

from . import abruf

logger = logging.getLogger(__name__)

CACHE_KEY: Final = "dokumentkette:abruf:kennzahlen:v1"
CACHE_SECONDS: Final = 60
#: Zeitplan, der Wiederholungen abarbeitet (bis Etappe 3 der Dokumentkette)
SCHEDULE: Final = "befehl:cache_files"


def counts() -> dict[str, int]:
    """Wartende Abrufe und fällige Wiederholungen, eine Minute zwischengespeichert."""
    try:
        gespeichert = cache.get(CACHE_KEY)
    except Exception:  # noqa: BLE001 - ohne Cache wird eben jedes Mal gemessen
        gespeichert = None
    if isinstance(gespeichert, dict):
        return gespeichert
    zahlen = abruf.counts()
    with contextlib.suppress(Exception):
        cache.set(CACHE_KEY, zahlen, CACHE_SECONDS)
    return zahlen


class AbrufCollector(Collector):
    """Misst erst beim Abruf von ``/metrics/`` (leere Beschreibung, siehe ``apps.common.metrics``)."""

    def describe(self) -> Iterator[Metric]:
        return iter(())

    def collect(self) -> Iterator[Metric]:
        try:
            zahlen = counts()
        except Exception:  # noqa: BLE001 – ein Sammler darf den Abruf nie zum Absturz bringen
            logger.debug("Kennzahlen des Abrufs nicht verfügbar", exc_info=True)
            return
        yield GaugeMetricFamily(
            "mandari_files_fetch_queued",
            "RIS-Dateien, deren Abruf ansteht (abzulegen, fällig)",
            value=zahlen.get("queued", 0),
        )
        yield GaugeMetricFamily(
            "mandari_files_fetch_retry_due",
            "Fällige Wiederholungen des Abrufs von RIS-Dateien",
            value=zahlen.get("retry_due", 0),
        )


def alert_hours() -> float:
    return float(getattr(settings, "DOCUMENT_FETCH_RETRY_ALERT_HOURS", 6))


def check_fetch() -> Check:
    """Prüfung ``dokumentabruf`` für ``/health/worker/``; nur Zahlen, keine Inhalte."""
    from apps.events.schedule import disabled_schedules

    if SCHEDULE in disabled_schedules():
        return Check(True, "Zeitplan abgeschaltet")
    ueberfaellig = abruf.overdue_retries()
    return Check(
        ueberfaellig == 0,
        f"{ueberfaellig} fällige Wiederholungen länger als {alert_hours():g} h offen",
    )


_COLLECTOR: Any = None


def register() -> None:
    """Sammler und Statusprüfung registrieren (einmal je Prozess, aus ``AppConfig.ready``)."""
    global _COLLECTOR
    from apps.events.status import register_check

    register_check("dokumentabruf", check_fetch)
    if _COLLECTOR is None:
        _COLLECTOR = AbrufCollector()
        # ValueError: bereits registriert (Modul erneut importiert, z. B. in Tests)
        with contextlib.suppress(ValueError):
            REGISTRY.register(_COLLECTOR)

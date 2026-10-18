# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kennzahlen und Statusprüfung der Texterkennung aus der Ablage (Issue #919, ADR Dokumentkette Abschnitt 10).

- ``mandari_files_stored_without_text``: abgelegte Inhalte (``local_status = ok``), deren Erkennung wartet oder
  läuft – der Rückstand der Erkennung.
- ``mandari_files_text_outdated``: abgelegte Inhalte mit Text aus einer älteren Erkennungsversion (bzw. mit
  angeforderter Neuerkennung); der Zeitplan plant sie nach den wartenden schrittweise neu ein. Kein Alarm.

Beide werden beim Abruf von ``/metrics/`` gemessen und eine Minute im gemeinsamen Cache gehalten (eine Abfrage).
Gelöschte, gesperrte und nach #787 geleerte Dateien zählen nicht.

Prüfung ``dokumenttext`` in ``/health/worker/``: scheitert (Warnung), sobald ein abgelegter Inhalt länger als
``TEXT_EXTRACTION_BACKLOG_ALERT_HOURS`` (Standard 24) seit dem Ablegen auf seinen Text wartet – dann steht die
Erkennung oder kommt nicht nach. Nach ``dokumentkette nacharbeiten`` erwartet, bis der Rückstand abgearbeitet ist.
Nur mit ``TEXT_EXTRACTION_RUNNER=worker``; mit ``ingestor`` meldet sie „in Ordnung“ (die Kennzahlen zählen weiter).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any, Final

from django.core.cache import cache
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from apps.events.status import Check
from insight_core.services.text_extraction_job import runner_is_worker

from . import erkennung

logger = logging.getLogger(__name__)

CACHE_KEY: Final = "dokumentkette:erkennung:kennzahlen:v1"
CACHE_SECONDS: Final = 60


def counts() -> dict[str, int]:
    """Abgelegte Inhalte ohne Text und mit veraltetem Text, eine Minute zwischengespeichert."""
    try:
        gespeichert = cache.get(CACHE_KEY)
    except Exception:  # noqa: BLE001 - ohne Cache wird eben jedes Mal gemessen
        gespeichert = None
    if isinstance(gespeichert, dict):
        return gespeichert
    zahlen = erkennung.counts()
    with contextlib.suppress(Exception):
        cache.set(CACHE_KEY, zahlen, CACHE_SECONDS)
    return zahlen


class ErkennungCollector(Collector):
    """Misst erst beim Abruf von ``/metrics/`` (leere Beschreibung, siehe ``apps.common.metrics``)."""

    def describe(self) -> Iterator[Metric]:
        return iter(())

    def collect(self) -> Iterator[Metric]:
        try:
            zahlen = counts()
        except Exception:  # noqa: BLE001 – ein Sammler darf den Abruf nie zum Absturz bringen
            logger.debug("Kennzahlen der Texterkennung nicht verfügbar", exc_info=True)
            return
        yield GaugeMetricFamily(
            "mandari_files_stored_without_text",
            "Abgelegte RIS-Dateien, deren Texterkennung wartet oder läuft",
            value=zahlen.get("stored_without_text", 0),
        )
        yield GaugeMetricFamily(
            "mandari_files_text_outdated",
            "Abgelegte RIS-Dateien mit Text aus einer älteren Version der Texterkennung",
            value=zahlen.get("text_outdated", 0),
        )


def check_text() -> Check:
    """
    Prüfung ``dokumenttext`` für ``/health/worker/``; nur Zahlen, keine Inhalte. Scharf erst mit
    ``TEXT_EXTRACTION_RUNNER=worker``: Vorher erkennt der Ingestor, und ein Altbestand ohne Text schlüge schon mit
    dem Deploy an (wie ``dokumentabruf`` am Zeitplan hängt).
    """
    if not runner_is_worker():
        return Check(True, "Texterkennung im Ingestor (TEXT_EXTRACTION_RUNNER ist nicht worker)")
    ueberfaellig = erkennung.overdue_without_text()
    return Check(
        ueberfaellig == 0,
        f"{ueberfaellig} abgelegte Dokumente warten länger als {erkennung.backlog_alert_hours():g} h auf ihren Text",
    )


_COLLECTOR: Any = None


def register() -> None:
    """Sammler und Statusprüfung registrieren (einmal je Prozess, aus ``AppConfig.ready``)."""
    global _COLLECTOR
    from apps.events.status import register_check

    register_check("dokumenttext", check_text)
    if _COLLECTOR is None:
        _COLLECTOR = ErkennungCollector()
        # ValueError: bereits registriert (Modul erneut importiert, z. B. in Tests)
        with contextlib.suppress(ValueError):
            REGISTRY.register(_COLLECTOR)

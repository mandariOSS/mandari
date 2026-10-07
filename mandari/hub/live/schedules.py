# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne der Live-Übertragungen (``apps.events.schedule``, Issue #915).

- ``live_status_abfragen``: jede Minute, nur mit ``LIVE_UEBERTRAGUNG_AKTIV`` eingeplant. Fragt für jede aktive Quelle
  den Status der aktuellen Übertragung ab, führt die Zustände und reiht bei laufender Übertragung einen Leseauftrag
  in die Warteschlange ``live`` ein (``hub.live.services``). Verpasste Termine werden ausgelassen.
- ``live_protokoll_aufraeumen``: täglich um 04:20 Uhr (``TIME_ZONE``) Protokolleinträge nach
  ``LIVE_PROTOKOLL_TAGE`` (Standard 90) löschen. Idempotent.
"""

from __future__ import annotations

from django.conf import settings
from django.tasks import task

from apps.events.schedule import Catchup, cron, every


@task(queue_name="live")
def live_status_abfragen() -> int:
    """Status aller aktiven Quellen abfragen; Rückgabe: Zahl der Quellen."""
    from .services import alle_abfragen

    return alle_abfragen()


# Nur eingeplant, wenn die Erkennung eingeschaltet ist; sonst entstünde jede Minute ein leerer Auftrag
if getattr(settings, "LIVE_UEBERTRAGUNG_AKTIV", False):
    live_status_abfragen = every(minutes=1, catchup=Catchup.AUSLASSEN)(live_status_abfragen)


@cron("20 4 * * *")
@task
def live_protokoll_aufraeumen() -> int:
    """Löscht Protokolleinträge nach der Aufbewahrungsfrist; liefert ihre Anzahl."""
    from .services import protokoll_aufraeumen

    return protokoll_aufraeumen()

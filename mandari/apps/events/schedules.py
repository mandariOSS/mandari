# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne der Plattform (``apps.events.schedule``); der Scheduler lädt dieses Modul beim Start.

- ``idempotenzschluessel_aufraeumen``: täglich um 03:40 Uhr (``TIME_ZONE``) Idempotenzschlüssel nach
  der Aufbewahrungsfrist löschen (``EVENTS_IDEMPOTENCY_RETENTION_DAYS``, Standard 30 Tage). Der
  Auftrag ist idempotent; ein verpasster Termin wird einmal nachgeholt.
"""

from __future__ import annotations

from django.tasks import task

from .idempotency import purge_expired
from .schedule import cron


@cron("40 3 * * *")
@task
def idempotenzschluessel_aufraeumen() -> int:
    """Löscht Idempotenzschlüssel nach der Aufbewahrungsfrist; liefert ihre Anzahl."""
    return purge_expired()

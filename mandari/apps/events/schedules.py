# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne der Plattform (``apps.events.schedule``); der Scheduler lädt dieses Modul beim Start.

- ``idempotenzschluessel_aufraeumen``: täglich um 03:40 Uhr (``TIME_ZONE``) Idempotenzschlüssel nach
  der Aufbewahrungsfrist löschen (``EVENTS_IDEMPOTENCY_RETENTION_DAYS``, Standard 30 Tage). Der
  Auftrag ist idempotent; ein verpasster Termin wird einmal nachgeholt.
- ``auftraege_aufraeumen``: täglich um 03:50 Uhr beendete Aufträge nach ihrer Frist löschen
  (erledigte 14 Tage, tote und endgültig fehlgeschlagene 90 Tage, ADR A4). Ohne das wüchse
  ``events_task`` mit jedem Lauf eines Zeitplans.
"""

from __future__ import annotations

from django.tasks import task

from .idempotency import purge_expired
from .schedule import cron
from .tasks_backend import purge_finished


@cron("40 3 * * *")
@task
def idempotenzschluessel_aufraeumen() -> int:
    """Löscht Idempotenzschlüssel nach der Aufbewahrungsfrist; liefert ihre Anzahl."""
    return purge_expired()


@cron("50 3 * * *")
@task
def auftraege_aufraeumen() -> int:
    """Löscht beendete Aufträge nach ihrer Aufbewahrungsfrist; liefert ihre Anzahl."""
    return purge_finished()

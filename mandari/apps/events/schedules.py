# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne der Plattform (``apps.events.schedule``); der Scheduler lädt dieses Modul beim Start.

- ``idempotenzschluessel_aufraeumen``: täglich um 03:40 Uhr (``TIME_ZONE``) Idempotenzschlüssel nach
  der Aufbewahrungsfrist löschen (``EVENTS_IDEMPOTENCY_RETENTION_DAYS``, Standard 30 Tage). Der
  Auftrag ist idempotent; ein verpasster Termin wird einmal nachgeholt.
- ``auftraege_aufraeumen``: täglich um 03:50 Uhr beendete Aufträge nach ihrer Frist löschen
  (erledigte 14 Tage, tote und endgültig fehlgeschlagene 90 Tage, ADR A4; einstellbar, ``task_retention``). Ohne das wüchse
  ``events_task`` mit jedem Lauf eines Zeitplans.
- ``befehl:events_purge``: täglich um 04:10 Uhr das Journal nach der Frist aufräumen
  (``manage.py events_purge --nur journal``, ``apps.events.aufbewahrung``) als eigener Prozess im Worker.
  Nur mit ``EVENTS_JOURNAL_PURGE_ENABLED`` (Standard aus); ein Lauf hört nach 50 Minuten auf, der nächste
  setzt fort.
"""

from __future__ import annotations

from django.conf import settings
from django.tasks import task

from .idempotency import purge_expired
from .schedule import ScheduleRegistry, cron
from .tasks_backend import purge_finished
from .verwaltungsbefehle import befehl_als_zeitplan

#: Zeitgrenze des Laufs (Sekunden); der Befehl selbst hört vorher auf und gibt den Platz frei
JOURNAL_PURGE_SECONDS = 3000


def registrieren(ziel: ScheduleRegistry | None = None) -> None:
    """Registriert die abschaltbaren Zeitpläne nach den Einstellungen (ohne ``ziel``: Register des Prozesses)."""
    if getattr(settings, "EVENTS_JOURNAL_PURGE_ENABLED", False):
        befehl_als_zeitplan(
            "events_purge",
            crontab="10 4 * * *",
            argumente=["--nur", "journal", "--max-seconds", str(JOURNAL_PURGE_SECONDS)],
            zeitgrenze=JOURNAL_PURGE_SECONDS + 300,
            ziel=ziel,
        )


registrieren()


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

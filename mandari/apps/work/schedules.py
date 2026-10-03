# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne der Fraktionssitzungen (``apps.events.schedule``, Issue #515).

Bis Issue #515 liefen diese Läufe in einem Faden im Webprozess (``insight_sync/daemon.py``). Jeder ist
idempotent und durch eine Cache-Sperre gegen parallele Läufe geschützt:

- ``fraktionserinnerungen_senden``: alle ``FACTION_REMINDER_INTERVAL_MINUTES`` (Standard 15) Minuten
  Erinnerungen 48 Stunden vor Sitzungsbeginn (Issue #59).
- ``fraktionseinladungen_senden``: alle ``FACTION_INVITATION_INTERVAL_MINUTES`` (Standard 15) Minuten
  automatische Einladungen und Freigabe-Hinweise (Issue #62).
- ``fraktionssitzungen_erzeugen``: alle ``FACTION_SCHEDULE_INTERVAL_MINUTES`` (Standard 60) Minuten
  Sitzungen aus Sitzungsreihen (Issue #61).
"""

from __future__ import annotations

from django.conf import settings
from django.tasks import task

from apps.events.schedule import every

from .faction.generation import run_faction_schedule_pass
from .faction.invitations import run_faction_invitation_pass
from .faction.services import run_faction_reminder_pass


def _minuten(name: str, standard: int) -> int:
    return max(1, int(getattr(settings, name, standard)))


@every(minutes=_minuten("FACTION_REMINDER_INTERVAL_MINUTES", 15))
@task
def fraktionserinnerungen_senden() -> None:
    """Erinnert an Fraktionssitzungen (einmal je Sitzung)."""
    run_faction_reminder_pass()


@every(minutes=_minuten("FACTION_INVITATION_INTERVAL_MINUTES", 15))
@task
def fraktionseinladungen_senden() -> None:
    """Versendet fällige Einladungen und Freigabe-Hinweise zu Fraktionssitzungen."""
    run_faction_invitation_pass()


@every(minutes=_minuten("FACTION_SCHEDULE_INTERVAL_MINUTES", 60))
@task
def fraktionssitzungen_erzeugen() -> None:
    """Erzeugt Fraktionssitzungen aus Sitzungsreihen."""
    run_faction_schedule_pass()

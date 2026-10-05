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
- ``ris_verknuepfungen_abgleichen``: alle ``WORK_RIS_RELINK_INTERVAL_MINUTES`` (Standard 15) Minuten Work-Daten
  nach einer Neuveröffentlichung im RIS an den Nachfolger des Tagesordnungspunkts bzw. der Vorlage hängen
  (Issue #547); tut nichts, solange ``WORK_RIS_RELINK`` auf ``aus`` (Standard) steht.
- ``fraktionsprotokolle_versenden``: alle ``FACTION_PROTOCOL_INTERVAL_MINUTES`` (Standard 15) Minuten
  automatischer Protokollversand, falls die Organisation ihn eingeschaltet hat (Issue #871).

Die Erinnerung zum Eintragen von TOPs und die automatische Einladung zum festen Zeitpunkt einer Reihe
(Issue #871) laufen im Einladungslauf mit.
"""

from __future__ import annotations

from django.conf import settings
from django.tasks import task

from apps.events.schedule import every

from .faction.generation import run_faction_schedule_pass
from .faction.invitations import run_faction_invitation_pass
from .faction.protocol_dispatch import run_faction_protocol_pass
from .faction.services import run_faction_reminder_pass
from .ris.verknuepfungen import abgleichen


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


@every(minutes=_minuten("WORK_RIS_RELINK_INTERVAL_MINUTES", 15))
@task
def ris_verknuepfungen_abgleichen() -> dict[str, object]:
    """Hängt Work-Daten nach einer Neuveröffentlichung im RIS um (``WORK_RIS_RELINK``)."""
    return abgleichen().as_dict()


@every(minutes=_minuten("FACTION_PROTOCOL_INTERVAL_MINUTES", 15))
@task
def fraktionsprotokolle_versenden() -> None:
    """Versendet fällige Protokolle von Fraktionssitzungen (nur wenn die Organisation es eingeschaltet hat)."""
    run_faction_protocol_pass()

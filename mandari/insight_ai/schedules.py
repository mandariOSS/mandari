# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne der KI-Funktionen (``apps.events.schedule``).

- ``zusammenfassungen_einreichen``: alle 30 Minuten einen Message Batch mit Vorgängen ohne Zusammenfassung bei der
  Claude API einreichen, sofern sie für die Zusammenfassungen eingerichtet, der Batch eingeschaltet, kein Batch offen
  und Budget übrig ist (``insight_ai.services.zusammenfassung_batch.einreichen``). Sonst endet der Lauf ohne Aufruf.
- ``zusammenfassungen_abholen``: alle 10 Minuten offene Batches prüfen und beendete übernehmen. Ohne offenen Batch
  bleibt es bei einer Datenbankabfrage.

Beide sind idempotent; abschaltbar mit ``EVENTS_SCHEDULES_DISABLED``.
"""

from __future__ import annotations

from django.tasks import task

from apps.events.schedule import every


@every(minutes=30)
@task
def zusammenfassungen_einreichen() -> int:
    """Batch mit Zusammenfassungen einreichen; Rückgabe: Zahl der Anfragen."""
    from .services.zusammenfassung_batch import einreichen

    return einreichen()


@every(minutes=10)
@task
def zusammenfassungen_abholen() -> int:
    """Ergebnisse beendeter Batches übernehmen; Rückgabe: Zahl der gespeicherten Zusammenfassungen."""
    from .services.zusammenfassung_batch import abholen

    return abholen()

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne der KI-Funktionen (``apps.events.schedule``).

- ``zusammenfassungen_nacharbeiten``: täglich um 20:00 Uhr (``TIME_ZONE``) am Abruf gescheiterte Anlagen der
  Kommunen im Tagesbatch neu einreihen (``zusammenfassung_batch.nacharbeiten``), damit die Texterkennung über Nacht
  läuft und ihr Text zum nächsten Tagesbatch vorliegt (Issue #1220).
- ``zusammenfassungen_einreichen``: täglich um 06:30 Uhr der Tagesbatch: Vorgänge der ausgewählten Kommunen ohne
  Zusammenfassung, deren Text vollständig erkannt ist, als Message Batch bei der Claude API einreichen, sofern sie
  für die Zusammenfassungen eingerichtet, der Batch eingeschaltet, kein Batch offen und Budget übrig ist
  (``insight_ai.services.zusammenfassung_batch.einreichen``). Sonst endet der Lauf ohne Aufruf. Ein verpasster
  Termin wird einmal nachgeholt.
- ``zusammenfassungen_abholen``: alle 10 Minuten offene Batches prüfen und beendete übernehmen. Ohne offenen Batch
  bleibt es bei einer Datenbankabfrage.

Alle sind idempotent; abschaltbar mit ``EVENTS_SCHEDULES_DISABLED``.
"""

from __future__ import annotations

from django.tasks import task

from apps.events.schedule import cron, every


@cron("0 20 * * *")
@task
def zusammenfassungen_nacharbeiten() -> int:
    """Am Abruf gescheiterte Anlagen der Kommunen im Tagesbatch neu einreihen; Rückgabe: ihre Zahl."""
    from .services.zusammenfassung_batch import nacharbeiten

    return nacharbeiten()


@cron("30 6 * * *")
@task
def zusammenfassungen_einreichen() -> int:
    """Tagesbatch mit Zusammenfassungen einreichen; Rückgabe: Zahl der Anfragen."""
    from .services.zusammenfassung_batch import einreichen

    return einreichen()


@every(minutes=10)
@task
def zusammenfassungen_abholen() -> int:
    """Ergebnisse beendeter Batches übernehmen; Rückgabe: Zahl der gespeicherten Zusammenfassungen."""
    from .services.zusammenfassung_batch import abholen

    return abholen()

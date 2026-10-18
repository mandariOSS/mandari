# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnements der KI-Funktionen (``apps.events.registry.load_subscribers`` importiert dieses Modul).

``insight.zusammenfassung`` (``insight_ai.abonnement``) wird nur registriert, wenn ``SUMMARY_SUBSCRIPTION`` nicht
``aus`` ist. Mit ``schatten`` startet ein neu angelegtes Abonnement im Schattenbetrieb und zählt nur; danach gilt
der Zustand in der Datenbank, der Handler verwirft aber mit ``schatten`` nie. Ein neues Abonnement beginnt am Ende
des Journals: Zusammenfassungen aus der Zeit davor bleiben stehen.
"""

from __future__ import annotations

from django.conf import settings

from apps.events import subscriber
from insight_ai import abonnement


def register() -> bool:
    """Registriert ``insight.zusammenfassung`` je nach Schalter; ``True``, wenn registriert."""
    modus = settings.SUMMARY_SUBSCRIPTION
    if modus == "aus":
        return False
    subscriber(
        abonnement.NAME,
        types=abonnement.TYPES,
        batch=abonnement.BATCH,
        queue=abonnement.QUEUE,
        transactional=True,
        shadow=modus == "schatten",
    )(abonnement.zusammenfassung_verwerfen)
    return True


register()

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnements der Suche (``apps.events.registry.load_subscribers`` importiert dieses Modul).

``suchindex`` (``insight_search.abonnement``) wird nur registriert, wenn
``SEARCH_INDEX_SUBSCRIPTION`` nicht ``aus`` ist. Mit ``schatten`` startet ein neu angelegtes
Abonnement im Schattenbetrieb; danach gilt der Zustand in der Datenbank, der Handler schreibt aber
mit ``schatten`` nie in den Live-Index. Ein neues Abonnement beginnt am Ende des Journals; den
Bestand davor liefert der Vollbau (``manage.py suchindex_schatten aufbauen``), ältere Ereignisse
holt ``manage.py events_dispatch --replay suchindex --from-seq N`` nach.
"""

from __future__ import annotations

from django.conf import settings

from apps.events import subscriber
from insight_search import abonnement


def register() -> bool:
    """Registriert ``suchindex`` je nach Schalter; ``True``, wenn registriert."""
    modus = settings.SEARCH_INDEX_SUBSCRIPTION
    if modus == "aus":
        return False
    subscriber(
        abonnement.NAME,
        types=abonnement.TYPES,
        batch=abonnement.BATCH,
        queue=abonnement.QUEUE,
        transactional=False,
        shadow=modus == "schatten",
    )(abonnement.suchindex)
    return True


register()

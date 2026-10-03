# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufträge aus dem Admin des Bürgerportals (Issue #515): Sync einer Quelle und Löschen einer Kommune.

Beide liefen bis Issue #515 in einem Faden im Webprozess. Jetzt legt der Admin sie immer im Journal
an (``apps.events.tasks_backend.journal_backend``), auch solange die Webprozesse Aufträge sonst sofort
ausführen: Ein Sync oder eine Löschung dauert Minuten und gehört nicht in die Anfrage. Ausgeführt
werden sie vom Worker (Warteschlange ``default``); Zeitgrenze und Versuche stehen in
``TASKS["default"]["OPTIONS"]["tasks"]``.
"""

from __future__ import annotations

import logging
from typing import Final

from django.core.cache import cache
from django.tasks import task

logger = logging.getLogger(__name__)

#: Doppelklick-Schutz je Kommune (``cache.add``); verfällt von selbst, falls der Auftrag nie läuft
DELETION_LOCK_TTL: Final = 7200


def deletion_lock_key(body_id: str) -> str:
    return f"body-deletion-{body_id}"


@task
def quelle_synchronisieren(source_id: str, full: bool = False) -> None:
    """Synchronisiert eine Quelle wie bisher aus dem Admin (Sync-Protokoll mit ``triggered_by=admin``)."""
    from insight_sync.tasks import run_sync_with_logging

    from .models import OParlSource

    source = OParlSource.objects.filter(pk=source_id).first()
    if source is None:
        logger.warning("Sync aus dem Admin: Quelle %s gibt es nicht mehr", source_id)
        return
    run_sync_with_logging(source=source, full=full, triggered_by="admin")


@task
def kommune_loeschen(body_id: str) -> None:
    """Löscht eine Kommune mit allen RIS-Daten; gibt danach den Doppelklick-Schutz frei."""
    from .services.body_deletion import delete_body_data

    try:
        delete_body_data(body_id)
    finally:
        cache.delete(deletion_lock_key(body_id))

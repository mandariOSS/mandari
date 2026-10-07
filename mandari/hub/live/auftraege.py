# SPDX-License-Identifier: AGPL-3.0-or-later
"""Auftrag der Warteschlange ``live`` (Issue #915): Einzelbilder einer laufenden Übertragung lesen."""

from __future__ import annotations

import logging

from django.tasks import task

logger = logging.getLogger(__name__)


@task(queue_name="live")
def live_bilder_lesen(broadcast_id: str) -> int:
    """
    Liest rund 50 Sekunden lang im Takt der Quelle Einzelbilder der Übertragung (``hub.live.services.lesen``).
    Eingereiht vom Zeitplan ``live_status_abfragen``, höchstens einer je Übertragung; wiederholbar.
    """
    from .services import lesen

    gelesen = lesen(broadcast_id)
    logger.info("Live-Übertragung %s: %d Bilder gelesen", broadcast_id, gelesen)
    return gelesen

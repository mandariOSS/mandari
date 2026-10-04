# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zeitpläne der Synchronisation (``apps.events.schedule``, Issue #515).

- ``haengende_syncs_bereinigen``: alle 5 Minuten Sync-Protokolle, die länger als 15 Minuten „läuft“
  melden, als fehlgeschlagen markieren (``insight_sync.daemon.cleanup_stale_syncs``). Bis Issue #515
  erledigte das ein Faden im Webprozess jede Minute.
"""

from __future__ import annotations

from django.tasks import task

from apps.events.schedule import every

from .daemon import cleanup_stale_syncs


@every(minutes=5)
@task
def haengende_syncs_bereinigen() -> int:
    """Markiert hängende Sync-Protokolle als fehlgeschlagen; liefert ihre Anzahl."""
    return cleanup_stale_syncs()

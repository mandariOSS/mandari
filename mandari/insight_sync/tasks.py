# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sync-Lauf mit Protokoll (SyncLog) für den Admin-Sync und ``sync_daemon``.

Regulär synchronisiert der Ingestor-Daemon; einen manuellen Lauf startet
``python -m src.main sync`` im Ingestor-Container.
"""

import asyncio
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def _get_sync_orchestrator():
    """Lazy import des SyncOrchestrators um zirkuläre Imports zu vermeiden."""
    import sys
    from pathlib import Path

    # Lokale Entwicklung: ingestor/ neben mandari/
    candidates = [
        settings.BASE_DIR.parent / "ingestor",  # dev/ingestor/
        settings.BASE_DIR.parent / "apps" / "ingestor",  # dev/apps/ingestor/
        Path("/ingestor"),  # Docker-Mount
    ]
    for path in candidates:
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from src.sync.orchestrator import SyncOrchestrator

    return SyncOrchestrator


def count_synced_entities(result) -> int:
    """Zählt alle synchronisierten Entitäten eines SyncResult."""
    return (
        result.organizations_synced
        + result.persons_synced
        + result.memberships_synced
        + result.meetings_synced
        + result.papers_synced
        + result.files_synced
        + result.locations_synced
        + result.agenda_items_synced
        + result.consultations_synced
    )


def _result_details(result) -> dict:
    """Erstellt ein Dict mit Entitäten-Zählern pro Typ."""
    return {
        "organizations": result.organizations_synced,
        "persons": result.persons_synced,
        "memberships": result.memberships_synced,
        "meetings": result.meetings_synced,
        "papers": result.papers_synced,
        "files": result.files_synced,
        "locations": result.locations_synced,
        "agenda_items": result.agenda_items_synced,
        "consultations": result.consultations_synced,
    }


def run_sync_with_logging(
    *,
    source=None,
    full: bool = False,
    triggered_by: str = "admin",
    max_concurrent: int = 10,
):
    """
    Führt einen Sync aus und schreibt ein SyncLog.

    Args:
        source: OParlSource-Instanz oder None (= alle Quellen)
        full: True für Full Sync
        triggered_by: "admin", "daemon", "cli"
        max_concurrent: Maximale gleichzeitige HTTP-Requests
    """
    from django.utils import timezone

    from .models import SyncLog

    sync_type = SyncLog.SyncType.FULL if full else SyncLog.SyncType.INCREMENTAL
    log = SyncLog.objects.create(
        source=source,
        sync_type=sync_type,
        triggered_by=triggered_by,
    )

    SyncOrchestrator = _get_sync_orchestrator()
    start = timezone.now()

    async def _run():
        async with SyncOrchestrator(max_concurrent=max_concurrent) as orchestrator:
            if source:
                result = await orchestrator.sync_source(source.url, full=full)
                return [result]
            return await orchestrator.sync_all(full=full)

    try:
        results = asyncio.run(_run())

        total_entities = 0
        all_errors = []
        details = {}
        for result in results:
            entities = count_synced_entities(result)
            total_entities += entities
            if result.errors:
                all_errors.extend(result.errors)
            rd = _result_details(result)
            source_name = result.source_name or "unknown"
            details[source_name] = rd

        end = timezone.now()
        has_errors = bool(all_errors)
        log.status = SyncLog.Status.FAILED if has_errors and total_entities == 0 else SyncLog.Status.SUCCESS
        log.finished_at = end
        log.duration_seconds = (end - start).total_seconds()
        log.entities_synced = total_entities
        log.errors = all_errors
        log.details = details
        log.save()

        # Kennzahlen der Startseite sofort neu zählen statt erst nach Ablauf des Caches
        from insight_core.services import portal_stats

        portal_stats.invalidate_portal_stats()

    except Exception as e:
        end = timezone.now()
        log.status = SyncLog.Status.FAILED
        log.finished_at = end
        log.duration_seconds = (end - start).total_seconds()
        log.errors = [str(e)]
        log.save()
        logger.exception("Sync failed")

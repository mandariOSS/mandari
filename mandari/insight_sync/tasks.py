"""
Django 6.0 Background Tasks für OParl Synchronisation.

Diese Tasks können über Django's eingebautes Task-Framework ausgeführt werden:
- Sofort: task.call(...)
- Im Hintergrund: task.enqueue(...)
- Geplant: Über Management Commands und System-Scheduler (cron/systemd)
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from django.conf import settings
from django.tasks import task

logger = logging.getLogger(__name__)


def _get_sync_orchestrator():
    """Lazy import des SyncOrchestrators um zirkuläre Imports zu vermeiden."""
    import sys

    sys.path.insert(0, str(settings.BASE_DIR.parent / "apps" / "ingestor"))
    from src.sync.orchestrator import SyncOrchestrator

    return SyncOrchestrator


@task
def sync_all_sources(full: bool = False) -> dict[str, Any]:
    """
    Synchronisiert alle registrierten OParl-Quellen.

    Args:
        full: True für Full Sync, False für Incremental Sync

    Returns:
        Dict mit Sync-Statistiken
    """
    logger.info(f"Starting {'full' if full else 'incremental'} sync of all sources")

    SyncOrchestrator = _get_sync_orchestrator()

    async def _run_sync():
        async with SyncOrchestrator(max_concurrent=10) as orchestrator:
            results = await orchestrator.sync_all(full=full)

            total_entities = 0
            source_results = []

            for result in results:
                entities = (
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
                total_entities += entities
                source_results.append(
                    {
                        "source": result.source_name,
                        "success": result.success,
                        "entities": entities,
                        "duration": result.duration_seconds,
                        "errors": result.errors,
                    }
                )

            return {
                "sync_type": "full" if full else "incremental",
                "timestamp": datetime.now().isoformat(),
                "total_entities": total_entities,
                "sources": source_results,
            }

    # Run async code in sync context
    return asyncio.run(_run_sync())


@task
def sync_source(source_url: str, full: bool = False) -> dict[str, Any]:
    """
    Synchronisiert eine einzelne OParl-Quelle.

    Args:
        source_url: URL der OParl-Quelle
        full: True für Full Sync, False für Incremental Sync

    Returns:
        Dict mit Sync-Statistiken
    """
    logger.info(f"Starting {'full' if full else 'incremental'} sync of {source_url}")

    SyncOrchestrator = _get_sync_orchestrator()

    async def _run_sync():
        async with SyncOrchestrator(max_concurrent=10) as orchestrator:
            result = await orchestrator.sync_source(source_url, full=full)

            entities = (
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

            return {
                "sync_type": "full" if full else "incremental",
                "timestamp": datetime.now().isoformat(),
                "source": result.source_name,
                "success": result.success,
                "entities": entities,
                "duration": result.duration_seconds,
                "errors": result.errors,
            }

    return asyncio.run(_run_sync())

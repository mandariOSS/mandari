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
    from pathlib import Path

    # Lokale Entwicklung: ingestor/ neben mandari/
    candidates = [
        settings.BASE_DIR.parent / "ingestor",        # dev/ingestor/
        settings.BASE_DIR.parent / "apps" / "ingestor",  # dev/apps/ingestor/
        Path("/ingestor"),                             # Docker-Mount
    ]
    for path in candidates:
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))

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


def _count_result_entities(result) -> int:
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
            else:
                return await orchestrator.sync_all(full=full)

    try:
        results = asyncio.run(_run())

        total_entities = 0
        all_errors = []
        details = {}
        for result in results:
            entities = _count_result_entities(result)
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

    except Exception as e:
        end = timezone.now()
        log.status = SyncLog.Status.FAILED
        log.finished_at = end
        log.duration_seconds = (end - start).total_seconds()
        log.errors = [str(e)]
        log.save()
        logger.exception("Sync failed")

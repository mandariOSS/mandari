"""
Scheduler module - Automated OParl sync tasks.

Provides near real-time data synchronization using APScheduler.
Supports both incremental (frequent) and full (daily) sync modes.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from rich.console import Console
from rich.panel import Panel

from src.config import settings
from src.sync.orchestrator import SyncOrchestrator

logger = logging.getLogger(__name__)
console = Console()


class SyncScheduler:
    """
    Scheduler for automated OParl data synchronization.

    Runs:
    - Incremental syncs every N minutes (default: 15)
    - Full syncs once per day (default: 3 AM)
    """

    def __init__(
        self,
        sync_interval_minutes: int | None = None,
        full_sync_hour: int = 3,
        max_concurrent: int = 10,
    ) -> None:
        """
        Initialize the sync scheduler.

        Args:
            sync_interval_minutes: Minutes between incremental syncs.
                                   Defaults to settings.sync_interval_minutes.
            full_sync_hour: Hour of day for full sync (24h format, default 3 AM).
            max_concurrent: Maximum concurrent HTTP requests for sync.
        """
        self.sync_interval = sync_interval_minutes or settings.sync_interval_minutes
        self.full_sync_hour = full_sync_hour
        self.max_concurrent = max_concurrent

        self.scheduler = AsyncIOScheduler()
        self._is_syncing = False
        self._last_sync: datetime | None = None
        self._sync_stats: dict[str, Any] = {}

    async def start(self) -> None:
        """Start the scheduler and register jobs."""
        console.print(Panel.fit(
            "[bold green]Starting Sync Scheduler[/bold green]\n"
            f"[dim]Incremental: every {self.sync_interval} minutes[/dim]\n"
            f"[dim]Full sync: daily at {self.full_sync_hour:02d}:00[/dim]",
            border_style="green",
        ))

        # Add incremental sync job
        self.scheduler.add_job(
            self._run_incremental_sync,
            trigger=IntervalTrigger(minutes=self.sync_interval),
            id="incremental_sync",
            name="OParl Incremental Sync",
            replace_existing=True,
            max_instances=1,  # Prevent overlapping syncs
        )

        # Add full sync job (runs once daily)
        self.scheduler.add_job(
            self._run_full_sync,
            trigger=CronTrigger(hour=self.full_sync_hour, minute=0),
            id="full_sync",
            name="OParl Full Sync",
            replace_existing=True,
            max_instances=1,
        )

        self.scheduler.start()

        console.print("[green]Scheduler started successfully[/green]")
        console.print()
        console.print("[dim]Running initial sync...[/dim]")

        # Run an immediate incremental sync
        await self._run_incremental_sync()

    async def stop(self) -> None:
        """Stop the scheduler gracefully."""
        console.print("[yellow]Stopping scheduler...[/yellow]")
        self.scheduler.shutdown(wait=True)
        console.print("[green]Scheduler stopped[/green]")

    async def _run_incremental_sync(self) -> None:
        """Run an incremental sync for all sources."""
        if self._is_syncing:
            logger.warning("Sync already in progress, skipping...")
            return

        self._is_syncing = True
        start_time = datetime.now()

        try:
            console.print(f"\n[blue][{start_time.strftime('%H:%M:%S')}] Starting incremental sync...[/blue]")

            async with SyncOrchestrator(max_concurrent=self.max_concurrent) as orchestrator:
                results = await orchestrator.sync_all(full=False)

                # Update stats
                total_synced = 0
                for result in results:
                    if result.success:
                        total_synced += (
                            result.organizations_synced +
                            result.persons_synced +
                            result.memberships_synced +
                            result.meetings_synced +
                            result.papers_synced +
                            result.files_synced +
                            result.locations_synced +
                            result.agenda_items_synced +
                            result.consultations_synced
                        )
                    orchestrator.print_result(result)

                self._sync_stats["last_incremental"] = {
                    "time": start_time,
                    "duration": (datetime.now() - start_time).total_seconds(),
                    "entities": total_synced,
                }

            self._last_sync = datetime.now()
            duration = (self._last_sync - start_time).total_seconds()
            console.print(f"[green]Incremental sync completed in {duration:.1f}s[/green]")

            # Write single sync log for the entire cycle
            await self._write_cycle_log(orchestrator, results, start_time, "incremental")

        except Exception as e:
            logger.exception("Incremental sync failed")
            console.print(f"[red]Sync error: {e}[/red]")
        finally:
            self._is_syncing = False

    async def _run_full_sync(self) -> None:
        """Run a full sync for all sources."""
        if self._is_syncing:
            logger.warning("Sync already in progress, skipping full sync...")
            return

        self._is_syncing = True
        start_time = datetime.now()

        try:
            console.print(f"\n[bold blue][{start_time.strftime('%H:%M:%S')}] Starting FULL sync...[/bold blue]")

            async with SyncOrchestrator(max_concurrent=self.max_concurrent) as orchestrator:
                results = await orchestrator.sync_all(full=True)

                total_synced = 0
                for result in results:
                    if result.success:
                        total_synced += (
                            result.organizations_synced +
                            result.persons_synced +
                            result.memberships_synced +
                            result.meetings_synced +
                            result.papers_synced +
                            result.files_synced +
                            result.locations_synced +
                            result.agenda_items_synced +
                            result.consultations_synced
                        )
                    orchestrator.print_result(result)

                self._sync_stats["last_full"] = {
                    "time": start_time,
                    "duration": (datetime.now() - start_time).total_seconds(),
                    "entities": total_synced,
                }

            self._last_sync = datetime.now()
            duration = (self._last_sync - start_time).total_seconds()
            console.print(f"[bold green]Full sync completed in {duration:.1f}s[/bold green]")

            # Write single sync log for the entire cycle
            await self._write_cycle_log(orchestrator, results, start_time, "full")

        except Exception as e:
            logger.exception("Full sync failed")
            console.print(f"[red]Full sync error: {e}[/red]")
        finally:
            self._is_syncing = False

    async def _write_cycle_log(self, orchestrator, results, start_time, sync_type):
        """Write a single sync log entry for the entire sync cycle."""
        from datetime import timezone as tz

        end_time = datetime.now(tz.utc)
        start_utc = start_time.astimezone(tz.utc) if start_time.tzinfo else start_time.replace(tzinfo=tz.utc)
        duration = (end_time - start_utc).total_seconds()

        total_entities = 0
        all_errors = []
        details = {}

        for result in results:
            synced = (
                result.organizations_synced + result.persons_synced
                + result.memberships_synced + result.meetings_synced
                + result.papers_synced + result.files_synced
                + result.locations_synced + result.agenda_items_synced
                + result.consultations_synced
            )
            total_entities += synced
            all_errors.extend(result.errors)
            name = result.source_name or "Unknown"
            details[name] = {
                "meetings": result.meetings_synced,
                "papers": result.papers_synced,
                "persons": result.persons_synced,
                "organizations": result.organizations_synced,
                "files": result.files_synced,
            }

        status = "success" if not all_errors or total_entities > 0 else "failed"

        try:
            await orchestrator.storage.write_sync_log(
                source_id=None,
                sync_type=sync_type,
                status=status,
                started_at=start_utc,
                finished_at=end_time,
                duration_seconds=duration,
                entities_synced=total_entities,
                errors=all_errors[:20],
                details=details,
                triggered_by="daemon",
            )
        except Exception as e:
            console.print(f"[yellow]Warning: Could not write sync log: {e}[/yellow]")

    def get_status(self) -> dict[str, Any]:
        """Get current scheduler status."""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
            })

        return {
            "running": self.scheduler.running,
            "is_syncing": self._is_syncing,
            "last_sync": str(self._last_sync) if self._last_sync else None,
            "jobs": jobs,
            "stats": self._sync_stats,
        }


async def run_scheduler(
    sync_interval: int | None = None,
    full_sync_hour: int = 3,
    max_concurrent: int = 10,
    metrics_port: int = 9090,
) -> None:
    """
    Run the sync scheduler continuously.

    Args:
        sync_interval: Minutes between incremental syncs.
        full_sync_hour: Hour for daily full sync.
        max_concurrent: Max concurrent HTTP requests.
        metrics_port: Port for Prometheus metrics server.
    """
    from src.metrics import metrics

    scheduler = SyncScheduler(
        sync_interval_minutes=sync_interval,
        full_sync_hour=full_sync_hour,
        max_concurrent=max_concurrent,
    )

    try:
        # Start metrics server
        await metrics.start_server(port=metrics_port)

        await scheduler.start()

        # Keep running until interrupted
        while True:
            await asyncio.sleep(60)

            # Print status periodically
            status = scheduler.get_status()
            if status["jobs"]:
                next_job = status["jobs"][0]
                console.print(
                    f"[dim]Next sync: {next_job.get('next_run', 'unknown')}[/dim]",
                    highlight=False,
                )
    except asyncio.CancelledError:
        await scheduler.stop()
    except KeyboardInterrupt:
        await scheduler.stop()

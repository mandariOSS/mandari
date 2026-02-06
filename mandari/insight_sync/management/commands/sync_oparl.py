"""
Django Management Command für OParl Synchronisation.

Beispiele:
    # Incremental Sync aller Quellen
    python manage.py sync_oparl

    # Full Sync aller Quellen
    python manage.py sync_oparl --full

    # Sync einer einzelnen Quelle
    python manage.py sync_oparl --source https://oparl.stadt-muenster.de/system

    # Im Hintergrund ausführen (Django 6.0 Tasks)
    python manage.py sync_oparl --background

Automatische Ausführung via Cron:
    # Incremental alle 15 Minuten
    */15 * * * * cd /path/to/mandari && python manage.py sync_oparl

    # Full Sync täglich um 3:00
    0 3 * * * cd /path/to/mandari && python manage.py sync_oparl --full
"""

import asyncio
import sys

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Synchronisiert OParl-Daten von registrierten Quellen"

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            "-s",
            type=str,
            help="URL einer einzelnen Quelle (sonst alle Quellen)",
        )
        parser.add_argument(
            "--full",
            "-f",
            action="store_true",
            help="Full Sync statt Incremental Sync",
        )
        parser.add_argument(
            "--background",
            "-b",
            action="store_true",
            help="Task im Hintergrund ausführen (Django 6.0 Tasks)",
        )
        parser.add_argument(
            "--concurrent",
            "-c",
            type=int,
            default=10,
            help="Maximale gleichzeitige HTTP-Requests (default: 10)",
        )

    def handle(self, *args, **options):
        source_url = options["source"]
        full = options["full"]
        background = options["background"]
        concurrent = options["concurrent"]

        if background:
            self._run_background(source_url, full)
        else:
            self._run_sync(source_url, full, concurrent)

    def _run_background(self, source_url: str | None, full: bool):
        """Führt den Sync als Django 6.0 Background Task aus."""
        from insight_sync.tasks import sync_all_sources, sync_source

        sync_type = "Full" if full else "Incremental"

        if source_url:
            self.stdout.write(f"Starte {sync_type} Sync für {source_url} im Hintergrund...")
            sync_source.enqueue(source_url=source_url, full=full)
        else:
            self.stdout.write(f"Starte {sync_type} Sync aller Quellen im Hintergrund...")
            sync_all_sources.enqueue(full=full)

        self.stdout.write(self.style.SUCCESS("Task wurde in die Queue eingereiht."))

    def _run_sync(self, source_url: str | None, full: bool, concurrent: int):
        """Führt den Sync direkt aus."""
        # Add ingestor to path
        ingestor_path = settings.BASE_DIR.parent / "apps" / "ingestor"
        sys.path.insert(0, str(ingestor_path))

        from src.sync.orchestrator import SyncOrchestrator

        sync_type = "Full" if full else "Incremental"

        async def _run():
            async with SyncOrchestrator(max_concurrent=concurrent) as orchestrator:
                if source_url:
                    self.stdout.write(f"Starte {sync_type} Sync für {source_url}...")
                    result = await orchestrator.sync_source(source_url, full=full)
                    orchestrator.print_result(result)

                    if result.success:
                        self.stdout.write(
                            self.style.SUCCESS(f"Sync erfolgreich: {self._count_entities(result)} Entitäten")
                        )
                    else:
                        self.stdout.write(self.style.ERROR(f"Sync fehlgeschlagen: {', '.join(result.errors)}"))
                else:
                    self.stdout.write(f"Starte {sync_type} Sync aller Quellen...")
                    results = await orchestrator.sync_all(full=full)

                    total_entities = 0
                    for result in results:
                        orchestrator.print_result(result)
                        if result.success:
                            total_entities += self._count_entities(result)

                    self.stdout.write(
                        self.style.SUCCESS(f"Sync abgeschlossen: {total_entities} Entitäten synchronisiert")
                    )

        asyncio.run(_run())

    def _count_entities(self, result) -> int:
        """Zählt alle synchronisierten Entitäten."""
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

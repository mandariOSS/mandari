"""
Django Management Command für kontinuierliche OParl Synchronisation.

Führt automatisch aus:
- Incremental Sync alle N Minuten (default: 15)
- Full Sync einmal täglich (default: 3:00 Uhr)

Beispiele:
    # Standard-Daemon starten
    python manage.py sync_daemon

    # Mit angepassten Intervallen
    python manage.py sync_daemon --interval 30 --full-hour 4

    # Nur einmal Incremental Sync und beenden
    python manage.py sync_daemon --once

    # Nur einmal Full Sync und beenden
    python manage.py sync_daemon --once --full

Für Produktion als Systemd Service:
    [Unit]
    Description=Mandari OParl Sync Daemon
    After=network.target postgresql.service

    [Service]
    Type=simple
    User=mandari
    WorkingDirectory=/path/to/mandari
    ExecStart=/path/to/venv/bin/python manage.py sync_daemon
    Restart=always
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
"""

import asyncio
import signal
import sys
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Startet einen Daemon für kontinuierliche OParl Synchronisation"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._running = True
        self._is_syncing = False
        self._last_full_sync_date = None

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval", "-i",
            type=int,
            default=getattr(settings, "SYNC_INTERVAL_MINUTES", 15),
            help="Minuten zwischen Incremental Syncs (default: 15)",
        )
        parser.add_argument(
            "--full-hour",
            type=int,
            default=getattr(settings, "SYNC_FULL_HOUR", 3),
            help="Stunde für täglichen Full Sync (0-23, default: 3)",
        )
        parser.add_argument(
            "--concurrent", "-c",
            type=int,
            default=10,
            help="Maximale gleichzeitige HTTP-Requests (default: 10)",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Nur einmal ausführen und beenden",
        )
        parser.add_argument(
            "--full", "-f",
            action="store_true",
            help="Full Sync statt Incremental (bei --once)",
        )

    def handle(self, *args, **options):
        interval = options["interval"]
        full_hour = options["full_hour"]
        concurrent = options["concurrent"]
        once = options["once"]
        full = options["full"]

        # Signal Handler für graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Add ingestor to path
        ingestor_path = settings.BASE_DIR.parent / "apps" / "ingestor"
        sys.path.insert(0, str(ingestor_path))

        if once:
            # Einmalige Ausführung
            self._run_once(full, concurrent)
        else:
            # Daemon-Modus
            self._run_daemon(interval, full_hour, concurrent)

    def _signal_handler(self, signum, frame):
        """Graceful shutdown bei SIGINT/SIGTERM."""
        self.stdout.write("\n" + self.style.WARNING("Shutdown Signal empfangen..."))
        self._running = False

    def _run_once(self, full: bool, concurrent: int):
        """Führt einen einzelnen Sync aus."""
        sync_type = "Full" if full else "Incremental"
        self.stdout.write(f"Starte {sync_type} Sync...")
        asyncio.run(self._sync_all(full, concurrent))

    def _run_daemon(self, interval: int, full_hour: int, concurrent: int):
        """Läuft als Daemon mit periodischen Syncs."""
        self.stdout.write(self.style.SUCCESS(
            f"\n{'='*60}\n"
            f"Mandari OParl Sync Daemon gestartet\n"
            f"{'='*60}\n"
            f"Incremental Sync: alle {interval} Minuten\n"
            f"Full Sync: täglich um {full_hour:02d}:00 Uhr\n"
            f"{'='*60}\n"
        ))

        asyncio.run(self._daemon_loop(interval, full_hour, concurrent))

    async def _daemon_loop(self, interval: int, full_hour: int, concurrent: int):
        """Haupt-Loop des Daemons."""
        # Initialer Sync
        self.stdout.write("Führe initialen Incremental Sync aus...")
        await self._sync_all(full=False, concurrent=concurrent)

        while self._running:
            try:
                # Warte bis zum nächsten Sync
                next_sync = datetime.now().replace(second=0, microsecond=0)
                minutes_to_wait = interval - (next_sync.minute % interval)
                next_sync = next_sync.replace(
                    minute=(next_sync.minute + minutes_to_wait) % 60
                )
                if minutes_to_wait == interval:
                    minutes_to_wait = 0

                wait_seconds = max(0, (next_sync - datetime.now()).total_seconds())

                if wait_seconds > 0:
                    self.stdout.write(
                        f"[{datetime.now().strftime('%H:%M:%S')}] "
                        f"Nächster Sync: {next_sync.strftime('%H:%M:%S')} "
                        f"(in {int(wait_seconds/60)} Minuten)"
                    )

                # Warte in kleinen Schritten für responsives Shutdown
                for _ in range(int(wait_seconds)):
                    if not self._running:
                        break
                    await asyncio.sleep(1)

                if not self._running:
                    break

                # Prüfe ob Full Sync fällig
                now = datetime.now()
                is_full_sync_time = (
                    now.hour == full_hour and
                    (self._last_full_sync_date is None or
                     self._last_full_sync_date != now.date())
                )

                if is_full_sync_time:
                    self.stdout.write(self.style.WARNING(
                        f"\n[{now.strftime('%H:%M:%S')}] Starte täglichen Full Sync..."
                    ))
                    await self._sync_all(full=True, concurrent=concurrent)
                    self._last_full_sync_date = now.date()
                else:
                    self.stdout.write(
                        f"\n[{now.strftime('%H:%M:%S')}] Starte Incremental Sync..."
                    )
                    await self._sync_all(full=False, concurrent=concurrent)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Sync Fehler: {e}"))
                # Warte kurz vor erneutem Versuch
                await asyncio.sleep(60)

        self.stdout.write(self.style.SUCCESS("Daemon beendet."))

    async def _sync_all(self, full: bool, concurrent: int):
        """Führt den Sync aller Quellen aus."""
        if self._is_syncing:
            self.stdout.write(self.style.WARNING("Sync läuft bereits, überspringe..."))
            return

        self._is_syncing = True
        start_time = datetime.now()

        try:
            from src.sync.orchestrator import SyncOrchestrator

            async with SyncOrchestrator(max_concurrent=concurrent) as orchestrator:
                results = await orchestrator.sync_all(full=full)

                total_entities = 0
                for result in results:
                    if result.success:
                        total_entities += self._count_entities(result)
                    orchestrator.print_result(result)

                duration = (datetime.now() - start_time).total_seconds()
                sync_type = "Full" if full else "Incremental"
                self.stdout.write(self.style.SUCCESS(
                    f"{sync_type} Sync abgeschlossen in {duration:.1f}s: "
                    f"{total_entities} Entitäten"
                ))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Sync fehlgeschlagen: {e}"))
        finally:
            self._is_syncing = False

    def _count_entities(self, result) -> int:
        """Zählt alle synchronisierten Entitäten."""
        return (
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

# SPDX-License-Identifier: AGPL-3.0-or-later
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

import signal
import time
from argparse import ArgumentParser
from datetime import date, datetime, timedelta
from types import FrameType
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand


def seconds_until_next_slot(now: datetime, interval_minutes: int) -> float:
    """Sekunden bis zum nächsten Zeitpunkt im Raster von ``interval_minutes`` ab Mitternacht.

    Liegt ``now`` genau auf einem Rasterpunkt, zählt der nächste (kein Sofortlauf direkt nach
    einem Sync). Über Stunden- und Tagesgrenzen hinweg korrekt, weil mit Zeitspannen statt mit
    einzelnen Uhrzeitfeldern gerechnet wird.
    """
    slot = max(1, int(interval_minutes)) * 60
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (now - midnight).total_seconds()
    return (int(elapsed // slot) + 1) * slot - elapsed


class Command(BaseCommand):
    help = "Startet einen Daemon für kontinuierliche OParl Synchronisation"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._running = True
        self._last_full_sync_date: date | None = None

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--interval",
            "-i",
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
            "--concurrent",
            "-c",
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
            "--full",
            "-f",
            action="store_true",
            help="Full Sync statt Incremental (bei --once)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        interval = options["interval"]
        full_hour = options["full_hour"]
        concurrent = options["concurrent"]
        once = options["once"]
        full = options["full"]

        # Signal Handler für graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        if once:
            # Einmalige Ausführung
            self._run_once(full, concurrent)
        else:
            # Daemon-Modus
            self._run_daemon(interval, full_hour, concurrent)

    def _signal_handler(self, signum: int, frame: FrameType | None) -> None:
        """Graceful shutdown bei SIGINT/SIGTERM."""
        self.stdout.write("\n" + self.style.WARNING("Shutdown Signal empfangen..."))
        self._running = False

    def _get_config(self) -> Any:
        """Liest die Sync-Konfiguration aus der Datenbank."""
        try:
            from insight_sync.models import SyncConfig

            return SyncConfig.get()
        except Exception:
            return None

    def _run_once(self, full: bool, concurrent: int) -> None:
        """Führt einen einzelnen Sync aus."""
        sync_type = "Full" if full else "Incremental"
        self.stdout.write(f"Starte {sync_type} Sync...")
        self._sync_all_with_logging(full, concurrent)

    def _run_daemon(self, interval: int, full_hour: int, concurrent: int) -> None:
        """Läuft als Daemon mit periodischen Syncs."""
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{'=' * 60}\n"
                f"Mandari OParl Sync Daemon gestartet\n"
                f"{'=' * 60}\n"
                f"Incremental Sync: alle {interval} Minuten\n"
                f"Full Sync: täglich um {full_hour:02d}:00 Uhr\n"
                f"{'=' * 60}\n"
            )
        )

        self._daemon_loop(interval, full_hour, concurrent)

    def _sleep(self, seconds: float) -> None:
        """In Sekundenschritten warten, damit SIGINT/SIGTERM zügig greifen."""
        for _ in range(int(seconds)):
            if not self._running:
                return
            time.sleep(1)

    def _daemon_loop(self, interval: int, full_hour: int, concurrent: int) -> None:
        """Haupt-Loop des Daemons.

        Bewusst synchron: run_sync_with_logging startet für den Sync eine eigene Event-Loop
        (asyncio.run) und schreibt das SyncLog über das ORM. Lief diese Schleife selbst unter
        asyncio.run, scheiterte jeder Lauf an der bereits laufenden Loop bzw. am ORM-Zugriff
        aus asynchronem Kontext und endete als fehlgeschlagen.
        """
        # Initialer Sync
        self.stdout.write("Führe initialen Incremental Sync aus...")
        self._sync_all_with_logging(full=False, concurrent=concurrent)

        while self._running:
            try:
                # Konfiguration aus DB lesen (überschreibt CLI-Werte)
                config = self._get_config()
                if config:
                    interval = config.interval_minutes
                    full_hour = config.full_sync_hour
                    concurrent = config.max_concurrent

                    if not config.sync_enabled:
                        self.stdout.write(
                            f"[{datetime.now().strftime('%H:%M:%S')}] "
                            f"Sync pausiert (über Admin deaktiviert). Prüfe in 60s erneut..."
                        )
                        self._sleep(60)
                        continue

                # Warte bis zum nächsten Zeitpunkt im Raster (Stunden- und Tagesübertrag korrekt)
                wait_seconds = seconds_until_next_slot(datetime.now(), interval)
                next_sync = datetime.now() + timedelta(seconds=wait_seconds)
                self.stdout.write(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"Nächster Sync: {next_sync.strftime('%H:%M:%S')} "
                    f"(in {int(wait_seconds / 60)} Minuten)"
                )
                self._sleep(wait_seconds)

                if not self._running:
                    break

                # Prüfe ob Full Sync fällig
                now = datetime.now()
                is_full_sync_time = now.hour == full_hour and (
                    self._last_full_sync_date is None or self._last_full_sync_date != now.date()
                )

                if is_full_sync_time:
                    self.stdout.write(
                        self.style.WARNING(f"\n[{now.strftime('%H:%M:%S')}] Starte täglichen Full Sync...")
                    )
                    self._sync_all_with_logging(full=True, concurrent=concurrent)
                    self._last_full_sync_date = now.date()
                else:
                    self.stdout.write(f"\n[{now.strftime('%H:%M:%S')}] Starte Incremental Sync...")
                    self._sync_all_with_logging(full=False, concurrent=concurrent)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Sync Fehler: {e}"))
                # Warte kurz vor erneutem Versuch
                self._sleep(60)

        self.stdout.write(self.style.SUCCESS("Daemon beendet."))

    def _sync_all_with_logging(self, full: bool, concurrent: int) -> None:
        """Führt den Sync aus und schreibt ein SyncLog."""
        try:
            from insight_sync.tasks import run_sync_with_logging

            run_sync_with_logging(
                full=full,
                triggered_by="daemon",
                max_concurrent=concurrent,
            )
            sync_type = "Full" if full else "Incremental"
            self.stdout.write(self.style.SUCCESS(f"{sync_type} Sync abgeschlossen (mit Logging)."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Sync fehlgeschlagen: {e}"))

        # Nach jedem Zyklus: begrenzter Georef-Lauf (Regex/Gazetteer, kein LLM).
        # Steuerung über GEOREF_AUTO_ENABLED / GEOREF_AUTO_LIMIT.
        self._run_georef_pass()

    def _run_georef_pass(self) -> None:
        """Automatischer Georef-Lauf nach einem Sync-Zyklus (best effort)."""
        try:
            from insight_core.services.georef_runner import run_auto_georef_pass

            stats = run_auto_georef_pass()
            if stats.get("processed") or stats.get("oparl_backfilled"):
                self.stdout.write(
                    f"Georef: {stats.get('processed', 0)} Papers verarbeitet "
                    f"({stats.get('completed', 0)} mit Orten), "
                    f"{stats.get('oparl_backfilled', 0)} OParl-Backfills"
                )
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"Georef-Lauf fehlgeschlagen: {e}"))

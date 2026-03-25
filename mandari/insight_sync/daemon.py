"""
Background Sync Daemon — läuft als Thread innerhalb des Django-Prozesses.

Wird automatisch von AppConfig.ready() gestartet wenn SYNC_DAEMON_AUTOSTART=True.
Liest bei jedem Zyklus SyncConfig aus der DB → Admin steuert Intervall, Pause, Full-Sync-Stunde.
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger("insight_sync.daemon")

_daemon_thread: threading.Thread | None = None
_stop_event = threading.Event()
_start_lock = threading.Lock()
_started = False


def is_running() -> bool:
    """Prüft ob der Daemon-Thread läuft."""
    return _daemon_thread is not None and _daemon_thread.is_alive()


def start():
    """Startet den Daemon-Thread (Thread-safe, idempotent)."""
    global _daemon_thread, _started

    with _start_lock:
        if _started or is_running():
            logger.debug("Sync-Daemon läuft bereits, überspringe Start.")
            return

        _started = True
        _stop_event.clear()
        _daemon_thread = threading.Thread(
            target=_daemon_loop,
            name="sync-daemon",
            daemon=True,
        )
        _daemon_thread.start()
        logger.info(
            "Sync-Daemon gestartet (pid=%d, thread=%s).",
            os.getpid(),
            _daemon_thread.name,
        )


def stop():
    """Stoppt den Daemon-Thread."""
    global _daemon_thread

    if not is_running():
        return

    _stop_event.set()
    _daemon_thread.join(timeout=10)
    _daemon_thread = None
    logger.info("Sync-Daemon gestoppt.")


def _get_config():
    """Liest SyncConfig aus der DB. Gibt None zurück wenn DB nicht bereit."""
    try:
        from .models import SyncConfig

        return SyncConfig.get()
    except Exception:
        return None


def _is_sync_running() -> bool:
    """Prüft ob aktuell ein Sync in der DB als 'running' markiert ist."""
    try:
        from .models import SyncLog

        return SyncLog.objects.filter(status="running").exists()
    except Exception:
        return False


def _cleanup_stale_syncs():
    """Markiert hängende Syncs (>15 Min) als fehlgeschlagen."""
    try:
        from django.utils import timezone

        from .models import SyncLog

        cutoff = timezone.now() - timedelta(minutes=15)
        stale = SyncLog.objects.filter(status="running", started_at__lt=cutoff)
        count = stale.update(
            status="failed",
            finished_at=timezone.now(),
            errors=["Sync-Timeout: Prozess hat nicht innerhalb von 15 Minuten geantwortet"],
        )
        if count:
            logger.warning(f"{count} hängende Sync-Logs bereinigt (>15 Min ohne Abschluss)")
    except Exception:
        pass


def _daemon_loop():
    """Haupt-Loop: Liest Config, wartet, synct."""
    from pathlib import Path

    from django.conf import settings

    # Ingestor-Pfad hinzufügen (lokal + Docker)
    for path in [
        settings.BASE_DIR.parent / "ingestor",
        settings.BASE_DIR.parent / "apps" / "ingestor",
        Path("/ingestor"),
    ]:
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))

    last_full_sync_date = None

    # Kurz warten bis Django vollständig gestartet ist
    _wait(5)

    # Beim Start: hängende Syncs aufräumen
    _cleanup_stale_syncs()

    logger.info("Sync-Daemon-Loop gestartet. Konfiguration wird aus DB gelesen (Admin → Sync-Einstellungen).")

    while not _stop_event.is_set():
        try:
            config = _get_config()
            if config is None:
                _wait(30)
                continue

            if not config.sync_enabled:
                logger.debug("Sync pausiert (Admin). Prüfe in 60s erneut.")
                _wait(60)
                continue

            interval = config.interval_minutes
            full_hour = config.full_sync_hour
            max_concurrent = config.max_concurrent

            # Berechne Wartezeit bis zum nächsten Intervall-Grenzwert
            now = datetime.now()
            if interval > 0:
                minutes_to_wait = interval - (now.minute % interval)
                if minutes_to_wait == interval:
                    minutes_to_wait = 0
                wait_seconds = max(0, minutes_to_wait * 60 - now.second)
            else:
                wait_seconds = 60

            if wait_seconds > 5:
                logger.debug(
                    f"Nächster Sync in {wait_seconds // 60} Min. {wait_seconds % 60} Sek. (Intervall: {interval} Min.)"
                )
                if _wait(wait_seconds):
                    break
                continue  # Config neu lesen nach dem Warten

            # Prüfen ob bereits ein Sync läuft
            _cleanup_stale_syncs()
            if _is_sync_running():
                logger.warning("Sync läuft bereits — überspringe diesen Intervall.")
                _wait(60)
                continue

            # Sync ausführen
            now = datetime.now()
            is_full = (
                0 <= full_hour <= 23
                and now.hour == full_hour
                and (last_full_sync_date is None or last_full_sync_date != now.date())
            )

            if is_full:
                logger.info(f"Starte täglichen Full Sync ({now.strftime('%H:%M')})...")
                _run_sync(full=True, max_concurrent=max_concurrent)
                last_full_sync_date = now.date()
            else:
                logger.info(f"Starte Incremental Sync ({now.strftime('%H:%M')})...")
                _run_sync(full=False, max_concurrent=max_concurrent)

            # Nach Sync mindestens 1 Intervall warten
            _wait(max(interval * 60 - 10, 60))

        except Exception:
            logger.exception("Fehler im Sync-Daemon-Loop")
            _wait(60)

    logger.info("Sync-Daemon-Loop beendet.")


def _run_sync(*, full: bool, max_concurrent: int):
    """Führt einen Sync synchron aus (blockiert den Daemon-Thread)."""
    try:
        from .tasks import run_sync_with_logging

        run_sync_with_logging(
            full=full,
            triggered_by="daemon",
            max_concurrent=max_concurrent,
        )
    except ImportError as e:
        logger.error(
            f"Ingestor nicht verfügbar: {e} — "
            f"Fehlende Abhängigkeiten? Installiere: pip install sqlalchemy asyncpg pydantic-settings. "
            f"Sync übersprungen."
        )
    except Exception:
        logger.exception("Sync fehlgeschlagen")


def _wait(seconds: float) -> bool:
    """Wartet `seconds`, bricht ab wenn Stop-Event gesetzt. Gibt True zurück bei Stop."""
    return _stop_event.wait(timeout=seconds)

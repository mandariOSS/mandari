# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sync Watchdog — prüft ob der Ingestor-Container lebt und bereinigt hängende Logs.

Der eigentliche Sync läuft im Ingestor-Container (eigener Prozess).
Django ist nur das Kontrollzentrum (Logs anzeigen, Syncs triggern via Redis).
"""

import logging
import os
import threading
from datetime import timedelta

logger = logging.getLogger("insight_sync.daemon")

_watchdog_thread: threading.Thread | None = None
_stop_event = threading.Event()
_start_lock = threading.Lock()
_started = False


def is_running() -> bool:
    """Prüft ob der Watchdog-Thread läuft."""
    return _watchdog_thread is not None and _watchdog_thread.is_alive()


def is_ingestor_active() -> bool:
    """Prüft ob der Ingestor kürzlich ein SyncLog geschrieben hat (<30 Min)."""
    try:
        from django.utils import timezone

        from .models import SyncLog

        cutoff = timezone.now() - timedelta(minutes=30)
        return SyncLog.objects.filter(started_at__gte=cutoff).exists()
    except Exception:
        return False


def start():
    """Startet den Watchdog-Thread (Thread-safe, idempotent)."""
    global _watchdog_thread, _started

    with _start_lock:
        if _started or is_running():
            return

        _started = True
        _stop_event.clear()
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop,
            name="sync-watchdog",
            daemon=True,
        )
        _watchdog_thread.start()
        logger.info("Sync-Watchdog gestartet (pid=%d).", os.getpid())


def stop():
    """Stoppt den Watchdog-Thread."""
    global _watchdog_thread

    if not is_running():
        return

    _stop_event.set()
    _watchdog_thread.join(timeout=10)
    _watchdog_thread = None
    logger.info("Sync-Watchdog gestoppt.")


def trigger_sync(full: bool = False):
    """Sendet ein Sync-Trigger-Event via Redis an den Ingestor."""
    import json

    try:
        import redis
        from django.conf import settings

        r = redis.from_url(getattr(settings, "REDIS_URL", "redis://localhost:6379"))
        r.publish("mandari:sync:trigger", json.dumps({"full": full}))
        logger.info(f"Sync-Trigger gesendet (full={full})")
        return True
    except Exception as e:
        logger.warning(f"Sync-Trigger fehlgeschlagen: {e}")
        return False


def _cleanup_stale_syncs():
    """Markiert hängende Syncs (>15 Min) als fehlgeschlagen."""
    try:
        from django.utils import timezone

        from .models import SyncLog

        cutoff = timezone.now() - timedelta(minutes=15)
        count = SyncLog.objects.filter(status="running", started_at__lt=cutoff).update(
            status="failed",
            finished_at=timezone.now(),
            errors=["Sync-Timeout: Prozess hat nicht innerhalb von 15 Minuten geantwortet"],
        )
        if count:
            logger.warning(f"{count} hängende Sync-Logs bereinigt")
    except Exception:
        pass


def _run_periodic_georef():
    """Periodischer Georef-Lauf (Regex/Gazetteer, begrenzt, cache-gelockt)."""
    try:
        from insight_core.services.georef_runner import run_auto_georef_pass

        run_auto_georef_pass()
    except Exception:
        logger.exception("Periodischer Georef-Lauf fehlgeschlagen")


def _run_periodic_faction_reminders():
    """Periodischer Erinnerungslauf für Fraktionssitzungen (48 h vorher, cache-gelockt)."""
    try:
        from apps.work.faction.services import run_faction_reminder_pass

        run_faction_reminder_pass()
    except Exception:
        logger.exception("Periodischer Fraktions-Erinnerungslauf fehlgeschlagen")


def _run_periodic_faction_schedule():
    """Periodische Sitzungserzeugung aus Sitzungsreihen (Issue #61, cache-gelockt)."""
    try:
        from apps.work.faction.generation import run_faction_schedule_pass

        run_faction_schedule_pass()
    except Exception:
        logger.exception("Periodische Fraktions-Sitzungserzeugung fehlgeschlagen")


def _watchdog_loop():
    """Watchdog-Loop: Bereinigt hängende Logs, prüft Ingestor-Status."""
    from django.conf import settings

    _wait(10)

    _cleanup_stale_syncs()
    logger.info("Sync-Watchdog aktiv. Ingestor-Container übernimmt die Synchronisation.")

    georef_interval = max(1, int(getattr(settings, "GEOREF_AUTO_INTERVAL_MINUTES", 15)))
    minutes_since_georef = georef_interval  # erster Lauf direkt nach dem Start

    reminder_interval = max(1, int(getattr(settings, "FACTION_REMINDER_INTERVAL_MINUTES", 15)))
    minutes_since_reminder = reminder_interval  # erster Lauf direkt nach dem Start

    schedule_interval = max(1, int(getattr(settings, "FACTION_SCHEDULE_INTERVAL_MINUTES", 60)))
    minutes_since_schedule = schedule_interval  # erster Lauf direkt nach dem Start

    while not _stop_event.is_set():
        try:
            _cleanup_stale_syncs()

            # Periodischer Georef-Lauf (der Ingestor-Container synct nur,
            # die Georeferenzierung läuft Django-seitig)
            minutes_since_georef += 1
            if minutes_since_georef >= georef_interval:
                minutes_since_georef = 0
                _run_periodic_georef()

            # Periodische Erinnerungen für Fraktionssitzungen (Issue #59)
            minutes_since_reminder += 1
            if minutes_since_reminder >= reminder_interval:
                minutes_since_reminder = 0
                _run_periodic_faction_reminders()

            # Periodische Sitzungserzeugung aus Sitzungsreihen (Issue #61)
            minutes_since_schedule += 1
            if minutes_since_schedule >= schedule_interval:
                minutes_since_schedule = 0
                _run_periodic_faction_schedule()

            _wait(60)
        except Exception:
            logger.exception("Fehler im Watchdog-Loop")
            _wait(60)


def _wait(seconds: float) -> bool:
    return _stop_event.wait(timeout=seconds)

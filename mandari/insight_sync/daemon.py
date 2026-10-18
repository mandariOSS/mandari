# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verbindung zum Ingestor: Status und Anstoß der Synchronisation.

Der eigentliche Sync läuft im Ingestor-Container (eigener Prozess). Django ist nur das
Kontrollzentrum (Logs anzeigen, Syncs per Redis anstoßen). Hängende Sync-Protokolle räumt der
Zeitplan ``insight_sync.schedules.haengende_syncs_bereinigen`` im Worker auf (Issue #515; bis dahin
ein Faden im Webprozess).

Seit Issue #1026 hat jede Quelle ihren eigenen Takt (nachts und am Wochenende seltener). Ob der Daemon lebt,
zeigt deshalb sein Lebenszeichen in Redis (``HEARTBEAT_KEY``, alle 30 Sekunden erneuert, läuft nach 3 Minuten ab),
nicht mehr allein ein Sync-Protokoll der letzten 30 Minuten.
"""

import logging
from datetime import timedelta

logger = logging.getLogger("insight_sync.daemon")

#: So lange darf ein Sync laufen, bevor sein Protokoll als fehlgeschlagen gilt
STALE_AFTER = timedelta(minutes=15)
#: Kanal für „Jetzt synchronisieren“ (Ingestor: ``src/scheduler/__init__.py``, ``TRIGGER_CHANNEL``)
TRIGGER_CHANNEL = "mandari:sync:trigger"
#: Lebenszeichen des Ingestor-Daemons (Ingestor: ``HEARTBEAT_KEY``)
HEARTBEAT_KEY = "mandari:ingestor:heartbeat"


def _redis():
    """Redis-Verbindung mit kurzen Zeitgrenzen: Ein fehlendes Redis soll keine Admin-Seite aufhalten."""
    import redis
    from django.conf import settings

    return redis.from_url(
        getattr(settings, "REDIS_URL", "redis://localhost:6379"),
        socket_connect_timeout=2,
        socket_timeout=2,
    )


#: Ohne Lebenszeichen gilt der Daemon als aktiv, wenn er in dieser Zeit ein Sync-Protokoll begonnen hat
ACTIVE_LOG_WINDOW = timedelta(minutes=30)
#: Woran der Daemon als aktiv erkannt wurde (``ingestor_activity``)
ACTIVE_BY_HEARTBEAT = "heartbeat"
ACTIVE_BY_SYNCLOG = "synclog"


def ingestor_activity() -> str | None:
    """
    Woran ist der Ingestor-Daemon als aktiv erkennbar? ``ACTIVE_BY_HEARTBEAT``, wenn sein Lebenszeichen in Redis
    steht, sonst ``ACTIVE_BY_SYNCLOG``, wenn er in den letzten 30 Minuten ein Sync-Protokoll begonnen hat (ältere
    Ingestoren ohne Lebenszeichen, Redis nicht erreichbar), sonst ``None``.
    """
    try:
        if _redis().exists(HEARTBEAT_KEY):
            return ACTIVE_BY_HEARTBEAT
    except Exception as e:  # noqa: BLE001 - ohne Redis entscheidet das Sync-Protokoll
        logger.debug("Lebenszeichen des Ingestors nicht lesbar: %s", type(e).__name__)
    try:
        from django.utils import timezone

        from .models import SyncLog

        cutoff = timezone.now() - ACTIVE_LOG_WINDOW
        return ACTIVE_BY_SYNCLOG if SyncLog.objects.filter(started_at__gte=cutoff).exists() else None
    except Exception:
        return None


def is_ingestor_active() -> bool:
    """Lebt der Ingestor-Daemon? Lebenszeichen in Redis, sonst ein Sync-Protokoll der letzten 30 Minuten."""
    return ingestor_activity() is not None


def trigger_sync(full: bool = False, source=None) -> bool:
    """
    Sendet ein Sync-Trigger-Event via Redis an den Ingestor, für alle aktiven Quellen oder eine (``source``).

    Liefert ``True``, wenn mindestens ein Ingestor-Daemon die Nachricht empfangen hat. Der Daemon startet die
    Quelle(n), sobald ein Platz im Pool frei ist; eine Quelle in Schonung startet er nicht.
    """
    import json

    try:
        from apps.common.observability import current_request_id, current_trace_context

        trace_id, span_id = current_trace_context()
        payload = {
            "full": full,
            "request_id": current_request_id() or None,
            "trace_id": trace_id or None,
            "span_id": span_id or None,  # Ingestor hängt seinen Sync-Span darunter
        }
        if source is not None:
            payload["source_id"] = str(source.pk)
            payload["source_url"] = source.url
        empfaenger = _redis().publish(TRIGGER_CHANNEL, json.dumps(payload))
        logger.info(
            "Sync-Trigger gesendet (full=%s, Quelle=%s, Empfänger=%s, request_id=%s)",
            full,
            payload.get("source_id", "alle"),
            empfaenger,
            payload["request_id"],
        )
        if not empfaenger:
            logger.warning("Sync-Trigger ohne Empfänger: Kein Ingestor-Daemon hört auf %s", TRIGGER_CHANNEL)
            return False
        return True
    except Exception as e:
        logger.warning("Sync-Trigger fehlgeschlagen: %s", type(e).__name__)
        return False


def cleanup_stale_syncs() -> int:
    """Markiert hängende Syncs (länger als ``STALE_AFTER``) als fehlgeschlagen; liefert ihre Anzahl."""
    from django.utils import timezone

    from .models import SyncLog

    jetzt = timezone.now()
    count = SyncLog.objects.filter(status="running", started_at__lt=jetzt - STALE_AFTER).update(
        status="failed",
        finished_at=jetzt,
        errors=["Sync-Timeout: Prozess hat nicht innerhalb von 15 Minuten geantwortet"],
    )
    if count:
        logger.warning("%d hängende Sync-Logs bereinigt", count)
    return count

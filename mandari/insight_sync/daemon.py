# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verbindung zum Ingestor: Status und Anstoß der Synchronisation.

Der eigentliche Sync läuft im Ingestor-Container (eigener Prozess). Django ist nur das
Kontrollzentrum (Logs anzeigen, Syncs per Redis anstoßen). Hängende Sync-Protokolle räumt der
Zeitplan ``insight_sync.schedules.haengende_syncs_bereinigen`` im Worker auf (Issue #515; bis dahin
ein Faden im Webprozess).
"""

import logging
from datetime import timedelta

logger = logging.getLogger("insight_sync.daemon")

#: So lange darf ein Sync laufen, bevor sein Protokoll als fehlgeschlagen gilt
STALE_AFTER = timedelta(minutes=15)


def is_ingestor_active() -> bool:
    """Prüft ob der Ingestor kürzlich ein SyncLog geschrieben hat (<30 Min)."""
    try:
        from django.utils import timezone

        from .models import SyncLog

        cutoff = timezone.now() - timedelta(minutes=30)
        return SyncLog.objects.filter(started_at__gte=cutoff).exists()
    except Exception:
        return False


def trigger_sync(full: bool = False):
    """Sendet ein Sync-Trigger-Event via Redis an den Ingestor."""
    import json

    try:
        import redis
        from django.conf import settings

        from apps.common.observability import current_request_id, current_trace_context

        trace_id, span_id = current_trace_context()
        payload = {
            "full": full,
            "request_id": current_request_id() or None,
            "trace_id": trace_id or None,
            "span_id": span_id or None,  # Ingestor hängt seinen Sync-Span darunter
        }
        r = redis.from_url(getattr(settings, "REDIS_URL", "redis://localhost:6379"))
        r.publish("mandari:sync:trigger", json.dumps(payload))
        logger.info("Sync-Trigger gesendet (full=%s, request_id=%s)", full, payload["request_id"])
        return True
    except Exception as e:
        logger.warning(f"Sync-Trigger fehlgeschlagen: {e}")
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

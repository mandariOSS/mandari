# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Automatischer Georeferenzierungs-Lauf (periodisch, begrenzt).

Wird nach Sync-Zyklen (sync_daemon) bzw. periodisch vom Sync-Watchdog
aufgerufen. Verarbeitet pro Lauf höchstens GEOREF_AUTO_LIMIT Papers mit
georef_status=pending und vorhandenem Text — nur der Regex/Gazetteer-Pass.
Der LLM-Pass läuft aus Kostengründen NIE automatisch (manuell via
`extract_locations --mode ai`).

Ein Cache-Lock verhindert parallele Läufe (mehrere Worker/Prozesse).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_LOCK_KEY = "georef:auto:lock"
_LOCK_TIMEOUT = 30 * 60  # Sicherheitsnetz, falls ein Lauf abbricht


def run_auto_georef_pass(limit: int | None = None) -> dict:
    """
    Führt einen begrenzten automatischen Georef-Lauf aus.

    1. Offizielle OParl-Locations frisch verknüpfter Papers übernehmen
    2. Regex/Gazetteer-Pass für Papers mit georef_status=pending

    Returns:
        Statistik-Dict (processed, completed, oparl_backfilled, skipped-Grund).
    """
    if not getattr(settings, "GEOREF_ENABLED", True):
        return {"skipped": "GEOREF_ENABLED=False"}
    if not getattr(settings, "GEOREF_AUTO_ENABLED", True):
        return {"skipped": "GEOREF_AUTO_ENABLED=False"}

    if limit is None:
        limit = getattr(settings, "GEOREF_AUTO_LIMIT", 50)
    if limit <= 0:
        return {"skipped": "limit<=0"}

    # Lock gegen parallele Läufe (mehrere Gunicorn-Worker / Daemon + Watchdog)
    if not cache.add(_LOCK_KEY, "1", timeout=_LOCK_TIMEOUT):
        return {"skipped": "lock"}

    try:
        return _run_pass(limit)
    except Exception:
        logger.exception("Automatischer Georef-Lauf fehlgeschlagen")
        return {"error": "exception"}
    finally:
        cache.delete(_LOCK_KEY)


def _run_pass(limit: int) -> dict:
    from django.db.models import Exists, OuterRef

    from insight_core.models import OParlFile, OParlPaper
    from insight_core.services.georeferencing import (
        process_paper_georef,
        update_paper_georef,
    )
    from insight_core.services.oparl_locations import apply_oparl_locations

    stats = {"oparl_backfilled": 0, "processed": 0, "completed": 0, "failed": 0}

    # 1. OParl-Locations übernehmen (billig, idempotent — apply_oparl_locations
    #    speichert nur bei tatsächlicher Änderung)
    backfill_qs = (
        OParlPaper.objects.filter(oparl_locations__isnull=False)
        .distinct()
        .prefetch_related("oparl_locations")
        .order_by("-updated_at")[: max(limit, 200)]
    )
    for paper in backfill_qs:
        try:
            if apply_oparl_locations(paper):
                stats["oparl_backfilled"] += 1
        except Exception:
            logger.exception("OParl-Location-Backfill fehlgeschlagen (paper=%s)", paper.id)

    # 2. Regex/Gazetteer-Pass für pending Papers mit extrahiertem Text.
    #    Nur Kommunen MIT importiertem Straßenverzeichnis — der automatische
    #    Lauf macht dadurch garantiert keine externen Geocoding-Calls
    #    (Legacy-Photon-Pfad bleibt manuellen extract_locations-Läufen
    #    vorbehalten).
    from insight_core.models import Street

    bodies_with_streets = Street.objects.values_list("body_id", flat=True).distinct()
    if not bodies_with_streets:
        stats["skipped"] = "kein Straßenverzeichnis importiert (import_streets)"
        return stats

    has_text = OParlFile.objects.filter(
        paper=OuterRef("pk"),
        text_extraction_status="completed",
        text_content__isnull=False,
    ).exclude(text_content="")
    queryset = (
        OParlPaper.objects.select_related("body")
        .filter(georef_status="pending", body_id__in=bodies_with_streets)
        .filter(Exists(has_text))
        .order_by("-date", "-oparl_created")[:limit]
    )

    for paper in queryset:
        stats["processed"] += 1
        try:
            paper.georef_status = "processing"
            paper.save(update_fields=["georef_status", "updated_at"])
            result = process_paper_georef(paper, mode="regex")
            update_paper_georef(paper, result)
            if result.get("status") == "completed":
                stats["completed"] += 1
        except Exception as exc:
            stats["failed"] += 1
            paper.georef_status = "failed"
            paper.georef_error = str(exc)[:500]
            paper.save(update_fields=["georef_status", "georef_error", "updated_at"])
            logger.exception("Georef fehlgeschlagen (paper=%s)", paper.id)

    if stats["processed"] or stats["oparl_backfilled"]:
        logger.info(
            "Auto-Georef: %d Papers verarbeitet (%d mit Orten), %d OParl-Backfills",
            stats["processed"],
            stats["completed"],
            stats["oparl_backfilled"],
        )
    return stats

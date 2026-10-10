# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Automatischer Georeferenzierungs-Lauf (periodisch, begrenzt).

Wird nach Sync-Zyklen (sync_daemon) bzw. periodisch vom Zeitplan ``verortung_automatisch``
(insight_core/schedules.py) aufgerufen. Verarbeitet pro Lauf höchstens GEOREF_AUTO_LIMIT Papers mit
georef_status=pending und vorhandenem Text — nur der Regex/Gazetteer-Pass.
Der LLM-Pass läuft aus Kostengründen NIE automatisch (manuell via
`extract_locations --mode ai`).

Ein Cache-Lock verhindert parallele Läufe (mehrere Worker/Prozesse).

Neuer Text stößt die Verortung zusätzlich direkt an (Issue #919, ADR Dokumentkette, Abschnitt 9): Das Abonnement
``insight.verortung`` (``georef_abonnement.py``) setzt den Vorgang auf ``pending`` und reiht mit
``TASKS_BACKEND=journal`` den Auftrag ``verortung_vorgang`` ein, der genau diesen Vorgang wie der Zeitplan
verortet. Der Zeitplan bleibt das Sicherheitsnetz. Zeitplan und Auftrag beanspruchen einen Vorgang nur aus
``pending`` (``processing``), keiner verortet ihn also doppelt.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from django.conf import settings
from django.core.cache import cache
from django.tasks import task

logger = logging.getLogger(__name__)

_LOCK_KEY = "georef:auto:lock"
_LOCK_TIMEOUT = 30 * 60  # Sicherheitsnetz, falls ein Lauf abbricht

#: Ergebniscodes des Auftrags ``verortung_vorgang`` (Protokoll und Tests)
VERORTET: Final = "verortet"
NICHT_ZU_TUN: Final = "nicht_zu_tun"
OHNE_STRASSEN: Final = "ohne_strassenverzeichnis"
ABGESCHALTET: Final = "abgeschaltet"


def auto_georef_enabled() -> bool:
    """Läuft die automatische Verortung (``GEOREF_ENABLED`` und ``GEOREF_AUTO_ENABLED``)?"""
    return bool(getattr(settings, "GEOREF_ENABLED", True)) and bool(getattr(settings, "GEOREF_AUTO_ENABLED", True))


def has_text_q() -> Any:
    """Bedingung „der Vorgang hat mindestens eine Anlage mit erkanntem Text“ (für ``filter``)."""
    from django.db.models import Exists, OuterRef

    from insight_core.models import OParlFile

    return Exists(
        OParlFile.objects.filter(
            paper=OuterRef("pk"),
            text_extraction_status="completed",
            text_content__isnull=False,
        ).exclude(text_content="")
    )


def _georef_one(paper: Any, stats: dict) -> bool:
    """
    Regex/Gazetteer-Pass für einen Vorgang; ``False``, wenn ihn inzwischen ein anderer Lauf beansprucht hat.

    Beansprucht wird nur aus ``pending`` (bedingtes ``UPDATE``), damit Zeitplan und Auftrag ``verortung_vorgang``
    denselben Vorgang nicht gleichzeitig verorten.
    """
    from django.utils import timezone

    from insight_core.models import OParlPaper
    from insight_core.services.georeferencing import process_paper_georef, update_paper_georef

    if not OParlPaper.objects.filter(pk=paper.pk, georef_status="pending").update(
        georef_status="processing", updated_at=timezone.now()
    ):
        return False
    paper.georef_status = "processing"
    stats["processed"] += 1
    try:
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
    return True


@task
def verortung_vorgang(paper_id: str) -> str:
    """
    Auftrag: einen Vorgang nach neuem Text verorten (Regex/Gazetteer-Pass wie der Zeitplan, nie KI).

    Eingereiht vom Abonnement ``insight.verortung``. Idempotent: Nur ein nicht gelöschter Vorgang im Stand
    ``pending`` mit erkanntem Text wird verortet; sonst endet der Auftrag ohne Wirkung. Wie im Zeitplan nur für
    Kommunen mit Straßenverzeichnis, damit kein externer Geocoding-Dienst gefragt wird; die übrigen bleiben
    ``pending``. Rückgabe: Ergebniscode.
    """
    from insight_core.models import OParlPaper, Street

    if not auto_georef_enabled():
        return ABGESCHALTET
    paper = (
        OParlPaper.objects.select_related("body")
        .filter(pk=paper_id, georef_status="pending", deleted=False)
        .filter(has_text_q())
        .first()
    )
    if paper is None:
        return NICHT_ZU_TUN
    if paper.body_id is None or not Street.objects.filter(body_id=paper.body_id).exists():
        return OHNE_STRASSEN
    stats = {"processed": 0, "completed": 0, "failed": 0}
    if not _georef_one(paper, stats):
        return NICHT_ZU_TUN
    logger.info("Verortung nach neuem Text: Vorgang %s (%s)", paper.pk, paper.georef_status)
    return VERORTET


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


def _papers_with_stale_oparl_locations(limit: int) -> list:
    """
    Vorgänge ohne verknüpften OParl-Ort, deren Verortung noch Einträge mit Herkunft ``oparl`` trägt.

    Der Ingestor entfernt die Verknüpfung, wenn die Quelle einen Ort nicht mehr nennt (Issue #553);
    die übernommenen Koordinaten räumt ``apply_oparl_locations`` hier auf. Gesucht wird über die
    Tabelle der Verortungen (automatische Zeilen mit Herkunft ``oparl``), nicht im JSON.
    """
    from insight_core.models import OParlPaper, PaperLocation

    stale = PaperLocation.objects.filter(source="oparl", status=PaperLocation.STATUS_AUTO)
    paper_ids = stale.filter(paper__oparl_locations__isnull=True).values_list("paper_id", flat=True).distinct()[:limit]
    return list(OParlPaper.objects.filter(pk__in=list(paper_ids)).prefetch_related("oparl_locations"))


def _run_pass(limit: int) -> dict:
    from insight_core.models import OParlPaper
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
    for paper in [*backfill_qs, *_papers_with_stale_oparl_locations(max(limit, 200))]:
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

    queryset = (
        OParlPaper.objects.select_related("body")
        .filter(georef_status="pending", deleted=False, body_id__in=bodies_with_streets)
        .filter(has_text_q())
        .order_by("-date", "-oparl_created")[:limit]
    )

    for paper in queryset:
        # Hat ein Auftrag verortung_vorgang ihn inzwischen beansprucht, zählt er hier nicht
        _georef_one(paper, stats)

    if stats["processed"] or stats["oparl_backfilled"]:
        logger.info(
            "Auto-Georef: %d Papers verarbeitet (%d mit Orten), %d OParl-Backfills",
            stats["processed"],
            stats["completed"],
            stats["oparl_backfilled"],
        )
    return stats

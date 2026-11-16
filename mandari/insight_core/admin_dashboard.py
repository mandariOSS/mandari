# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dashboard-Konfiguration für Django Unfold Admin.

Zeigt Statistiken und Übersichten für das Mandari-Projekt.
"""

import logging
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.common.formatting import MONTH_ABBREVIATIONS

# Diagrammdaten landen im Skript der Admin-Startseite; Kommunennamen stammen aus den Quellen
from .seo import script_json

logger = logging.getLogger(__name__)

#: Vorberechnete Kennzahlen der Admin-Startseite (Issue #1409)
KENNZAHLEN_KEY = "insight:admin-startseite:kennzahlen:v1"
#: ``check_source_health`` rechnet sie stündlich neu; die Frist überbrückt zwei ausgefallene Läufe
KENNZAHLEN_TTL = 3 * 3600


def _kennzahlen_berechnen() -> dict:
    """Zählwerte und Diagrammdaten über die großen OParl-Tabellen (in Produktion rund 11 s)."""
    from insight_core.models import (
        OParlAgendaItem,
        OParlBody,
        OParlFile,
        OParlMeeting,
        OParlMembership,
        OParlOrganization,
        OParlPaper,
        OParlPerson,
        OParlSource,
    )

    now = timezone.now()
    recent_date = now - timedelta(days=7)
    twelve_months_ago = now - timedelta(days=365)

    stats = {
        "sources": OParlSource.objects.count(),
        "sources_active": OParlSource.objects.filter(is_active=True).count(),
        "bodies": OParlBody.objects.count(),
        "organizations": OParlOrganization.objects.count(),
        "persons": OParlPerson.objects.count(),
        "meetings": OParlMeeting.objects.count(),
        "meetings_upcoming": OParlMeeting.objects.filter(start__gte=now).count(),
        "papers": OParlPaper.objects.count(),
        "papers_recent": OParlPaper.objects.filter(created_at__gte=recent_date).count(),
        "agenda_items": OParlAgendaItem.objects.count(),
        "files": OParlFile.objects.count(),
        "memberships": OParlMembership.objects.count(),
    }

    papers_monthly = list(
        OParlPaper.objects.filter(date__gte=twelve_months_ago, date__isnull=False)
        .annotate(month=TruncMonth("date"))
        .values("month")
        .annotate(count=Count("id"))
        .order_by("month")
    )
    meetings_monthly = list(
        OParlMeeting.objects.filter(start__gte=twelve_months_ago)
        .annotate(month=TruncMonth("start"))
        .values("month")
        .annotate(count=Count("id"))
        .order_by("month")
    )
    meetings_by_month = {m["month"]: m["count"] for m in meetings_monthly}
    chart_months, chart_papers, chart_meetings = [], [], []
    for item in papers_monthly:
        if item["month"]:
            chart_months.append(MONTH_ABBREVIATIONS[item["month"].month - 1])
            chart_papers.append(item["count"])
            chart_meetings.append(meetings_by_month.get(item["month"], 0))

    papers_by_body = list(OParlPaper.objects.values("body__name").annotate(count=Count("id")).order_by("-count")[:10])

    return {
        "stats": stats,
        "chart_months": chart_months,
        "chart_papers": chart_papers,
        "chart_meetings": chart_meetings,
        "papers_by_body_labels": [p["body__name"] or "Unbekannt" for p in papers_by_body],
        "papers_by_body_data": [p["count"] for p in papers_by_body],
        "ermittelt_am": now,
    }


def dashboard_kennzahlen(*, refresh: bool = False) -> dict:
    """
    Kennzahlen der Admin-Startseite aus dem Cache (Issue #1409).

    Die Zählungen über Vorgänge, Dateien und Tagesordnungspunkte dauern in Produktion rund 11 s. Der stündliche
    Prüflauf ``check_source_health`` rechnet sie vor (``refresh=True``); fehlt der Stand, rechnet ihn der erste
    Aufruf einmal selbst.
    """
    if not refresh:
        try:
            stand = cache.get(KENNZAHLEN_KEY)
        except Exception:  # noqa: BLE001 – ohne Cache rechnet der Aufruf selbst
            stand = None
        if stand is not None:
            return stand
    stand = _kennzahlen_berechnen()
    try:
        cache.set(KENNZAHLEN_KEY, stand, KENNZAHLEN_TTL)
    except Exception:  # noqa: BLE001
        logger.warning("Kennzahlen der Admin-Startseite nicht speicherbar", exc_info=True)
    return stand


def dashboard_callback(request, context):
    """
    Bereitet die Dashboard-Daten für die Admin-Startseite vor.

    Diese Funktion wird von Unfold aufgerufen und fügt
    statistische Daten zum Template-Context hinzu.
    """
    from insight_core.models import OParlMeeting, OParlPaper, OParlSource

    # Zählwerte und Diagramme: vorberechnet (Issue #1409)
    kennzahlen = dashboard_kennzahlen()
    context["stats"] = kennzahlen["stats"]
    context["kennzahlen_stand"] = kennzahlen["ermittelt_am"]

    # Betriebsstatus (Quellen-Gesundheit, Systemchecks, Handlungsbedarf)
    try:
        from insight_core.services.source_health import collect_health

        context["health"] = collect_health()
    except Exception:
        context["health"] = None

    # Letzte Sync-Aktivität mit Stunden-Berechnung
    sources = OParlSource.objects.filter(is_active=True).order_by("-last_sync")[:5]
    for source in sources:
        if source.last_sync:
            age = timezone.now() - source.last_sync
            source.hours_since_sync = age.total_seconds() / 3600
        else:
            source.hours_since_sync = 999
    context["recent_sources"] = sources

    # Sync-Verwaltung: Logs, laufender Sync, Daemon-Status
    try:
        from insight_sync import daemon
        from insight_sync.models import SyncLog

        context["recent_sync_logs"] = SyncLog.objects.order_by("-started_at")[:5]
        context["is_syncing"] = SyncLog.objects.filter(status=SyncLog.Status.RUNNING).exists()
        context["daemon_running"] = daemon.is_ingestor_active()
    except Exception:
        context["recent_sync_logs"] = []
        context["is_syncing"] = False
        context["daemon_running"] = False

    # Anstehende Sitzungen
    context["upcoming_meetings"] = OParlMeeting.objects.filter(start__gte=timezone.now(), cancelled=False).order_by(
        "start"
    )[:5]

    # Kürzlich hinzugefügte Vorgänge
    context["recent_papers"] = OParlPaper.objects.order_by("-created_at")[:5]

    # === CHART DATA ===
    context["chart_months"] = script_json(kennzahlen["chart_months"])
    context["chart_papers"] = script_json(kennzahlen["chart_papers"])
    context["chart_meetings"] = script_json(kennzahlen["chart_meetings"])
    context["papers_by_body_labels"] = script_json(kennzahlen["papers_by_body_labels"])
    context["papers_by_body_data"] = script_json(kennzahlen["papers_by_body_data"])

    return context

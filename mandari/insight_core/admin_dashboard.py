"""
Dashboard-Konfiguration für Django Unfold Admin.

Zeigt Statistiken und Übersichten für das Mandari-Projekt.
"""

import json
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.utils import timezone
from datetime import timedelta


def dashboard_callback(request, context):
    """
    Bereitet die Dashboard-Daten für die Admin-Startseite vor.

    Diese Funktion wird von Unfold aufgerufen und fügt
    statistische Daten zum Template-Context hinzu.
    """
    from insight_core.models import (
        OParlSource,
        OParlBody,
        OParlOrganization,
        OParlPerson,
        OParlMeeting,
        OParlPaper,
        OParlAgendaItem,
        OParlFile,
        OParlMembership,
    )
    from insight_content.models import BlogPost, Release

    # Zeitraum für "kürzlich"
    recent_date = timezone.now() - timedelta(days=7)
    twelve_months_ago = timezone.now() - timedelta(days=365)

    # OParl Statistiken
    context["stats"] = {
        "sources": OParlSource.objects.count(),
        "sources_active": OParlSource.objects.filter(is_active=True).count(),
        "bodies": OParlBody.objects.count(),
        "organizations": OParlOrganization.objects.count(),
        "persons": OParlPerson.objects.count(),
        "meetings": OParlMeeting.objects.count(),
        "meetings_upcoming": OParlMeeting.objects.filter(
            start__gte=timezone.now()
        ).count(),
        "papers": OParlPaper.objects.count(),
        "papers_recent": OParlPaper.objects.filter(
            created_at__gte=recent_date
        ).count(),
        "agenda_items": OParlAgendaItem.objects.count(),
        "files": OParlFile.objects.count(),
        "memberships": OParlMembership.objects.count(),
    }

    # Content Statistiken
    context["content_stats"] = {
        "blog_posts": BlogPost.objects.count(),
        "blog_published": BlogPost.objects.filter(
            status=BlogPost.Status.PUBLISHED
        ).count(),
        "releases": Release.objects.count(),
        "releases_published": Release.objects.filter(is_published=True).count(),
    }

    # Letzte Sync-Aktivität mit Stunden-Berechnung
    sources = OParlSource.objects.filter(is_active=True).order_by("-last_sync")[:5]
    for source in sources:
        if source.last_sync:
            age = timezone.now() - source.last_sync
            source.hours_since_sync = age.total_seconds() / 3600
        else:
            source.hours_since_sync = 999
    context["recent_sources"] = sources

    # Anstehende Sitzungen
    context["upcoming_meetings"] = OParlMeeting.objects.filter(
        start__gte=timezone.now(),
        cancelled=False
    ).order_by("start")[:5]

    # Kürzlich hinzugefügte Vorgänge
    context["recent_papers"] = OParlPaper.objects.order_by(
        "-created_at"
    )[:5]

    # === CHART DATA ===

    # German month names
    month_names = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
                   "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]

    # Papers per month
    papers_monthly = list(
        OParlPaper.objects
        .filter(date__gte=twelve_months_ago, date__isnull=False)
        .annotate(month=TruncMonth("date"))
        .values("month")
        .annotate(count=Count("id"))
        .order_by("month")
    )

    # Meetings per month
    meetings_monthly = list(
        OParlMeeting.objects
        .filter(start__gte=twelve_months_ago)
        .annotate(month=TruncMonth("start"))
        .values("month")
        .annotate(count=Count("id"))
        .order_by("month")
    )

    # Build chart data
    chart_months = []
    chart_papers = []
    chart_meetings = []

    # Create month lookup
    meetings_by_month = {m["month"]: m["count"] for m in meetings_monthly}

    for item in papers_monthly:
        if item["month"]:
            chart_months.append(month_names[item["month"].month - 1])
            chart_papers.append(item["count"])
            chart_meetings.append(meetings_by_month.get(item["month"], 0))

    context["chart_months"] = json.dumps(chart_months)
    context["chart_papers"] = json.dumps(chart_papers)
    context["chart_meetings"] = json.dumps(chart_meetings)

    # Papers by body (for doughnut chart)
    papers_by_body = list(
        OParlPaper.objects
        .values("body__name")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    context["papers_by_body_labels"] = json.dumps(
        [p["body__name"] or "Unbekannt" for p in papers_by_body]
    )
    context["papers_by_body_data"] = json.dumps(
        [p["count"] for p in papers_by_body]
    )

    return context

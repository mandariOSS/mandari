# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin Dashboard für Mandari Insight.

Zeigt Statistiken und Charts für OParl-Daten.
"""

from datetime import timedelta
from django.db.models import Count
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone


def get_sync_status_data():
    """Get sync status for all sources."""
    from insight_core.models import OParlSource

    sources = OParlSource.objects.filter(is_active=True)
    data = []

    for source in sources:
        if not source.last_sync:
            status = "never"
            status_label = "Nie"
            status_color = "#dc2626"
        else:
            age = timezone.now() - source.last_sync
            hours = age.total_seconds() / 3600

            if hours < 0.5:
                status = "current"
                status_label = "Aktuell"
                status_color = "#16a34a"
            elif hours < 2:
                status = "ok"
                status_label = "OK"
                status_color = "#65a30d"
            elif hours < 24:
                status = "stale"
                status_label = "Veraltet"
                status_color = "#ca8a04"
            else:
                status = "old"
                status_label = "Sehr alt"
                status_color = "#dc2626"

        data.append({
            "name": source.name,
            "url": source.url,
            "status": status,
            "status_label": status_label,
            "status_color": status_color,
            "last_sync": source.last_sync,
            "body_count": source.bodies.count(),
        })

    return data


def get_entity_counts():
    """Get counts for all OParl entity types."""
    from insight_core.models import (
        OParlSource, OParlBody, OParlOrganization, OParlPerson,
        OParlMeeting, OParlPaper, OParlAgendaItem, OParlFile,
        OParlMembership, OParlLocation, OParlConsultation,
    )

    return {
        "sources": OParlSource.objects.filter(is_active=True).count(),
        "bodies": OParlBody.objects.count(),
        "organizations": OParlOrganization.objects.count(),
        "persons": OParlPerson.objects.count(),
        "meetings": OParlMeeting.objects.count(),
        "papers": OParlPaper.objects.count(),
        "agenda_items": OParlAgendaItem.objects.count(),
        "files": OParlFile.objects.count(),
        "memberships": OParlMembership.objects.count(),
        "locations": OParlLocation.objects.count(),
        "consultations": OParlConsultation.objects.count(),
    }


def get_papers_per_month():
    """Get paper counts per month for the last 12 months."""
    from insight_core.models import OParlPaper

    twelve_months_ago = timezone.now() - timedelta(days=365)

    papers = (
        OParlPaper.objects
        .filter(date__gte=twelve_months_ago)
        .annotate(month=TruncMonth("date"))
        .values("month")
        .annotate(count=Count("id"))
        .order_by("month")
    )

    return list(papers)


def get_meetings_per_month():
    """Get meeting counts per month for the last 12 months."""
    from insight_core.models import OParlMeeting

    twelve_months_ago = timezone.now() - timedelta(days=365)

    meetings = (
        OParlMeeting.objects
        .filter(start__gte=twelve_months_ago)
        .annotate(month=TruncMonth("start"))
        .values("month")
        .annotate(count=Count("id"))
        .order_by("month")
    )

    return list(meetings)


def get_papers_by_body():
    """Get paper counts per body."""
    from insight_core.models import OParlPaper

    papers = (
        OParlPaper.objects
        .values("body__name")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    return list(papers)


def get_recent_activity():
    """Get recent sync and data activity."""
    from insight_core.models import OParlPaper, OParlMeeting

    recent_papers = (
        OParlPaper.objects
        .order_by("-created_at")[:5]
        .values("reference", "name", "body__name", "created_at")
    )

    recent_meetings = (
        OParlMeeting.objects
        .order_by("-created_at")[:5]
        .values("name", "start", "body__name", "created_at")
    )

    return {
        "papers": list(recent_papers),
        "meetings": list(recent_meetings),
    }


def dashboard_callback(request, context):
    """
    Callback function for Unfold dashboard.

    Called by Unfold to populate the dashboard context.
    """
    # Entity counts for stat cards
    counts = get_entity_counts()

    # Papers per month for chart
    papers_monthly = get_papers_per_month()
    meetings_monthly = get_meetings_per_month()

    # Sync status
    sync_status = get_sync_status_data()

    # Papers by body for pie chart
    papers_by_body = get_papers_by_body()

    # Format data for Chart.js
    months = []
    paper_counts = []
    meeting_counts = []

    # German month names
    month_names = [
        "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
        "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"
    ]

    for item in papers_monthly:
        if item["month"]:
            months.append(month_names[item["month"].month - 1])
            paper_counts.append(item["count"])

    for item in meetings_monthly:
        meeting_counts.append(item["count"])

    # Pad meeting_counts to match paper_counts length
    while len(meeting_counts) < len(paper_counts):
        meeting_counts.append(0)

    context.update({
        # Stats
        "entity_counts": counts,
        "total_entities": sum(counts.values()) - counts["sources"] - counts["bodies"],

        # Sync status
        "sync_status": sync_status,
        "sync_ok_count": len([s for s in sync_status if s["status"] in ("current", "ok")]),
        "sync_stale_count": len([s for s in sync_status if s["status"] in ("stale", "old", "never")]),

        # Chart data
        "chart_months": months,
        "chart_papers": paper_counts,
        "chart_meetings": meeting_counts,

        # Papers by body
        "papers_by_body": papers_by_body,
        "papers_by_body_labels": [p["body__name"] or "Unbekannt" for p in papers_by_body],
        "papers_by_body_data": [p["count"] for p in papers_by_body],

        # Recent activity
        "recent_activity": get_recent_activity(),
    })

    return context

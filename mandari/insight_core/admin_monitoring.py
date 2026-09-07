# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin-Seite „Betriebsmonitor“: Quellen-Gesundheit, Systemchecks, Handlungsbedarf."""

from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render


@staff_member_required
def monitoring_view(request):
    from insight_core.services.source_health import collect_health

    health = collect_health()
    try:
        from insight_sync.models import SyncLog

        recent_logs = list(SyncLog.objects.select_related("source").order_by("-started_at")[:20])
    except Exception:
        recent_logs = []

    context = {
        **admin.site.each_context(request),
        "title": "Betriebsmonitor",
        "health": health,
        "recent_logs": recent_logs,
    }
    return render(request, "admin/monitoring.html", context)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Faction meeting views for the Work module.

Simplified architecture: 4 views instead of 13.
- FactionMeetingListView: List + Create (POST)
- FactionMeetingDetailView: Detail/Protocol page
- FactionActionView: Central HTMX action handler
- FactionSettingsView: Legacy redirect to organization settings
"""

import logging
from datetime import datetime

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from ..models import (
    FactionAttendance,
    FactionMeeting,
)

logger = logging.getLogger(__name__)
from ._helpers import _get_meeting_context


class FactionMeetingListView(WorkViewMixin, TemplateView):
    """List of faction meetings. POST creates a new meeting (from modal)."""

    template_name = "work/faction/list.html"
    permission_required = "faction.view_public"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "faction"

        meetings = FactionMeeting.objects.filter(organization=self.organization).select_related(
            "created_by__user", "schedule"
        )

        # Filter by status
        status = self.request.GET.get("status")
        if status:
            meetings = meetings.filter(status=status)
            context["selected_status"] = status

        # Filter by time — meetings become "past" at midnight, not at start time
        time_filter = self.request.GET.get("time", "upcoming")
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if time_filter == "upcoming":
            meetings = meetings.filter(start__gte=today_start)
            context["selected_time"] = "upcoming"
        elif time_filter == "past":
            meetings = meetings.filter(start__lt=today_start)
            context["selected_time"] = "past"
        else:
            context["selected_time"] = "all"

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            meetings = meetings.filter(Q(title__icontains=search) | Q(description__icontains=search))
            context["search_query"] = search

        # Order
        if time_filter == "upcoming":
            meetings = meetings.order_by("start")
        else:
            meetings = meetings.order_by("-start")

        # Annotate
        meetings = meetings.annotate(
            attendee_count=Count("attendances", filter=Q(attendances__status__in=["confirmed", "present"]))
        )

        # Pagination
        paginator = Paginator(meetings, 15)
        page = self.request.GET.get("page", 1)
        context["meetings"] = paginator.get_page(page)

        # Statistics
        all_meetings = FactionMeeting.objects.filter(organization=self.organization)
        context["stats"] = {
            "total": all_meetings.count(),
            "upcoming": all_meetings.filter(start__gte=today_start).count(),
            "pending_protocol": all_meetings.filter(status="completed", protocol_approved=False).count(),
        }

        context["status_choices"] = FactionMeeting.STATUS_CHOICES

        return context

    def post(self, request, *args, **kwargs):
        """Create a new meeting from the modal form."""
        if not self.membership.has_permission("faction.create"):
            messages.error(request, "Keine Berechtigung zum Erstellen von Sitzungen.")
            return redirect("work:faction", org_slug=self.organization.slug)

        # Combine date and time
        start_date = request.POST.get("start_date")
        start_time = request.POST.get("start_time", "18:00")

        if not start_date:
            messages.error(request, "Datum ist erforderlich.")
            return redirect("work:faction", org_slug=self.organization.slug)

        title = request.POST.get("title", "").strip()
        if not title:
            messages.error(request, "Titel ist erforderlich.")
            return redirect("work:faction", org_slug=self.organization.slug)

        try:
            start_datetime = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
            start_datetime = (
                timezone.make_aware(start_datetime) if timezone.is_naive(start_datetime) else start_datetime
            )
        except ValueError:
            messages.error(request, "Ungültiges Datum oder Uhrzeit.")
            return redirect("work:faction", org_slug=self.organization.slug)

        # Create meeting
        meeting = FactionMeeting(
            organization=self.organization,
            created_by=self.membership,
            title=title,
            start=start_datetime,
            location=request.POST.get("location", ""),
            is_virtual=request.POST.get("is_virtual") == "on",
            video_link=request.POST.get("video_link", "") if request.POST.get("is_virtual") == "on" else "",
            description=request.POST.get("description", ""),
            status="draft" if request.POST.get("save_as") == "draft" else "planned",
            meeting_number=FactionMeeting.get_next_meeting_number(self.organization),
        )

        # Find and link previous meeting — nur wenn die Vorsitzung noch
        # nicht verkettet ist (OneToOne; Muster aus der Sitzungserzeugung #61)
        previous = FactionMeeting.find_previous_meeting(self.organization, before_date=meeting.start)
        if previous is not None and FactionMeeting.objects.filter(previous_meeting=previous).exists():
            previous = None
        meeting.previous_meeting = previous
        meeting.save()

        # Create attendance records for all active members
        for member in self.organization.memberships.filter(is_active=True):
            FactionAttendance.objects.create(meeting=meeting, membership=member, status="invited")

        # Auto-create approval agenda item if enabled — über den Service, damit
        # das Vorprotokoll gleichzeitig in den Status "Zur Genehmigung" wechselt
        faction_settings = meeting.get_faction_settings()
        if faction_settings.get("auto_create_approval_item", True):
            from ..services import ProtocolApprovalService

            ProtocolApprovalService.auto_create_approval_item(meeting)

        messages.success(request, "Sitzung erfolgreich erstellt.")
        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)


# ---------------------------------------------------------------------------
# 2. Detail (= Protocol page)
# ---------------------------------------------------------------------------


class FactionMeetingDetailView(WorkViewMixin, TemplateView):
    """Combined detail + protocol page."""

    template_name = "work/faction/detail.html"
    permission_required = "faction.view_public"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "faction"

        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        context.update(_get_meeting_context(self, meeting))
        return context


# ---------------------------------------------------------------------------
# 3. Central Action Handler
# ---------------------------------------------------------------------------

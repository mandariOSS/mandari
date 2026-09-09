# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsliste, Kalender und Sitzungsdetail der Vorbereitung.
"""

from datetime import datetime

from django.http import JsonResponse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors
from ..serializers import serialize_calendar_event


class MeetingListView(WorkViewMixin, TemplateView):
    """List of OParl meetings for preparation."""

    template_name = "work/meetings/list.html"
    permission_required = "meetings.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        bodies = selectors.organization_bodies(self.organization)
        if bodies is None:
            context["has_body"] = False
            context["meetings"] = []
            return context

        context["has_body"] = True
        context["bodies"] = bodies
        context["has_multiple_bodies"] = bodies.count() > 1
        now = timezone.now()

        time_filter = self.request.GET.get("time", "upcoming")
        committee_filter = self.request.GET.get("committee", "")
        search_query = self.request.GET.get("q", "").strip()
        view_mode = self.request.GET.get("view", "my")

        assigned = selectors.assigned_committees(self.membership, bodies)
        meetings = selectors.filter_meetings(
            selectors.meetings_for_list(bodies, time_filter, now),
            committee_ids=[c.id for c in assigned] if view_mode == "my" else [],
            committee_filter=committee_filter,
            search_query=search_query,
        )
        selectors.annotate_meetings_for_list(meetings, self.organization)

        context.update(
            {
                "meetings": meetings,
                "assigned_committees": assigned,
                "all_committees": selectors.committee_choices(bodies),
                "time_filter": time_filter,
                "committee_filter": committee_filter,
                "search_query": search_query,
                "view_mode": view_mode,
                "has_assignments": bool(assigned),
                "now": now,
            }
        )
        return context


class MeetingCalendarView(WorkViewMixin, TemplateView):
    """Calendar view for meetings."""

    template_name = "work/meetings/calendar.html"
    permission_required = "meetings.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"
        return context


class MeetingCalendarEventsView(WorkViewMixin, View):
    """JSON endpoint for calendar events."""

    permission_required = "meetings.view"

    def get(self, request, *args, **kwargs):
        bodies = selectors.organization_bodies(self.organization)
        if bodies is None:
            return JsonResponse([], safe=False)

        try:
            start = datetime.fromisoformat(request.GET.get("start", "").replace("Z", "+00:00"))
            end = datetime.fromisoformat(request.GET.get("end", "").replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return JsonResponse([], safe=False)

        meetings = selectors.meetings_between(bodies, start, end)
        return JsonResponse([serialize_calendar_event(m, self.organization.slug) for m in meetings], safe=False)


class MeetingDetailView(WorkViewMixin, TemplateView):
    """Meeting detail view with agenda items."""

    template_name = "work/meetings/detail.html"
    permission_required = "meetings.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        bodies = selectors.organization_bodies(self.organization)
        if bodies is None:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = selectors.get_meeting_or_404(bodies, self.kwargs["meeting_id"], with_agenda=True)
        selectors.attach(meeting, committee_name=selectors.committee_name_for_meeting(meeting, {}))

        context["meeting"] = meeting
        context["agenda_items"] = selectors.sorted_agenda_items(meeting)
        context["preparation"] = selectors.get_preparation(self.organization, meeting)
        context["is_upcoming"] = meeting.start and meeting.start > timezone.now()
        return context

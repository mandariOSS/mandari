# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation views for the Work module.

Org-weite Sitzungsvorbereitung mit 5 Sektionen pro TOP:
1. Position/Beschluss (org-weit)
2. Private Notizen (pro User)
3. Redebeitrag (pro User, teilbar)
4. Fraktionsdiskussion (org-weit)
5. Dokumente (org-weit)
"""

from datetime import datetime, timedelta

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from insight_core.models import OParlMeeting, OParlOrganization

from ..models import (
    MeetingPreparation,
)
from ._helpers import natural_sort_key

# =============================================================================
# MEETING LIST + CALENDAR
# =============================================================================


class MeetingListView(WorkViewMixin, TemplateView):
    """List of OParl meetings for preparation."""

    template_name = "work/meetings/list.html"
    permission_required = "meetings.view"

    @staticmethod
    def _get_organization_name(meeting, org_cache):
        """Extract organization name from meeting."""
        try:
            orgs = meeting.organizations.all()
            if orgs:
                return orgs[0].name
        except Exception:
            pass

        try:
            raw = meeting.raw_json or {}
            orgs = raw.get("organization", [])
            if isinstance(orgs, list) and orgs:
                org_url = orgs[0]
                if org_url in org_cache:
                    return org_cache[org_url]
                org_obj = OParlOrganization.objects.filter(external_id=org_url).first()
                if org_obj:
                    org_cache[org_url] = org_obj.name
                    return org_obj.name
        except Exception:
            pass

        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        organization = self.organization
        membership = self.membership
        bodies = organization.get_all_bodies() if organization else None

        if bodies is None or not bodies.exists():
            context["has_body"] = False
            context["meetings"] = []
            return context

        context["has_body"] = True
        context["bodies"] = bodies
        context["has_multiple_bodies"] = bodies.count() > 1
        now = timezone.now()

        # Filters
        time_filter = self.request.GET.get("time", "upcoming")
        committee_filter = self.request.GET.get("committee", "")
        search_query = self.request.GET.get("q", "").strip()
        view_mode = self.request.GET.get("view", "my")

        # Get assigned committees
        assigned_committees = []
        if membership:
            assigned_committees = list(membership.oparl_committees.filter(body__in=bodies))

        assigned_committee_ids = [c.id for c in assigned_committees]

        # Build queryset
        meetings_qs = OParlMeeting.objects.filter(body__in=bodies).prefetch_related("organizations")

        if time_filter == "upcoming":
            meetings_qs = meetings_qs.filter(start__gte=now - timedelta(hours=2)).order_by("start")
        elif time_filter == "past":
            meetings_qs = meetings_qs.filter(start__lt=now).order_by("-start")
        else:
            meetings_qs = meetings_qs.order_by("-start")

        # Limit to 180 days
        if time_filter in ("upcoming", "past"):
            cutoff = now + timedelta(days=180) if time_filter == "upcoming" else now - timedelta(days=180)
            if time_filter == "upcoming":
                meetings_qs = meetings_qs.filter(start__lte=cutoff)
            else:
                meetings_qs = meetings_qs.filter(start__gte=cutoff)

        meetings = list(meetings_qs[:100])

        # Filter by view mode (my committees only)
        if view_mode == "my" and assigned_committee_ids:
            meetings = [m for m in meetings if any(org.id in assigned_committee_ids for org in m.organizations.all())]

        # Committee filter
        if committee_filter:
            meetings = [m for m in meetings if any(str(org.id) == committee_filter for org in m.organizations.all())]

        # Search
        if search_query:
            q = search_query.lower()
            meetings = [m for m in meetings if q in (m.name or "").lower()]

        # Add organization name
        org_cache = {}
        for meeting in meetings:
            meeting.committee_name = self._get_organization_name(meeting, org_cache)

        # Check which meetings are prepared (org-level now)
        prepared_meeting_ids = set(
            MeetingPreparation.objects.filter(organization=organization, is_prepared=True).values_list(
                "meeting_id", flat=True
            )
        )
        for meeting in meetings:
            meeting.is_user_prepared = meeting.id in prepared_meeting_ids

        # All committees for filter dropdown
        all_committees = list(
            OParlOrganization.objects.filter(body__in=bodies, organization_type__icontains="committee")
            .order_by("name")
            .values("id", "name")
        )

        has_assignments = bool(assigned_committees)

        context.update(
            {
                "meetings": meetings,
                "assigned_committees": assigned_committees,
                "all_committees": all_committees,
                "time_filter": time_filter,
                "committee_filter": committee_filter,
                "search_query": search_query,
                "view_mode": view_mode,
                "has_assignments": has_assignments,
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
        organization = self.organization
        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists():
            return JsonResponse([], safe=False)

        start_str = request.GET.get("start", "")
        end_str = request.GET.get("end", "")

        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return JsonResponse([], safe=False)

        meetings = OParlMeeting.objects.filter(body__in=bodies, start__gte=start, start__lte=end).prefetch_related(
            "organizations"
        )

        events = []
        for meeting in meetings:
            org_name = ""
            try:
                orgs = meeting.organizations.all()
                if orgs:
                    org_name = orgs[0].name
            except Exception:
                pass

            events.append(
                {
                    "id": str(meeting.id),
                    "title": meeting.name or org_name or "Sitzung",
                    "start": meeting.start.isoformat() if meeting.start else None,
                    "end": meeting.end.isoformat() if meeting.end else None,
                    "url": f"/work/{organization.slug}/meetings/{meeting.id}/",
                    "extendedProps": {"committee": org_name, "cancelled": meeting.cancelled},
                    "color": "#ef4444" if meeting.cancelled else None,
                }
            )

        return JsonResponse(events, safe=False)


# =============================================================================
# MEETING DETAIL
# =============================================================================


class MeetingDetailView(WorkViewMixin, TemplateView):
    """Meeting detail view with agenda items."""

    template_name = "work/meetings/detail.html"
    permission_required = "meetings.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization
        bodies = organization.get_all_bodies() if organization else None

        if bodies is None or not bodies.exists():
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(
            OParlMeeting.objects.prefetch_related("organizations", "agenda_items"),
            id=meeting_id,
            body__in=bodies,
        )

        meeting.committee_name = MeetingListView._get_organization_name(meeting, {})
        agenda_items = sorted(meeting.agenda_items.all(), key=natural_sort_key)

        # Org-level preparation
        preparation = MeetingPreparation.objects.filter(organization=organization, meeting=meeting).first()

        context["meeting"] = meeting
        context["agenda_items"] = agenda_items
        context["preparation"] = preparation
        context["is_upcoming"] = meeting.start and meeting.start > timezone.now()

        return context

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from ._mixins import RISBodiesMixin


class RISMeetingsView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS meetings list."""

    template_name = "work/ris/meetings.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_meetings"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlMeeting, OParlOrganization

        # Base queryset
        meetings = OParlMeeting.objects.filter(body__in=bodies).prefetch_related("organizations")

        # Filter: upcoming/past
        view_mode = self.request.GET.get("view", "upcoming")
        now = timezone.now()

        if view_mode == "upcoming":
            meetings = meetings.filter(start__gt=now, cancelled=False)
            meetings = meetings.order_by("start")
        elif view_mode == "past":
            meetings = meetings.filter(start__lte=now)
            meetings = meetings.order_by("-start")
        else:
            meetings = meetings.order_by("-start")

        context["view_mode"] = view_mode

        # Filter by organization
        org_id = self.request.GET.get("org")
        if org_id:
            meetings = meetings.filter(organizations__id=org_id)
            context["selected_org"] = org_id

        # Get available organizations for filter
        context["organizations"] = OParlOrganization.objects.filter(body__in=bodies).order_by("name")

        # Filter by year
        year = self.request.GET.get("year")
        if year:
            try:
                meetings = meetings.filter(start__year=int(year))
                context["selected_year"] = int(year)
            except ValueError:
                pass

        context["years"] = OParlMeeting.objects.filter(body__in=bodies, start__isnull=False).dates(
            "start", "year", order="DESC"
        )

        # Pagination
        paginator = Paginator(meetings, 25)
        page = self.request.GET.get("page", 1)
        context["meetings"] = paginator.get_page(page)
        context["paginator"] = paginator

        return context


class RISMeetingDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS meeting detail view."""

    template_name = "work/ris/meeting_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_meetings"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        from insight_core.models import OParlMeeting

        meeting = get_object_or_404(OParlMeeting, id=kwargs.get("meeting_id"), body__in=bodies)

        context["meeting"] = meeting

        # Get agenda items with related papers
        # Natural sort: 1, 2, 10 instead of 1, 10, 2
        import re

        agenda_items = sorted(
            meeting.agenda_items.all(),
            key=lambda x: [
                (0, int(p)) if p.isdigit() else (1, p.lower()) for p in re.split(r"(\d+)", x.number or "999") if p
            ],
        )

        # Enrich with papers
        items_with_papers = []
        for item in agenda_items:
            papers = item.get_papers()
            items_with_papers.append(
                {
                    "item": item,
                    "papers": papers,
                }
            )

        context["agenda_items"] = items_with_papers
        context["organizations"] = meeting.organizations.all()

        return context

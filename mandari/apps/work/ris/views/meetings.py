# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors
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

        params = self.request.GET
        view_mode = params.get("view", "upcoming")
        context["view_mode"] = view_mode

        org_id = params.get("org")
        if org_id:
            context["selected_org"] = org_id

        year = None
        raw_year = params.get("year")
        if raw_year:
            try:
                year = int(raw_year)
                context["selected_year"] = year
            except ValueError:
                pass

        meetings = selectors.meetings_queryset(bodies, view_mode=view_mode, organization_id=org_id or "", year=year)
        context["organizations"] = selectors.organizations_for_filter(bodies)
        context["years"] = selectors.meeting_years(bodies)

        paginator = Paginator(meetings, 25)
        context["meetings"] = paginator.get_page(params.get("page", 1))
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

        meeting = get_object_or_404(selectors.meetings_in_bodies(bodies), id=kwargs.get("meeting_id"))
        context["meeting"] = meeting
        context["agenda_items"] = selectors.agenda_items_with_papers(meeting)
        context["organizations"] = meeting.organizations.all()
        return context

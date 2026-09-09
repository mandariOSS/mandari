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


class RISOrganizationsView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS organizations list."""

    template_name = "work/ris/organizations.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_organizations"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        tab = self.request.GET.get("tab", "active")
        q = self.request.GET.get("q", "").strip()
        if q:
            context["search_query"] = q
        context["tab"] = tab

        organizations = selectors.filter_organizations(
            selectors.organizations_with_meeting_info(bodies), search=q, active_only=tab == "active"
        )
        context.update(selectors.organization_tab_counts(bodies))

        paginator = Paginator(selectors.ranked_organizations(organizations), 25)
        context["organizations"] = paginator.get_page(self.request.GET.get("page", 1))
        context["paginator"] = paginator
        return context


class RISOrganizationDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS organization detail view."""

    template_name = "work/ris/organization_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_organizations"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        org = get_object_or_404(selectors.organizations_in_bodies(bodies), id=kwargs.get("org_id"))
        context["org"] = org

        members = selectors.organization_members(org)
        context["active_members"] = members["active"]
        context["past_members"] = members["past"]

        meetings = selectors.organization_meetings(org)
        context["upcoming_meetings"] = meetings["upcoming"]
        context["past_meetings"] = meetings["past"]

        context["active_tab"] = self.request.GET.get("tab", "members")
        return context

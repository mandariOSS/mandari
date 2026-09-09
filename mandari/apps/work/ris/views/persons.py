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


class RISPersonsView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS persons list."""

    template_name = "work/ris/persons.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_persons"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        search = self.request.GET.get("q", "").strip()
        if search:
            context["search_query"] = search

        paginator = Paginator(selectors.persons_queryset(bodies, search=search), 50)
        context["persons"] = paginator.get_page(self.request.GET.get("page", 1))
        context["paginator"] = paginator
        return context


class RISPersonDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS person detail view."""

    template_name = "work/ris/person_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_persons"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        person = get_object_or_404(selectors.persons_in_bodies(bodies), id=kwargs.get("person_id"))
        context["person"] = person

        memberships = selectors.person_memberships(person)
        context["active_memberships"] = memberships["active"]
        context["past_memberships"] = memberships["past"]

        context["active_tab"] = self.request.GET.get("tab", "memberships")
        return context

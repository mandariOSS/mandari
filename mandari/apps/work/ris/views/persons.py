# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.db.models import Count, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

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

        from insight_core.models import OParlMembership, OParlOrganization, OParlPerson

        # Base queryset
        persons = OParlPerson.objects.filter(body__in=bodies)

        # Council role annotation (like Insight portal)
        today = timezone.now().date()
        rat_orgs = OParlOrganization.objects.filter(body__in=bodies, name="Rat")
        if rat_orgs.exists():
            council_role_sq = Subquery(
                OParlMembership.objects.filter(
                    person=OuterRef("pk"),
                    organization__in=rat_orgs,
                )
                .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
                .values("role")[:1]
            )
            persons = persons.annotate(council_role=council_role_sq)

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            persons = persons.filter(
                Q(name__icontains=search)
                | Q(family_name__icontains=search)
                | Q(given_name__icontains=search)
                | Q(email__icontains=search)
            )
            context["search_query"] = search

        # Order
        persons = persons.annotate(membership_count=Count("memberships")).order_by("family_name", "given_name")

        # Pagination
        paginator = Paginator(persons, 50)
        page = self.request.GET.get("page", 1)
        context["persons"] = paginator.get_page(page)
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

        from insight_core.models import OParlPerson

        person = get_object_or_404(OParlPerson, id=kwargs.get("person_id"), body__in=bodies)

        context["person"] = person

        today = timezone.now().date()

        # Split memberships into active and past
        all_memberships = person.memberships.select_related("organization")
        context["active_memberships"] = all_memberships.filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today)
        ).order_by("organization__name")
        context["past_memberships"] = all_memberships.filter(end_date__lt=today).order_by("-end_date")

        # Active tab
        context["active_tab"] = self.request.GET.get("tab", "memberships")

        return context

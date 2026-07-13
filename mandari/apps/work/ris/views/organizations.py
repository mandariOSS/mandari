# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

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

        from insight_core.models import OParlMeeting, OParlOrganization
        from insight_core.ranking import sort_organizations_by_ranking

        now = timezone.now()
        today = now.date()
        tab = self.request.GET.get("tab", "active")
        q = self.request.GET.get("q", "").strip()

        # Meetings become "past" at midnight, not at meeting start time
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Subqueries for next/last meeting dates
        next_meeting_sq = Subquery(
            OParlMeeting.objects.filter(
                organizations=OuterRef("pk"),
                start__gte=today_start,
                cancelled=False,
            )
            .order_by("start")
            .values("start")[:1]
        )
        last_meeting_sq = Subquery(
            OParlMeeting.objects.filter(
                organizations=OuterRef("pk"),
                start__lt=today_start,
            )
            .order_by("-start")
            .values("start")[:1]
        )
        has_any_meeting = Exists(OParlMeeting.objects.filter(organizations=OuterRef("pk")))

        # Base queryset with meeting annotations
        organizations = OParlOrganization.objects.filter(body__in=bodies).annotate(
            next_meeting=next_meeting_sq,
            last_meeting=last_meeting_sq,
            has_meetings=has_any_meeting,
        )

        # Search
        if q:
            organizations = organizations.filter(Q(name__icontains=q) | Q(short_name__icontains=q))
            context["search_query"] = q

        # Tab filter: active vs all
        if tab == "active":
            organizations = organizations.filter(
                Q(end_date__isnull=True) | Q(end_date__gte=today),
                has_meetings=True,
            )
        context["tab"] = tab

        # Tab counts (without search filter)
        all_orgs = OParlOrganization.objects.filter(body__in=bodies).annotate(
            has_meetings=has_any_meeting,
        )
        context["active_count"] = all_orgs.filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today),
            has_meetings=True,
        ).count()
        context["all_count"] = all_orgs.count()

        # Apply ranking (with activity check)
        organizations = sort_organizations_by_ranking(organizations, include_activity=True)

        # Pagination
        paginator = Paginator(organizations, 25)
        page = self.request.GET.get("page", 1)
        context["organizations"] = paginator.get_page(page)
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

        from insight_core.models import OParlOrganization

        org = get_object_or_404(OParlOrganization, id=kwargs.get("org_id"), body__in=bodies)

        context["org"] = org

        today = timezone.now().date()
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

        # Active members (no end_date or end_date >= today)
        all_memberships = org.memberships.select_related("person")
        context["active_members"] = all_memberships.filter(Q(end_date__isnull=True) | Q(end_date__gte=today)).order_by(
            "person__family_name", "person__name"
        )
        # Past members (end_date < today)
        context["past_members"] = all_memberships.filter(end_date__lt=today).order_by("-end_date")

        # Upcoming meetings (today + future)
        context["upcoming_meetings"] = org.meetings.filter(start__gte=today_start, cancelled=False).order_by("start")
        # Past meetings
        context["past_meetings"] = org.meetings.filter(start__lt=today_start).order_by("-start")[:30]

        # Active tab from query parameter
        context["active_tab"] = self.request.GET.get("tab", "members")

        return context

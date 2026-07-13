# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.db.models import Q
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from ._mixins import RISBodiesMixin


class RISOverviewView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS overview page with statistics."""

    template_name = "work/ris/overview.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_overview"

        # Get the linked OParl bodies (multi-Kommune-fähig)
        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        # Import here to avoid circular imports
        from insight_core.models import (
            OParlMeeting,
            OParlOrganization,
            OParlPaper,
            OParlPerson,
        )

        # Statistics
        today = timezone.now().date()

        # Papers (Vorgänge)
        papers_total = OParlPaper.objects.filter(body__in=bodies).count()
        papers_this_year = OParlPaper.objects.filter(body__in=bodies, date__year=today.year).count()

        # Meetings (Sitzungen)
        meetings_total = OParlMeeting.objects.filter(body__in=bodies).count()
        meetings_upcoming = OParlMeeting.objects.filter(
            body__in=bodies, start__gt=timezone.now(), cancelled=False
        ).count()

        # Organizations (Gremien)
        organizations_total = OParlOrganization.objects.filter(body__in=bodies).count()
        organizations_active = (
            OParlOrganization.objects.filter(body__in=bodies)
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .count()
        )

        # Persons (Personen)
        persons_total = OParlPerson.objects.filter(body__in=bodies).count()

        context["stats"] = {
            "papers_total": papers_total,
            "papers_this_year": papers_this_year,
            "meetings_total": meetings_total,
            "meetings_upcoming": meetings_upcoming,
            "organizations_total": organizations_total,
            "organizations_active": organizations_active,
            "persons_total": persons_total,
        }

        # Recent papers
        context["recent_papers"] = OParlPaper.objects.filter(body__in=bodies).order_by("-date", "-oparl_created")[:5]

        # Upcoming meetings
        context["upcoming_meetings"] = (
            OParlMeeting.objects.filter(body__in=bodies, start__gt=timezone.now(), cancelled=False)
            .prefetch_related("organizations")
            .order_by("start")[:5]
        )

        return context

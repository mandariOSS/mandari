# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors
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

        context["stats"] = selectors.overview_stats(bodies)
        context["recent_papers"] = selectors.recent_papers(bodies)
        context["upcoming_meetings"] = selectors.upcoming_meetings(bodies)
        return context

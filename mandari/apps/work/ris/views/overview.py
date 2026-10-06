# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.stats_text import ris_overview_sentence

from .. import neu, selectors
from ._mixins import RISBodiesMixin


class RISOverviewView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS overview page with statistics."""

    template_name = "work/ris/overview.html"
    neue_vorlage = "work/ris/neu/uebersicht.html"
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
        # Zahlen im Satz statt als Zählerkacheln (Issue #851)
        context["stats_sentence"] = ris_overview_sentence(context["stats"])
        context["recent_papers"] = selectors.recent_papers(bodies)
        context["upcoming_meetings"] = selectors.upcoming_meetings(bodies)
        if self.neu:
            context |= neu.uebersicht(self.organization, self.membership, bodies, context)
        return context

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zusammenfassung der org-weiten Positionen und geteilten Redebeiträge einer Sitzung.
"""

from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors


class PreparationSummaryView(WorkViewMixin, TemplateView):
    """Summary view of org-level positions for a meeting."""

    template_name = "work/meetings/_summary.html"
    permission_required = "meetings.prepare"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        bodies = selectors.organization_bodies(self.organization)
        if bodies is None:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = selectors.get_meeting_or_404(bodies, self.kwargs["meeting_id"])
        context["meeting"] = meeting

        positions = selectors.positions_for_meeting(self.organization, meeting)
        positions_by_type, sections, has_positions = selectors.group_positions_by_type(positions)

        context["positions_by_type"] = positions_by_type
        context["position_sections"] = sections
        context["has_positions"] = has_positions
        context["speeches"] = selectors.shared_speeches_for_meeting(self.organization, meeting)
        context["preparation"] = selectors.get_preparation(self.organization, meeting)
        return context

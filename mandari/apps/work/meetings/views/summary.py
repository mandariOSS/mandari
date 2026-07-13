# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation views for the Work module.

Org-weite Sitzungsvorbereitung mit 5 Sektionen pro TOP:
1. Position/Beschluss (org-weit)
2. Private Notizen (pro User)
3. Redebeitrag (pro User, teilbar)
4. Fraktionsdiskussion (org-weit)
5. Dokumente (org-weit)
"""

from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from insight_core.models import OParlMeeting

from ..models import (
    AgendaItemPosition,
    AgendaSpeechNote,
    MeetingPreparation,
)

# =============================================================================
# SUMMARY
# =============================================================================


class PreparationSummaryView(WorkViewMixin, TemplateView):
    """Summary view of org-level positions for a meeting."""

    template_name = "work/meetings/_summary.html"
    permission_required = "meetings.prepare"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization

        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists():
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body__in=bodies)
        context["meeting"] = meeting

        preparation = MeetingPreparation.objects.filter(organization=organization, meeting=meeting).first()

        positions = AgendaItemPosition.objects.filter(
            organization=organization, agenda_item__meeting=meeting
        ).select_related("agenda_item", "set_by", "set_by__user")

        # Alle 8 Positionsarten (inkl. "open" für Vollständigkeit)
        positions_by_type = {code: [] for code, _label in AgendaItemPosition.POSITION_CHOICES}
        for pos in positions:
            if pos.position in positions_by_type:
                positions_by_type[pos.position].append(pos)

        position_sections = [
            {"code": code, "label": label, "positions": positions_by_type[code]}
            for code, label in AgendaItemPosition.POSITION_CHOICES
            if code != "open"
        ]
        has_positions = any(section["positions"] for section in position_sections)

        speeches = (
            AgendaSpeechNote.objects.filter(organization=organization, agenda_item__meeting=meeting, is_shared=True)
            .select_related("agenda_item", "author__user")
            .order_by("agenda_item__number")
        )

        context["positions_by_type"] = positions_by_type
        context["position_sections"] = position_sections
        context["has_positions"] = has_positions
        context["speeches"] = speeches
        context["preparation"] = preparation

        return context

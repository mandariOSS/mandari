# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Org-weite Sitzungsvorbereitung — Hauptansicht mit 5 Sektionen pro TOP:
1. Position/Beschluss (org-weit)
2. Private Notizen (pro User)
3. Redebeitrag (pro User, teilbar)
4. Fraktionsdiskussion (org-weit)
5. Dokumente (org-weit)
"""

import json

from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..models import AgendaItemNote, AgendaItemPosition
from ..serializers import build_prepare_config
from ._helpers import unauthorized


class MeetingPrepareView(WorkViewMixin, TemplateView):
    """Org-weite Sitzungsvorbereitung — Hauptansicht."""

    template_name = "work/meetings/prepare.html"
    permission_required = "meetings.prepare"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        bodies = selectors.organization_bodies(self.organization)
        if bodies is None:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = selectors.get_meeting_or_404(bodies, self.kwargs["meeting_id"], with_agenda=True)
        selectors.attach(meeting, committee_name=selectors.committee_name_for_meeting(meeting, {}))
        context["meeting"] = meeting

        if not self.membership:
            return context

        # Org-weite Preparation (eine pro Org+Meeting)
        preparation = services.ensure_preparation(self.organization, meeting, self.membership)
        data = selectors.load_preparation_data(self.organization, self.membership, meeting)

        context["preparation"] = preparation
        context["prepared_items"] = [entry.as_template_dict() for entry in data.prepared_items]
        context["position_choices"] = AgendaItemPosition.POSITION_CHOICES
        context["outcome_choices"] = AgendaItemPosition.OUTCOME_CHOICES
        context["visibility_choices"] = AgendaItemNote.VISIBILITY_CHOICES
        context["stats"] = data.stats

        # Daten für die Alpine-Komponente `preparationApp` (frontend/alpine/prepare-meeting.ts):
        # ein JSON-Objekt für den Client (json_script im Template), keine String-Interpolation in JS
        context["prepare_config"] = build_prepare_config(
            organization=self.organization,
            meeting=meeting,
            preparation=preparation,
            data=data,
            current_user_name=self.request.user.get_display_name(),
        )
        return context

    def post(self, request, *args, **kwargs):
        """
        Form-/JSON-Submissions verarbeiten.

        JSON (Auto-Save der UI): {"notes": "..."} speichert die org-weiten
        Sitzungsnotizen idempotent und liefert JSON zurück.

        Form-Actions (save_notes, mark_prepared, unmark_prepared) siehe
        ``services.apply_preparation_action``.
        """
        bodies = selectors.organization_bodies(self.organization)
        if bodies is None:
            return redirect("work:meetings", org_slug=self.organization.slug)

        meeting = selectors.get_meeting_or_404(bodies, self.kwargs["meeting_id"])

        # JSON-Auto-Save der org-weiten Sitzungsnotizen
        if request.content_type == "application/json":
            if not self.membership:
                return unauthorized()
            data = json.loads(request.body)
            if "notes" in data:
                services.save_meeting_notes(self.organization, meeting, self.membership, data.get("notes") or "")
            return JsonResponse({"success": True})

        if self.membership:
            services.apply_preparation_action(
                self.organization, meeting, self.membership, request.POST.get("action"), request.POST.get("notes", "")
            )

        return redirect("work:meeting_prepare", org_slug=self.organization.slug, meeting_id=self.kwargs["meeting_id"])

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teleprompter-Ansicht für den eigenen Redebeitrag zu einem TOP.
"""

from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors
from ..sanitize import sanitize_speech_html


class TeleprompterView(WorkViewMixin, TemplateView):
    """
    Teleprompter-Ansicht für den eigenen Redebeitrag zu einem TOP.

    Redebeiträge enthalten HTML (WYSIWYG). Vor dem Rendern wird der Inhalt
    über die strikte Whitelist (sanitize_speech_html) bereinigt. Bei einem
    verknüpften Dokument ("Dokument als Redebeitrag") wird dessen Inhalt
    read-only geliefert — mit can_access-Prüfung.
    """

    template_name = "work/meetings/teleprompter.html"
    permission_required = "meetings.prepare"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        bodies = selectors.organization_bodies(self.organization)
        if bodies is None or not self.membership:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = selectors.get_meeting_or_404(bodies, self.kwargs["meeting_id"])
        agenda_item = selectors.get_agenda_item_or_404(self.kwargs["item_id"], meeting)
        speech_note = selectors.get_own_speech(self.membership, agenda_item)

        context["meeting"] = meeting
        context["agenda_item"] = agenda_item
        context["speech_note"] = speech_note
        # HTML sicher rendern: strikte Whitelist (b/i/u/strong/em/ul/ol/li/p/br/h2/h3)
        context["speech_content"] = sanitize_speech_html(selectors.speech_content_for(speech_note, self.membership))
        return context

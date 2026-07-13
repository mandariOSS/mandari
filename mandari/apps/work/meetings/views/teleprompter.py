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
from insight_core.models import OParlAgendaItem, OParlMeeting

from ..models import (
    AgendaSpeechNote,
)
from ..sanitize import sanitize_speech_html

# =============================================================================
# TELEPROMPTER
# =============================================================================


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

        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists() or not membership:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body__in=bodies)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        speech_note = (
            AgendaSpeechNote.objects.filter(author=membership, agenda_item=agenda_item)
            .select_related("linked_document")
            .first()
        )

        speech_content = ""
        if speech_note:
            if speech_note.linked_document_id and speech_note.linked_document.can_access(membership):
                speech_content = speech_note.linked_document.get_content_decrypted()
            elif not speech_note.linked_document_id:
                speech_content = speech_note.get_content_decrypted()

        context["meeting"] = meeting
        context["agenda_item"] = agenda_item
        context["speech_note"] = speech_note
        # HTML sicher rendern: strikte Whitelist (b/i/u/strong/em/ul/ol/li/p/br/h2/h3)
        context["speech_content"] = sanitize_speech_html(speech_content)
        return context

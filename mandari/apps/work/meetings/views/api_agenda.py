# SPDX-License-Identifier: AGPL-3.0-or-later
"""
JSON-APIs der Sitzungsvorbereitung je TOP: Position, private Notiz, Redebeitrag,
Diskussions-Thread. Die Views parsen die Anfrage, prüfen Mitgliedschaft und
Org-Grenze und delegieren an ``selectors``/``services``.
"""

from django.http import Http404, JsonResponse
from django.views import View

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..serializers import (
    decrypted,
    serialize_agenda_note,
    serialize_paper_comment_as_note,
    serialize_position,
    serialize_shared_speech,
    serialize_speech_note,
)
from ..services import PreparationError
from ._helpers import error_response, request_payload, unauthorized


class AgendaPositionAPIView(WorkViewMixin, View):
    """
    API: Org-weite Position zu einem TOP (Position, Begründung, Ergebnis).

    POST unterstützt partielle, idempotente Saves (Debounce-Auto-Save):
    nur die übergebenen Felder position / is_final / reasoning / outcome
    werden geändert.

    GET liefert die eigene Position plus die Positionen derselben
    Organisation aus anderen Gremien/Sitzungen zur selben Vorlage
    ("Entscheidungen übergreifend").
    """

    permission_required = "meetings.prepare"

    def _get_item(self):
        bodies = selectors.organization_bodies(self.organization)
        if bodies is None:
            return None, None
        meeting = selectors.get_meeting_or_404(bodies, self.kwargs["meeting_id"])
        agenda_item = selectors.get_agenda_item_or_404(self.kwargs["item_id"], meeting)
        return meeting, agenda_item

    def get(self, request, *args, **kwargs):
        meeting, agenda_item = self._get_item()
        if agenda_item is None or not self.membership:
            return unauthorized()

        position = selectors.get_position(self.organization, agenda_item)
        cross = selectors.cross_positions(self.organization, [agenda_item])
        return JsonResponse(
            {"position": serialize_position(position), "cross_positions": cross.get(agenda_item.id, [])}
        )

    def post(self, request, *args, **kwargs):
        meeting, agenda_item = self._get_item()
        if agenda_item is None or not self.membership:
            return unauthorized()

        try:
            payload = services.save_position(
                self.organization, meeting, agenda_item, self.membership, request_payload(request)
            )
        except PreparationError as exc:
            return error_response(str(exc), exc.status)
        return JsonResponse({"success": True, **payload})


class PrivateNoteAPIView(WorkViewMixin, View):
    """API: Private Notiz pro User pro TOP (idempotenter Auto-Save)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        note = selectors.get_private_note(self.membership, self.kwargs["item_id"])
        return JsonResponse({"content": decrypted(note, "content") if note else ""})

    def post(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        agenda_item = selectors.get_agenda_item_or_404(self.kwargs["item_id"])
        content = request_payload(request).get("content", "")
        services.save_private_note(self.organization, agenda_item, self.membership, content)
        return JsonResponse({"success": True})


class SpeechNoteAPIView(WorkViewMixin, View):
    """
    API: Redebeitrag (pro User, mit Share-Toggle).

    POST unterstützt partielle, idempotente Saves: title / content /
    estimated_duration / is_shared / linked_document sind einzeln patchbar.
    content enthält HTML (WYSIWYG); beim Lesen/Schreiben wird nichts
    gestrippt — nur Ausgabe-Views (Teleprompter) sanitizen.

    "Dokument als Redebeitrag": linked_document verknüpft ein work.Motion-
    Dokument; die API liefert dessen Inhalt read-only als Redetext
    (can_access-Prüfung, sonst 403).
    """

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        agenda_item = selectors.get_agenda_item_or_404(self.kwargs["item_id"])
        own = selectors.get_own_speech(self.membership, agenda_item)
        shared = selectors.shared_speeches(self.organization, agenda_item, exclude_author=self.membership)
        return JsonResponse(
            {
                "own": serialize_speech_note(own, self.membership),
                "shared": [serialize_shared_speech(s) for s in shared],
            }
        )

    def post(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        agenda_item = selectors.get_agenda_item_or_404(self.kwargs["item_id"])
        try:
            note = services.save_speech_note(self.organization, agenda_item, self.membership, request_payload(request))
        except PreparationError as exc:
            return error_response(str(exc), exc.status)
        return JsonResponse(
            {"success": True, "is_shared": note.is_shared, "speech": serialize_speech_note(note, self.membership)}
        )

    def delete(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        services.delete_speech_note(self.membership, self.kwargs["item_id"])
        return JsonResponse({"success": True})


class SpeechLinkableDocumentsAPIView(WorkViewMixin, View):
    """API: Dokumente der Organisation, die als Redebeitrag verknüpfbar sind."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        docs = selectors.linkable_documents(self.membership, request.GET.get("q", "").strip())
        return JsonResponse(
            {"documents": [{"id": str(d.id), "title": d.title, "updated_at": d.updated_at.isoformat()} for d in docs]}
        )


class AgendaNotesAPIView(WorkViewMixin, View):
    """
    API: Einheitlicher Diskussions-Thread pro TOP.

    ARCHITEKTUR-ENTSCHEIDUNG:
    - TOP MIT Vorlage: Der Thread ist PaperComment (hängt am OParlPaper und
      ist damit automatisch im gesamten Beratungsverlauf sichtbar). POST
      legt hier einen PaperComment an, GET liefert die sichtbaren
      PaperComments plus (noch nicht migrierte) Alt-Notizen.
    - TOP OHNE Vorlage: AgendaItemNote (org-lokal) wie bisher.

    Nach jedem Speichern wird in die Channels-Gruppen gebroadcastet
    (Echtzeit); das 5-Sekunden-Polling der UI bleibt Fallback.
    """

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        membership = self.membership
        item_id = self.kwargs["item_id"]
        agenda_item = selectors.find_agenda_item(item_id)

        result = [serialize_agenda_note(n, membership) for n in selectors.thread_notes(self.organization, item_id)]
        if agenda_item:
            foreign = selectors.consulting_notes(self.organization, [agenda_item]).get(agenda_item.id, [])
            foreign = sorted(foreign, key=lambda n: n.created_at, reverse=True)
            result += [
                serialize_agenda_note(n, membership, origin_meeting=getattr(n, "origin_meeting", None)) for n in foreign
            ]

        # TOP mit Vorlage: PaperComments sind der eigentliche Thread
        paper = selectors.get_primary_paper_for_item(agenda_item)
        if paper:
            comments = selectors.visible_paper_comments(paper, membership)
            result = [serialize_paper_comment_as_note(c, membership) for c in comments] + result

        result.sort(key=lambda n: n["created_at"], reverse=True)
        return JsonResponse({"notes": result})

    def post(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        bodies = selectors.organization_bodies(self.organization)
        if bodies is None:
            return unauthorized()
        meeting = selectors.get_meeting_or_404(bodies, self.kwargs["meeting_id"])
        agenda_item = selectors.get_agenda_item_or_404(self.kwargs["item_id"], meeting)

        try:
            serialized = services.create_thread_note(
                self.organization, meeting, agenda_item, self.membership, request_payload(request)
            )
        except PreparationError as exc:
            return error_response(str(exc), exc.status)
        return JsonResponse({"success": True, "note": serialized})

    def delete(self, request, *args, **kwargs):
        if not self.membership:
            return unauthorized()
        # Einheitlicher Thread: ID kann Alt-Notiz ODER PaperComment sein
        if not services.delete_thread_note(self.membership, self.kwargs["note_id"]):
            raise Http404
        return JsonResponse({"success": True})

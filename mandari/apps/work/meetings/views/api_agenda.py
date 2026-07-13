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

import json

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from apps.common.mixins import WorkViewMixin
from insight_core.models import OParlAgendaItem, OParlMeeting

from .. import consumers
from ..models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    MeetingPreparation,
    PaperComment,
)
from ._helpers import get_primary_paper_for_item
from .serializers import serialize_agenda_note, serialize_paper_comment_as_note

# =============================================================================
# API ENDPOINTS
# =============================================================================


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
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization

        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists():
            return None, None

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body__in=bodies)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)
        return meeting, agenda_item

    def get(self, request, *args, **kwargs):
        organization = self.organization
        membership = self.membership

        meeting, agenda_item = self._get_item()
        if agenda_item is None or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        position = AgendaItemPosition.objects.filter(organization=organization, agenda_item=agenda_item).first()
        cross = AgendaItemPosition.get_cross_positions_for_items(organization, [agenda_item])

        return JsonResponse(
            {
                "position": {
                    "position": position.position if position else "open",
                    "position_display": position.get_position_display() if position else "Noch offen",
                    "is_final": position.is_final if position else False,
                    "reasoning": position.get_reasoning_decrypted() if position else "",
                    "outcome": position.outcome if position else "",
                    "outcome_display": position.get_outcome_display() if position and position.outcome else "",
                },
                "cross_positions": cross.get(agenda_item.id, []),
            }
        )

    def post(self, request, *args, **kwargs):
        organization = self.organization
        membership = self.membership

        meeting, agenda_item = self._get_item()
        if agenda_item is None or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        # Org-weite Position (eine pro Org+TOP)
        position, _ = AgendaItemPosition.objects.get_or_create(
            organization=organization,
            agenda_item=agenda_item,
        )

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        if "position" in data:
            if data["position"] not in dict(AgendaItemPosition.POSITION_CHOICES):
                return JsonResponse({"error": "Ungültige Position"}, status=400)
            position.position = data["position"]
        if "is_final" in data:
            position.is_final = data["is_final"] in [True, "true", "1", "on"]
        if "reasoning" in data:
            position.set_reasoning_encrypted(data.get("reasoning") or "")
        if "outcome" in data:
            if data["outcome"] not in dict(AgendaItemPosition.OUTCOME_CHOICES):
                return JsonResponse({"error": "Ungültiges Ergebnis"}, status=400)
            position.outcome = data["outcome"]
        position.set_by = membership
        position.save()

        # Abgeleiteter Vorbereitungsstatus: erster inhaltlicher Save
        if position.position != "open" or position.outcome or position.reasoning_encrypted:
            MeetingPreparation.record_activity(organization, meeting, membership)

        payload = {
            "success": True,
            "position": position.position,
            "position_display": position.get_position_display(),
            "is_final": position.is_final,
            "reasoning": position.get_reasoning_decrypted(),
            "outcome": position.outcome,
            "outcome_display": position.get_outcome_display() if position.outcome else "",
            "set_by": membership.user.get_display_name(),
        }

        # Echtzeit-Broadcast (Polling bleibt Fallback)
        paper = get_primary_paper_for_item(agenda_item)
        consumers.broadcast_preparation_event(
            organization.id,
            {
                "type": "position",
                "event": "updated",
                "agenda_item_id": str(agenda_item.id),
                "position": {k: v for k, v in payload.items() if k != "success"},
            },
            agenda_item_id=agenda_item.id,
            paper_id=paper.id if paper else None,
        )

        return JsonResponse(payload)


class PrivateNoteAPIView(WorkViewMixin, View):
    """API: Private Notiz pro User pro TOP (idempotenter Auto-Save)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        item_id = self.kwargs.get("item_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        note = AgendaPrivateNote.objects.filter(author=membership, agenda_item_id=item_id).first()
        return JsonResponse({"content": note.get_content_decrypted() if note else ""})

    def post(self, request, *args, **kwargs):
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id)
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST
        content = data.get("content", "")

        note, _ = AgendaPrivateNote.objects.get_or_create(
            author=membership,
            agenda_item=agenda_item,
            defaults={"organization": organization},
        )
        note.set_content_encrypted(content)
        note.save()

        if content.strip():
            MeetingPreparation.record_activity(organization, agenda_item.meeting, membership)

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

    @staticmethod
    def _serialize(note, membership):
        """Redebeitrag inkl. aufgelöstem linked_document-Inhalt serialisieren."""
        if note is None:
            return {
                "content": "",
                "title": "",
                "estimated_duration": 0,
                "is_shared": False,
                "linked_document": None,
                "content_readonly": False,
            }
        linked = None
        content = note.get_content_decrypted()
        readonly = False
        if note.linked_document_id:
            doc = note.linked_document
            if doc.can_access(membership):
                linked = {"id": str(doc.id), "title": doc.title}
                content = doc.get_content_decrypted()
                readonly = True
            else:
                # Verknüpfung existiert, aber kein Zugriff (z.B. entzogene Freigabe)
                linked = {"id": str(doc.id), "title": None, "access": False}
                content = ""
                readonly = True
        return {
            "content": content,
            "title": note.title,
            "estimated_duration": note.estimated_duration,
            "is_shared": note.is_shared,
            "linked_document": linked,
            "content_readonly": readonly,
        }

    def get(self, request, *args, **kwargs):
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id)

        # Eigener Redebeitrag
        own = (
            AgendaSpeechNote.objects.filter(author=membership, agenda_item=agenda_item)
            .select_related("linked_document")
            .first()
        )
        # Geteilte Redebeiträge anderer
        shared = (
            AgendaSpeechNote.objects.filter(
                organization=organization,
                agenda_item=agenda_item,
                is_shared=True,
            )
            .exclude(author=membership)
            .select_related("author__user")
        )

        return JsonResponse(
            {
                "own": self._serialize(own, membership),
                "shared": [
                    {"author": s.author.user.get_display_name(), "content": s.get_content_decrypted()} for s in shared
                ],
            }
        )

    def post(self, request, *args, **kwargs):
        from apps.work.motions.models import Motion

        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id)
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        # Verknüpftes Dokument VOR dem Anlegen prüfen (403 ohne Seiteneffekt)
        linked_document = None
        if "linked_document" in data and data.get("linked_document"):
            linked_document = Motion.objects.filter(id=data["linked_document"], organization=organization).first()
            if linked_document is None or not linked_document.can_access(membership):
                return JsonResponse({"error": "Kein Zugriff auf dieses Dokument"}, status=403)

        note, _ = AgendaSpeechNote.objects.get_or_create(
            author=membership,
            agenda_item=agenda_item,
            defaults={"organization": organization},
        )

        # Partielle Saves: nur übergebene Felder ändern
        if "content" in data:
            note.set_content_encrypted(data.get("content") or "")
        if "title" in data:
            note.title = data.get("title") or ""
        if "estimated_duration" in data:
            try:
                note.estimated_duration = max(0, int(data.get("estimated_duration") or 0))
            except (TypeError, ValueError):
                pass
        if "is_shared" in data:
            note.is_shared = data.get("is_shared") in [True, "true", "1", "on"]
        if "linked_document" in data:
            note.linked_document = linked_document
        note.save()

        MeetingPreparation.record_activity(organization, agenda_item.meeting, membership)

        return JsonResponse(
            {
                "success": True,
                "is_shared": note.is_shared,
                "speech": self._serialize(note, membership),
            }
        )

    def delete(self, request, *args, **kwargs):
        item_id = self.kwargs.get("item_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        AgendaSpeechNote.objects.filter(author=membership, agenda_item_id=item_id).delete()
        return JsonResponse({"success": True})


class SpeechLinkableDocumentsAPIView(WorkViewMixin, View):
    """API: Dokumente der Organisation, die als Redebeitrag verknüpfbar sind."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        from apps.work.motions.models import Motion

        membership = self.membership
        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        q = request.GET.get("q", "").strip()
        docs = Motion.visible_to(membership).order_by("-updated_at")
        if q:
            docs = docs.filter(title__icontains=q)

        return JsonResponse(
            {
                "documents": [
                    {"id": str(d.id), "title": d.title, "updated_at": d.updated_at.isoformat()} for d in docs[:50]
                ]
            }
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
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        agenda_item = OParlAgendaItem.objects.filter(id=item_id).first()

        # Alt-Notizen (migrierte ausschließen — deren Inhalt kommt als PaperComment)
        notes = list(
            AgendaItemNote.objects.filter(
                organization=organization,
                agenda_item_id=item_id,
                migrated_to_paper_comment__isnull=True,
            )
            .select_related("author", "author__user")
            .order_by("-is_pinned", "-is_decision", "-created_at")
        )

        # Consulting-Notizen aus Vorbereitungen anderer Gremien zur selben
        # Vorlage (gleiche Organisation)
        foreign_notes = []
        if agenda_item:
            consulting_by_item = AgendaItemNote.get_consulting_notes_for_items(organization, [agenda_item])
            foreign_notes = sorted(
                consulting_by_item.get(agenda_item.id, []),
                key=lambda n: n.created_at,
                reverse=True,
            )

        result = [serialize_agenda_note(n, membership) for n in notes] + [
            serialize_agenda_note(n, membership, origin_meeting=n.origin_meeting) for n in foreign_notes
        ]

        # TOP mit Vorlage: PaperComments sind der eigentliche Thread
        paper = get_primary_paper_for_item(agenda_item) if agenda_item else None
        if paper:
            visible_comments = PaperComment.get_visible_comments_for_paper(paper, membership)
            result = [serialize_paper_comment_as_note(c, membership) for c in visible_comments] + result

        result.sort(key=lambda n: n["created_at"], reverse=True)

        return JsonResponse({"notes": result})

    def post(self, request, *args, **kwargs):
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists():
            return JsonResponse({"error": "Unauthorized"}, status=403)
        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body__in=bodies)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST
        content = data.get("content", "").strip()
        if not content:
            return JsonResponse({"error": "Content required"}, status=400)

        is_decision = data.get("is_decision", False) in [True, "true", "1", "on"]

        visibility = data.get("visibility", "organization")
        if visibility not in dict(AgendaItemNote.VISIBILITY_CHOICES):
            visibility = "organization"

        paper = get_primary_paper_for_item(agenda_item)

        if paper:
            # TOP mit Vorlage: Thread läuft über PaperComment
            comment = PaperComment(
                paper=paper,
                organization=organization,
                author=membership,
                visibility=visibility,
                is_recommendation=is_decision,
            )
            comment.set_content_encrypted(content)
            comment.save()
            serialized = serialize_paper_comment_as_note(comment, membership)
        else:
            # TOP ohne Vorlage: org-lokale Notiz
            note = AgendaItemNote(
                organization=organization,
                agenda_item=agenda_item,
                author=membership,
                visibility=visibility,
                is_decision=is_decision,
            )
            note.set_content_encrypted(content)
            note.save()
            serialized = serialize_agenda_note(note, membership)

        MeetingPreparation.record_activity(organization, meeting, membership)

        # Echtzeit-Broadcast an (org, item) und ggf. (org, paper).
        # Private Kommentare NICHT broadcasten — sie sind nur für den Autor
        # sichtbar (is_visible_to), der Broadcast würde sie an die ganze
        # Organisation ausliefern. Der Autor erhält das Objekt via Response.
        if visibility != "private":
            consumers.broadcast_preparation_event(
                organization.id,
                {"type": "comment", "event": "created", "agenda_item_id": str(agenda_item.id), "comment": serialized},
                agenda_item_id=agenda_item.id,
                paper_id=paper.id if paper else None,
            )

        return JsonResponse({"success": True, "note": serialized})

    def delete(self, request, *args, **kwargs):
        note_id = self.kwargs.get("note_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        # Einheitlicher Thread: ID kann Alt-Notiz ODER PaperComment sein
        note = AgendaItemNote.objects.filter(id=note_id, author=membership).first()
        if note is not None:
            agenda_item_id = note.agenda_item_id
            organization_id = note.organization_id
            note.delete()
            consumers.broadcast_preparation_event(
                organization_id,
                {"type": "comment", "event": "deleted", "comment_id": str(note_id)},
                agenda_item_id=agenda_item_id,
            )
            return JsonResponse({"success": True})

        comment = get_object_or_404(PaperComment, id=note_id, author=membership)
        paper_id = comment.paper_id
        organization_id = comment.organization_id
        comment.delete()
        consumers.broadcast_preparation_event(
            organization_id,
            {"type": "comment", "event": "deleted", "comment_id": str(note_id)},
            paper_id=paper_id,
        )
        return JsonResponse({"success": True})

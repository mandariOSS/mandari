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
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from insight_core.models import OParlConsultation, OParlMeeting

from ..models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    FileAnnotation,
    MeetingPreparation,
)
from ._helpers import is_pdf_file, natural_sort_key, prefetch_papers_for_agenda_items
from .list import MeetingListView

# =============================================================================
# MEETING PREPARATION (Org-weit, 5 Sektionen)
# =============================================================================


class MeetingPrepareView(WorkViewMixin, TemplateView):
    """Org-weite Sitzungsvorbereitung — Hauptansicht."""

    template_name = "work/meetings/prepare.html"
    permission_required = "meetings.prepare"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization
        membership = self.membership

        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists():
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(
            OParlMeeting.objects.prefetch_related("organizations", "agenda_items"),
            id=meeting_id,
            body__in=bodies,
        )
        meeting.committee_name = MeetingListView._get_organization_name(meeting, {})
        context["meeting"] = meeting

        if not membership:
            return context

        # Org-weite Preparation (eine pro Org+Meeting)
        # Übergangsphase: Es können noch alte per-User-Einträge existieren
        preparation = MeetingPreparation.objects.filter(
            organization=organization,
            meeting=meeting,
        ).first()
        if not preparation:
            preparation = MeetingPreparation.objects.create(
                organization=organization,
                meeting=meeting,
                membership=membership,
            )

        agenda_items = sorted(meeting.agenda_items.all(), key=natural_sort_key)
        papers_by_item = prefetch_papers_for_agenda_items(agenda_items)

        # Org-weite Positionen
        positions_by_item = {}
        for pos in AgendaItemPosition.objects.filter(
            organization=organization,
            agenda_item__in=agenda_items,
        ).select_related("agenda_item", "set_by", "set_by__user"):
            positions_by_item[pos.agenda_item_id] = pos

        # Private Notizen des aktuellen Users
        private_notes_by_item = {}
        for note in AgendaPrivateNote.objects.filter(author=membership, agenda_item__in=agenda_items):
            private_notes_by_item[note.agenda_item_id] = note

        # Redebeiträge: eigene + geteilte von anderen
        own_speeches_by_item = {}
        shared_speeches_by_item = {}
        for sn in AgendaSpeechNote.objects.filter(
            organization=organization,
            agenda_item__in=agenda_items,
        ).select_related("author", "author__user", "linked_document"):
            if sn.author == membership:
                own_speeches_by_item[sn.agenda_item_id] = sn
            elif sn.is_shared:
                shared_speeches_by_item.setdefault(sn.agenda_item_id, []).append(sn)

        # Org-weite Diskussionsnotizen (nach PaperComment migrierte ausschließen,
        # sonst Doppelanzeige mit dem Vorlagen-Thread)
        notes_by_item = {}
        for note in AgendaItemNote.objects.filter(
            organization=organization,
            agenda_item__in=agenda_items,
            migrated_to_paper_comment__isnull=True,
        ).select_related("author", "author__user"):
            notes_by_item.setdefault(note.agenda_item_id, []).append(note)

        # Consulting-Notizen aus Vorbereitungen anderer Gremien (gleiche Vorlage,
        # gleiche Organisation)
        for item_id, consulting_notes in AgendaItemNote.get_consulting_notes_for_items(
            organization, agenda_items
        ).items():
            notes_by_item.setdefault(item_id, []).extend(consulting_notes)

        # Positionen derselben Org aus anderen Gremien zur selben Vorlage
        # ("Entscheidungen übergreifend")
        cross_positions_by_item = AgendaItemPosition.get_cross_positions_for_items(organization, agenda_items)

        # Ergänzende Dokumente: direkte TOP-Anhänge + über Gremien geteilte
        # Vorlagen-Anhänge der eigenen Organisation
        from django.db.models import Count, Q

        all_paper_ids = {p.id for papers in papers_by_item.values() for p in papers}
        docs_by_item = {}
        seen_doc_ids_by_item = {}
        docs_qs = (
            AgendaSupplementaryDocument.objects.filter(organization=organization)
            .filter(Q(agenda_item__in=agenda_items) | Q(paper_id__in=all_paper_ids, share_across_committees=True))
            .select_related("added_by__user", "oparl_file")
        )
        item_ids = {item.id for item in agenda_items}
        items_by_paper = {}
        for item in agenda_items:
            for p in papers_by_item.get(item.id, []):
                items_by_paper.setdefault(p.id, []).append(item.id)
        for doc in docs_qs:
            targets = set()
            if doc.agenda_item_id in item_ids:
                targets.add(doc.agenda_item_id)
            if doc.paper_id and doc.share_across_committees:
                targets.update(items_by_paper.get(doc.paper_id, []))
            for target_id in targets:
                seen = seen_doc_ids_by_item.setdefault(target_id, set())
                if doc.id not in seen:
                    seen.add(doc.id)
                    docs_by_item.setdefault(target_id, []).append(doc)

        # Anmerkungs-Zähler für RIS-Dateien (org-weit, wie Fraktionskommentare)
        all_file_ids = {f.id for papers in papers_by_item.values() for p in papers for f in p.files.all()}
        file_annotation_counts = {}
        if all_file_ids:
            file_annotation_counts = {
                row["oparl_file"]: row["c"]
                for row in FileAnnotation.objects.filter(organization=organization, oparl_file_id__in=all_file_ids)
                .values("oparl_file")
                .annotate(c=Count("id"))
            }

        # Build prepared items
        prepared_items = []
        for item in agenda_items:
            position = positions_by_item.get(item.id)
            papers = papers_by_item.get(item.id, [])
            primary_paper = papers[0] if papers else None
            has_files = any(p.files.exists() for p in papers) if papers else False
            private_note = private_notes_by_item.get(item.id)
            own_speech = own_speeches_by_item.get(item.id)
            shared_speeches = shared_speeches_by_item.get(item.id, [])

            prepared_items.append(
                {
                    "item": item,
                    "position": position,
                    "private_note": private_note,
                    "own_speech": own_speech,
                    "shared_speeches": shared_speeches,
                    "notes": notes_by_item.get(item.id, []),
                    "documents": docs_by_item.get(item.id, []),
                    "papers": papers,
                    "primary_paper": primary_paper,
                    "has_files": has_files,
                }
            )

        # Konkrete Beratungsfolge je Vorlage auflösen (welche Gremien, wann)
        consultations_by_paper = self._resolve_consultations(prepared_items, meeting)

        # Stats
        stats = {
            "total_items": len(agenda_items),
            "positioned": len([i for i in prepared_items if i["position"] and i["position"].position != "open"]),
            "want_to_speak": len([i for i in prepared_items if i["own_speech"]]),
            "with_notes": len([i for i in prepared_items if i["private_note"]]),
        }

        context["preparation"] = preparation
        context["prepared_items"] = prepared_items
        context["position_choices"] = AgendaItemPosition.POSITION_CHOICES
        context["outcome_choices"] = AgendaItemPosition.OUTCOME_CHOICES
        context["visibility_choices"] = AgendaItemNote.VISIBILITY_CHOICES
        context["stats"] = stats

        # JSON für Alpine.js
        context["prepared_items_json"] = json.dumps(
            [
                {
                    "id": str(item["item"].id),
                    "number": item["item"].number or str(idx + 1),
                    "name": item["item"].name or "Ohne Titel",
                    # Position (org-weit)
                    "position": item["position"].position if item["position"] else "open",
                    "isFinal": item["position"].is_final if item["position"] else False,
                    "reasoning": item["position"].get_reasoning_decrypted() if item["position"] else "",
                    "outcome": item["position"].outcome if item["position"] else "",
                    "setBy": item["position"].set_by.user.get_display_name()
                    if item["position"] and item["position"].set_by
                    else None,
                    # Positionen derselben Org aus anderen Gremien zur selben Vorlage
                    "crossPositions": cross_positions_by_item.get(item["item"].id, []),
                    # Private Notiz (pro User)
                    "privateNote": item["private_note"].get_content_decrypted() if item["private_note"] else "",
                    # Redebeitrag (pro User)
                    "hasSpeechNote": bool(item["own_speech"]),
                    "speechTitle": item["own_speech"].title if item["own_speech"] else "",
                    "speechContent": item["own_speech"].get_content_decrypted() if item["own_speech"] else "",
                    "speechDuration": item["own_speech"].estimated_duration if item["own_speech"] else 0,
                    "speechShared": item["own_speech"].is_shared if item["own_speech"] else False,
                    "speechLinkedDocument": {
                        "id": str(item["own_speech"].linked_document_id),
                        "title": item["own_speech"].linked_document.title,
                    }
                    if item["own_speech"] and item["own_speech"].linked_document_id
                    else None,
                    "sharedSpeeches": [
                        {"author": s.author.user.get_display_name(), "content": s.get_content_decrypted()}
                        for s in item["shared_speeches"]
                    ],
                    # Paper
                    "paper": {
                        "id": str(item["primary_paper"].id),
                        "name": item["primary_paper"].name or "Ohne Titel",
                        "reference": item["primary_paper"].reference or "",
                        "paperType": item["primary_paper"].paper_type or "",
                        "consultationCount": getattr(item["primary_paper"], "_prefetched_consultation_count", 0),
                        "consultations": consultations_by_paper.get(item["primary_paper"].id, []),
                    }
                    if item["primary_paper"]
                    else None,
                    "hasFiles": item["has_files"],
                    "files": [
                        {
                            "id": str(f.id),
                            "name": f.name or f.file_name or "Dokument",
                            "url": f.access_url or f.download_url,
                            "previewUrl": reverse("insight_core:insight:file_proxy", args=[f.id]),
                            "mimeType": f.mime_type or "",
                            "isPdf": is_pdf_file(f.mime_type, f.file_name, f.name),
                            "size": f.size_human,
                            "pageCount": f.page_count or 0,
                            "annotations": file_annotation_counts.get(f.id, 0),
                        }
                        for p in item["papers"]
                        for f in p.files.all()
                        if f.access_url or f.download_url
                    ],
                    # Dokumente (TOP-Anhänge + geteilte Vorlagen-Anhänge)
                    "documents": [
                        {
                            "id": str(d.id),
                            "title": d.title,
                            "url": d.display_url,
                            "type": d.document_type,
                            "addedBy": d.added_by.user.get_display_name(),
                            "paperId": str(d.paper_id) if d.paper_id else None,
                            "sharedAcrossCommittees": d.share_across_committees,
                        }
                        for d in item["documents"]
                    ],
                    # Alias für Altbestand im Template (documentLinks)
                    "documentLinks": [
                        {"id": str(d.id), "title": d.title, "url": d.display_url}
                        for d in item["documents"]
                        if d.document_type == "link"
                    ],
                    "notesCount": len(item["notes"]),
                }
                for idx, item in enumerate(prepared_items)
            ]
        )

        return context

    @staticmethod
    def _resolve_consultations(prepared_items, current_meeting):
        """Löst je Vorlage die konkrete Beratungsfolge auf: welches Gremium berät wann (mit Sitzungs-Link)."""
        paper_ids = {item["primary_paper"].id for item in prepared_items if item["primary_paper"]}
        if not paper_ids:
            return {}

        consults = list(OParlConsultation.objects.filter(paper_id__in=paper_ids))
        meeting_ext_ids = {c.meeting_external_id for c in consults if c.meeting_external_id}
        meetings_by_ext = {
            m.external_id: m
            for m in OParlMeeting.objects.filter(external_id__in=meeting_ext_ids).prefetch_related("organizations")
        }

        consultations_by_paper = {}
        for c in consults:
            m = meetings_by_ext.get(c.meeting_external_id) if c.meeting_external_id else None
            org_names = ", ".join(o.short_name or o.name or "" for o in m.organizations.all()) if m else ""
            consultations_by_paper.setdefault(c.paper_id, []).append(
                {
                    "role": c.role or "",
                    "authoritative": c.authoritative,
                    "meetingId": str(m.id) if m else None,
                    "meetingName": (m.name or "") if m else "",
                    "meetingStart": timezone.localtime(m.start).strftime("%d.%m.%Y") if m and m.start else "",
                    "organization": org_names,
                    "isCurrent": bool(m and m.id == current_meeting.id),
                    "_sort": m.start.isoformat() if m and m.start else "",
                }
            )

        for entries in consultations_by_paper.values():
            entries.sort(key=lambda e: e["_sort"])
            for e in entries:
                e.pop("_sort", None)

        return consultations_by_paper

    def post(self, request, *args, **kwargs):
        """
        Form-/JSON-Submissions verarbeiten.

        JSON (Auto-Save der UI): {"notes": "..."} speichert die org-weiten
        Sitzungsnotizen idempotent und liefert JSON zurück.

        Form-Actions:
        - save_notes: org-weite Notizen speichern
        - mark_prepared / unmark_prepared: DEPRECATED — is_prepared wird
          inzwischen aus der inhaltlichen Arbeit abgeleitet (record_activity).
          Die Actions bleiben funktionsfähig, bis die UI (Etappe 2) den
          Button entfernt.
        """
        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization
        membership = self.membership

        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists():
            return redirect("work:meetings", org_slug=organization.slug)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body__in=bodies)

        # JSON-Auto-Save der org-weiten Sitzungsnotizen
        if request.content_type == "application/json":
            if not membership:
                return JsonResponse({"error": "Unauthorized"}, status=403)
            data = json.loads(request.body)
            if "notes" in data:
                preparation = MeetingPreparation.record_activity(organization, meeting, membership)
                preparation.set_notes_encrypted(data.get("notes") or "")
                preparation.save(update_fields=["notes_encrypted", "updated_at"])
            return JsonResponse({"success": True})

        if membership:
            preparation = MeetingPreparation.objects.filter(
                organization=organization,
                meeting=meeting,
            ).first()

            if preparation:
                action = request.POST.get("action")
                if action == "mark_prepared":
                    # DEPRECATED: bleibt für Alt-UI funktionsfähig
                    preparation.is_prepared = True
                    preparation.prepared_at = timezone.now()
                    preparation.prepared_by = membership
                    preparation.save()
                elif action == "unmark_prepared":
                    # DEPRECATED: bleibt für Alt-UI funktionsfähig
                    preparation.is_prepared = False
                    preparation.prepared_at = None
                    preparation.prepared_by = None
                    preparation.save()
                elif action == "save_notes":
                    notes = request.POST.get("notes", "")
                    preparation.set_notes_encrypted(notes)
                    preparation.save()
                    if notes.strip():
                        MeetingPreparation.record_activity(organization, meeting, membership)

        return redirect("work:meeting_prepare", org_slug=organization.slug, meeting_id=meeting_id)

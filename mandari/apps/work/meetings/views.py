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
import re
from datetime import datetime, timedelta

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from insight_core.models import OParlAgendaItem, OParlConsultation, OParlMeeting, OParlOrganization

from .models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    MeetingPreparation,
)


def natural_sort_key(item):
    """Sort agenda items naturally: 1, 2, 10, 11 instead of 1, 10, 11, 2."""
    number = item.number or "999"
    parts = re.split(r"(\d+)", str(number))
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in parts if p]


def prefetch_papers_for_agenda_items(agenda_items):
    """
    Pre-fetch papers for a list of agenda items via consultations.
    Returns a dict mapping agenda_item.id to list of papers with their files.
    """
    from django.db.models import Count

    if not agenda_items:
        return {}

    external_ids = [item.external_id for item in agenda_items if item.external_id]
    if not external_ids:
        return {}

    consultations = (
        OParlConsultation.objects.filter(agenda_item_external_id__in=external_ids)
        .select_related("paper")
        .prefetch_related("paper__files", "paper__consultations")
    )

    paper_ids = set()
    for consultation in consultations:
        if consultation.paper:
            paper_ids.add(consultation.paper.id)

    paper_consultation_counts = {}
    if paper_ids:
        from insight_core.models import OParlPaper

        papers_with_counts = OParlPaper.objects.filter(id__in=paper_ids).annotate(
            consultation_count=Count("consultations")
        )
        paper_consultation_counts = {p.id: p.consultation_count for p in papers_with_counts}

    papers_by_ext_id = {}
    for consultation in consultations:
        if consultation.paper and consultation.agenda_item_external_id:
            ext_id = consultation.agenda_item_external_id
            if ext_id not in papers_by_ext_id:
                papers_by_ext_id[ext_id] = []
            if consultation.paper not in papers_by_ext_id[ext_id]:
                consultation.paper._prefetched_consultation_count = paper_consultation_counts.get(
                    consultation.paper.id, 0
                )
                papers_by_ext_id[ext_id].append(consultation.paper)

    papers_by_item_id = {}
    for item in agenda_items:
        if item.external_id and item.external_id in papers_by_ext_id:
            papers_by_item_id[item.id] = papers_by_ext_id[item.external_id]
        else:
            papers_by_item_id[item.id] = []

    return papers_by_item_id


# =============================================================================
# MEETING LIST + CALENDAR
# =============================================================================


class MeetingListView(WorkViewMixin, TemplateView):
    """List of OParl meetings for preparation."""

    template_name = "work/meetings/list.html"
    permission_required = "meetings.view"

    @staticmethod
    def _get_organization_name(meeting, org_cache):
        """Extract organization name from meeting."""
        try:
            orgs = meeting.organizations.all()
            if orgs:
                return orgs[0].name
        except Exception:
            pass

        try:
            raw = meeting.raw_json or {}
            orgs = raw.get("organization", [])
            if isinstance(orgs, list) and orgs:
                org_url = orgs[0]
                if org_url in org_cache:
                    return org_cache[org_url]
                org_obj = OParlOrganization.objects.filter(external_id=org_url).first()
                if org_obj:
                    org_cache[org_url] = org_obj.name
                    return org_obj.name
        except Exception:
            pass

        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        organization = self.organization
        membership = self.membership
        body = organization.body if organization else None

        if not body:
            context["has_body"] = False
            context["meetings"] = []
            return context

        context["has_body"] = True
        now = timezone.now()

        # Filters
        time_filter = self.request.GET.get("time", "upcoming")
        committee_filter = self.request.GET.get("committee", "")
        search_query = self.request.GET.get("q", "").strip()
        view_mode = self.request.GET.get("view", "my")

        # Get assigned committees
        assigned_committees = []
        if membership:
            assigned_committees = list(membership.oparl_committees.filter(body=body))

        assigned_committee_ids = [c.id for c in assigned_committees]

        # Build queryset
        meetings_qs = OParlMeeting.objects.filter(body=body).prefetch_related("organizations")

        if time_filter == "upcoming":
            meetings_qs = meetings_qs.filter(start__gte=now - timedelta(hours=2)).order_by("start")
        elif time_filter == "past":
            meetings_qs = meetings_qs.filter(start__lt=now).order_by("-start")
        else:
            meetings_qs = meetings_qs.order_by("-start")

        # Limit to 180 days
        if time_filter in ("upcoming", "past"):
            cutoff = now + timedelta(days=180) if time_filter == "upcoming" else now - timedelta(days=180)
            if time_filter == "upcoming":
                meetings_qs = meetings_qs.filter(start__lte=cutoff)
            else:
                meetings_qs = meetings_qs.filter(start__gte=cutoff)

        meetings = list(meetings_qs[:100])

        # Filter by view mode (my committees only)
        if view_mode == "my" and assigned_committee_ids:
            meetings = [m for m in meetings if any(org.id in assigned_committee_ids for org in m.organizations.all())]

        # Committee filter
        if committee_filter:
            meetings = [m for m in meetings if any(str(org.id) == committee_filter for org in m.organizations.all())]

        # Search
        if search_query:
            q = search_query.lower()
            meetings = [m for m in meetings if q in (m.name or "").lower()]

        # Add organization name
        org_cache = {}
        for meeting in meetings:
            meeting.committee_name = self._get_organization_name(meeting, org_cache)

        # Check which meetings are prepared (org-level now)
        prepared_meeting_ids = set(
            MeetingPreparation.objects.filter(organization=organization, is_prepared=True).values_list(
                "meeting_id", flat=True
            )
        )
        for meeting in meetings:
            meeting.is_user_prepared = meeting.id in prepared_meeting_ids

        # All committees for filter dropdown
        all_committees = list(
            OParlOrganization.objects.filter(body=body, organization_type__icontains="committee")
            .order_by("name")
            .values("id", "name")
        )

        has_assignments = bool(assigned_committees)

        context.update(
            {
                "meetings": meetings,
                "assigned_committees": assigned_committees,
                "all_committees": all_committees,
                "time_filter": time_filter,
                "committee_filter": committee_filter,
                "search_query": search_query,
                "view_mode": view_mode,
                "has_assignments": has_assignments,
                "now": now,
            }
        )
        return context


class MeetingCalendarView(WorkViewMixin, TemplateView):
    """Calendar view for meetings."""

    template_name = "work/meetings/calendar.html"
    permission_required = "meetings.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"
        return context


class MeetingCalendarEventsView(WorkViewMixin, View):
    """JSON endpoint for calendar events."""

    permission_required = "meetings.view"

    def get(self, request, *args, **kwargs):
        organization = self.organization
        body = organization.body if organization else None
        if not body:
            return JsonResponse([], safe=False)

        start_str = request.GET.get("start", "")
        end_str = request.GET.get("end", "")

        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return JsonResponse([], safe=False)

        meetings = OParlMeeting.objects.filter(body=body, start__gte=start, start__lte=end).prefetch_related(
            "organizations"
        )

        events = []
        for meeting in meetings:
            org_name = ""
            try:
                orgs = meeting.organizations.all()
                if orgs:
                    org_name = orgs[0].name
            except Exception:
                pass

            events.append(
                {
                    "id": str(meeting.id),
                    "title": meeting.name or org_name or "Sitzung",
                    "start": meeting.start.isoformat() if meeting.start else None,
                    "end": meeting.end.isoformat() if meeting.end else None,
                    "url": f"/work/{organization.slug}/meetings/{meeting.id}/",
                    "extendedProps": {"committee": org_name, "cancelled": meeting.cancelled},
                    "color": "#ef4444" if meeting.cancelled else None,
                }
            )

        return JsonResponse(events, safe=False)


# =============================================================================
# MEETING DETAIL
# =============================================================================


class MeetingDetailView(WorkViewMixin, TemplateView):
    """Meeting detail view with agenda items."""

    template_name = "work/meetings/detail.html"
    permission_required = "meetings.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization
        body = organization.body if organization else None

        if not body:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(
            OParlMeeting.objects.prefetch_related("organizations", "agenda_items"),
            id=meeting_id,
            body=body,
        )

        meeting.committee_name = MeetingListView._get_organization_name(meeting, {})
        agenda_items = sorted(meeting.agenda_items.all(), key=natural_sort_key)

        # Org-level preparation
        preparation = MeetingPreparation.objects.filter(organization=organization, meeting=meeting).first()

        context["meeting"] = meeting
        context["agenda_items"] = agenda_items
        context["preparation"] = preparation
        context["is_upcoming"] = meeting.start and meeting.start > timezone.now()

        return context


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

        body = organization.body if organization else None
        if not body:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(
            OParlMeeting.objects.prefetch_related("organizations", "agenda_items"),
            id=meeting_id,
            body=body,
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
        ).select_related("author", "author__user"):
            if sn.author == membership:
                own_speeches_by_item[sn.agenda_item_id] = sn
            elif sn.is_shared:
                shared_speeches_by_item.setdefault(sn.agenda_item_id, []).append(sn)

        # Org-weite Diskussionsnotizen
        notes_by_item = {}
        for note in AgendaItemNote.objects.filter(
            organization=organization,
            agenda_item__in=agenda_items,
        ).select_related("author", "author__user"):
            notes_by_item.setdefault(note.agenda_item_id, []).append(note)

        # Ergänzende Dokumente
        docs_by_item = {}
        for doc in AgendaSupplementaryDocument.objects.filter(
            organization=organization,
            agenda_item__in=agenda_items,
        ).select_related("added_by__user", "oparl_file"):
            docs_by_item.setdefault(doc.agenda_item_id, []).append(doc)

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
                    "setBy": item["position"].set_by.user.get_display_name()
                    if item["position"] and item["position"].set_by
                    else None,
                    # Private Notiz (pro User)
                    "privateNote": item["private_note"].get_content_decrypted() if item["private_note"] else "",
                    # Redebeitrag (pro User)
                    "speechContent": item["own_speech"].get_content_decrypted() if item["own_speech"] else "",
                    "speechShared": item["own_speech"].is_shared if item["own_speech"] else False,
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
                    }
                    if item["primary_paper"]
                    else None,
                    "hasFiles": item["has_files"],
                    "files": [
                        {"name": f.name or f.file_name or "Dokument", "url": f.access_url or f.download_url}
                        for p in item["papers"]
                        for f in p.files.all()
                        if f.access_url or f.download_url
                    ],
                    # Dokumente
                    "documents": [
                        {
                            "id": str(d.id),
                            "title": d.title,
                            "url": d.display_url,
                            "type": d.document_type,
                            "addedBy": d.added_by.user.get_display_name(),
                        }
                        for d in item["documents"]
                    ],
                    "notesCount": len(item["notes"]),
                }
                for idx, item in enumerate(prepared_items)
            ]
        )

        return context

    def post(self, request, *args, **kwargs):
        """Handle form submissions (mark as prepared, save general notes)."""
        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body:
            return redirect("work:meetings", org_slug=organization.slug)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)

        if membership:
            preparation = MeetingPreparation.objects.filter(
                organization=organization,
                meeting=meeting,
            ).first()

            if preparation:
                action = request.POST.get("action")
                if action == "mark_prepared":
                    preparation.is_prepared = True
                    preparation.prepared_at = timezone.now()
                    preparation.prepared_by = membership
                    preparation.save()
                elif action == "unmark_prepared":
                    preparation.is_prepared = False
                    preparation.prepared_at = None
                    preparation.prepared_by = None
                    preparation.save()
                elif action == "save_notes":
                    notes = request.POST.get("notes", "")
                    preparation.set_notes_encrypted(notes)
                    preparation.save()

        return redirect("work:meeting_prepare", org_slug=organization.slug, meeting_id=meeting_id)


# =============================================================================
# API ENDPOINTS
# =============================================================================


class AgendaPositionAPIView(WorkViewMixin, View):
    """API: Org-weite Position zu einem TOP setzen."""

    permission_required = "meetings.prepare"

    def post(self, request, *args, **kwargs):
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        # Org-weite Position (eine pro Org+TOP)
        position, _ = AgendaItemPosition.objects.get_or_create(
            organization=organization,
            agenda_item=agenda_item,
        )

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        if "position" in data:
            position.position = data["position"]
        if "is_final" in data:
            position.is_final = data["is_final"] in [True, "true", "1", "on"]
        position.set_by = membership
        position.save()

        return JsonResponse(
            {
                "success": True,
                "position": position.position,
                "position_display": position.get_position_display(),
                "is_final": position.is_final,
                "set_by": membership.user.get_display_name(),
            }
        )


class PrivateNoteAPIView(WorkViewMixin, View):
    """API: Private Notiz pro User pro TOP."""

    permission_required = "meetings.prepare"

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

        return JsonResponse({"success": True})


class SpeechNoteAPIView(WorkViewMixin, View):
    """API: Redebeitrag (pro User, mit Share-Toggle)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id)

        # Eigener Redebeitrag
        own = AgendaSpeechNote.objects.filter(author=membership, agenda_item=agenda_item).first()
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
                "own": {
                    "content": own.get_content_decrypted() if own else "",
                    "is_shared": own.is_shared if own else False,
                },
                "shared": [
                    {"author": s.author.user.get_display_name(), "content": s.get_content_decrypted()} for s in shared
                ],
            }
        )

    def post(self, request, *args, **kwargs):
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id)
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        note, _ = AgendaSpeechNote.objects.get_or_create(
            author=membership,
            agenda_item=agenda_item,
            defaults={"organization": organization},
        )
        note.set_content_encrypted(data.get("content", ""))
        note.is_shared = data.get("is_shared", False) in [True, "true", "1", "on"]
        note.save()

        return JsonResponse({"success": True, "is_shared": note.is_shared})


class AgendaNotesAPIView(WorkViewMixin, View):
    """API: Org-weite Fraktionsdiskussion pro TOP."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        notes = (
            AgendaItemNote.objects.filter(organization=organization, agenda_item_id=item_id)
            .select_related("author", "author__user")
            .order_by("-is_pinned", "-is_decision", "-created_at")
        )

        return JsonResponse(
            {
                "notes": [
                    {
                        "id": str(n.id),
                        "content": n.get_content_decrypted(),
                        "is_decision": n.is_decision,
                        "is_pinned": n.is_pinned,
                        "author": n.author.user.get_display_name(),
                        "is_own": n.author == membership,
                        "created_at": n.created_at.isoformat(),
                    }
                    for n in notes
                ]
            }
        )

    def post(self, request, *args, **kwargs):
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        body = organization.body if organization else None
        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST
        content = data.get("content", "").strip()
        if not content:
            return JsonResponse({"error": "Content required"}, status=400)

        is_decision = data.get("is_decision", False) in [True, "true", "1", "on"]

        note = AgendaItemNote(
            organization=organization,
            agenda_item=agenda_item,
            author=membership,
            visibility="organization",
            is_decision=is_decision,
        )
        note.set_content_encrypted(content)
        note.save()

        return JsonResponse(
            {
                "success": True,
                "note": {
                    "id": str(note.id),
                    "content": content,
                    "is_decision": note.is_decision,
                    "author": membership.user.get_display_name(),
                },
            }
        )

    def delete(self, request, *args, **kwargs):
        note_id = self.kwargs.get("note_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        note = get_object_or_404(AgendaItemNote, id=note_id, author=membership)
        note.delete()
        return JsonResponse({"success": True})


class SupplementaryDocumentAPIView(WorkViewMixin, View):
    """API: Ergänzende Dokumente (Links + Uploads + OParl-Referenzen)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        item_id = self.kwargs.get("item_id")
        organization = self.organization

        docs = AgendaSupplementaryDocument.objects.filter(
            organization=organization,
            agenda_item_id=item_id,
        ).select_related("added_by__user", "oparl_file")

        return JsonResponse(
            {
                "documents": [
                    {
                        "id": str(d.id),
                        "title": d.title,
                        "url": d.display_url,
                        "document_type": d.document_type,
                        "description": d.description,
                        "added_by": d.added_by.user.get_display_name(),
                        "created_at": d.created_at.isoformat(),
                    }
                    for d in docs
                ]
            }
        )

    def post(self, request, *args, **kwargs):
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        # Unterstützt sowohl JSON (Links) als auch Multipart (Uploads)
        if request.content_type and "multipart" in request.content_type:
            return self._handle_file_upload(request, organization, membership, agenda_item)

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST
        doc_type = data.get("document_type", "link")
        title = data.get("title", "").strip()

        if not title:
            return JsonResponse({"error": "Titel erforderlich"}, status=400)

        doc = AgendaSupplementaryDocument.objects.create(
            organization=organization,
            added_by=membership,
            agenda_item=agenda_item,
            document_type=doc_type,
            title=title,
            url=data.get("url", ""),
            description=data.get("description", ""),
        )

        return JsonResponse(
            {
                "success": True,
                "document": {
                    "id": str(doc.id),
                    "title": doc.title,
                    "url": doc.display_url,
                    "document_type": doc.document_type,
                },
            }
        )

    def _handle_file_upload(self, request, organization, membership, agenda_item):
        """Datei-Upload verarbeiten."""
        uploaded_file = request.FILES.get("file")
        title = request.POST.get("title", "").strip()

        if not uploaded_file:
            return JsonResponse({"error": "Keine Datei"}, status=400)

        if not title:
            title = uploaded_file.name

        # Max 50 MB
        if uploaded_file.size > 50 * 1024 * 1024:
            return JsonResponse({"error": "Datei zu groß (max. 50 MB)"}, status=400)

        doc = AgendaSupplementaryDocument.objects.create(
            organization=organization,
            added_by=membership,
            agenda_item=agenda_item,
            document_type="file",
            title=title,
            file=uploaded_file,
            filename=uploaded_file.name,
            mime_type=uploaded_file.content_type or "",
            file_size=uploaded_file.size,
            description=request.POST.get("description", ""),
        )

        return JsonResponse(
            {
                "success": True,
                "document": {"id": str(doc.id), "title": doc.title, "url": doc.display_url, "document_type": "file"},
            }
        )

    def delete(self, request, *args, **kwargs):
        doc_id = self.kwargs.get("doc_id") or self.kwargs.get("link_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        doc = get_object_or_404(AgendaSupplementaryDocument, id=doc_id, added_by=membership)
        if doc.file:
            doc.file.delete(save=False)
        doc.delete()
        return JsonResponse({"success": True})


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

        body = organization.body if organization else None
        if not body:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        context["meeting"] = meeting

        preparation = MeetingPreparation.objects.filter(organization=organization, meeting=meeting).first()

        positions = (
            AgendaItemPosition.objects.filter(organization=organization, agenda_item__meeting=meeting)
            .exclude(position="open")
            .select_related("agenda_item", "set_by", "set_by__user")
        )

        positions_by_type = {
            "for": [],
            "against": [],
            "abstain": [],
            "defer": [],
            "refer": [],
            "amended": [],
            "info": [],
        }
        for pos in positions:
            if pos.position in positions_by_type:
                positions_by_type[pos.position].append(pos)

        speeches = (
            AgendaSpeechNote.objects.filter(organization=organization, agenda_item__meeting=meeting, is_shared=True)
            .select_related("agenda_item", "author__user")
            .order_by("agenda_item__number")
        )

        context["positions_by_type"] = positions_by_type
        context["speeches"] = speeches
        context["preparation"] = preparation

        return context


# =============================================================================
# PAPER COMMENTS (unverändert, gremienübergreifend)
# =============================================================================


class PaperCommentAPIView(WorkViewMixin, View):
    """API endpoint for comments on OParl Papers (cross-committee collaboration)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        from insight_core.models import OParlPaper

        from .models import PaperComment

        paper_id = self.kwargs.get("paper_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        paper = get_object_or_404(OParlPaper, id=paper_id)
        visible_comments = PaperComment.get_visible_comments_for_paper(paper, membership)

        return JsonResponse(
            {
                "comments": [
                    {
                        "id": str(c.id),
                        "content": c.get_content_decrypted(),
                        "visibility": c.visibility,
                        "visibility_display": c.get_visibility_display(),
                        "is_recommendation": c.is_recommendation,
                        "author": c.author.user.get_display_name(),
                        "organization": c.organization.name,
                        "is_own": c.author == membership,
                        "is_own_org": c.organization == membership.organization,
                        "created_at": c.created_at.isoformat(),
                    }
                    for c in visible_comments
                ]
            }
        )

    def post(self, request, *args, **kwargs):
        from insight_core.models import OParlPaper

        from .models import PaperComment

        paper_id = self.kwargs.get("paper_id")
        organization = self.organization
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        paper = get_object_or_404(OParlPaper, id=paper_id)
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        content = data.get("content", "").strip()
        if not content:
            return JsonResponse({"error": "Content required"}, status=400)

        visibility = data.get("visibility", "organization")
        is_recommendation = data.get("is_recommendation", False) in [True, "true", "1", "on"]

        comment = PaperComment(
            paper=paper,
            organization=organization,
            author=membership,
            visibility=visibility,
            is_recommendation=is_recommendation,
        )
        comment.set_content_encrypted(content)
        comment.save()

        return JsonResponse(
            {
                "success": True,
                "comment": {
                    "id": str(comment.id),
                    "content": content,
                    "visibility_display": comment.get_visibility_display(),
                    "is_recommendation": comment.is_recommendation,
                    "author": membership.user.get_display_name(),
                    "organization": organization.name,
                },
            }
        )

    def delete(self, request, *args, **kwargs):
        from .models import PaperComment

        comment_id = self.kwargs.get("comment_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        comment = get_object_or_404(PaperComment, id=comment_id, author=membership)
        comment.delete()
        return JsonResponse({"success": True})

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

from .consumers import broadcast_preparation_event
from .models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    MeetingPreparation,
    PaperComment,
)
from .sanitize import sanitize_speech_html


def get_primary_paper_for_item(agenda_item):
    """Erste Vorlage eines TOPs (über OParlConsultation) oder None."""
    if not agenda_item.external_id:
        return None
    consultation = (
        OParlConsultation.objects.filter(agenda_item_external_id=agenda_item.external_id, paper__isnull=False)
        .select_related("paper")
        .first()
    )
    return consultation.paper if consultation else None


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
        bodies = organization.get_all_bodies() if organization else None

        if bodies is None or not bodies.exists():
            context["has_body"] = False
            context["meetings"] = []
            return context

        context["has_body"] = True
        context["bodies"] = bodies
        context["has_multiple_bodies"] = bodies.count() > 1
        now = timezone.now()

        # Filters
        time_filter = self.request.GET.get("time", "upcoming")
        committee_filter = self.request.GET.get("committee", "")
        search_query = self.request.GET.get("q", "").strip()
        view_mode = self.request.GET.get("view", "my")

        # Get assigned committees
        assigned_committees = []
        if membership:
            assigned_committees = list(membership.oparl_committees.filter(body__in=bodies))

        assigned_committee_ids = [c.id for c in assigned_committees]

        # Build queryset
        meetings_qs = OParlMeeting.objects.filter(body__in=bodies).prefetch_related("organizations")

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
            OParlOrganization.objects.filter(body__in=bodies, organization_type__icontains="committee")
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
        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists():
            return JsonResponse([], safe=False)

        start_str = request.GET.get("start", "")
        end_str = request.GET.get("end", "")

        try:
            start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return JsonResponse([], safe=False)

        meetings = OParlMeeting.objects.filter(body__in=bodies, start__gte=start, start__lte=end).prefetch_related(
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
        from django.db.models import Q

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
                        {"name": f.name or f.file_name or "Dokument", "url": f.access_url or f.download_url}
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
        broadcast_preparation_event(
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


def serialize_paper_comment_as_note(comment, membership):
    """
    PaperComment im Format des TOP-Diskussions-Threads serialisieren.

    ARCHITEKTUR: PaperComment ist DER Thread für TOPs mit Vorlage.
    is_recommendation und is_decision heißen im UI (Etappe 2) einheitlich
    "Position der Fraktion" — beide Flags bleiben im Backend erhalten.
    """
    return {
        "id": str(comment.id),
        "content": comment.get_content_decrypted(),
        "is_decision": comment.is_recommendation,
        "is_recommendation": comment.is_recommendation,
        "is_pinned": False,
        "author": comment.author.user.get_display_name(),
        "organization": comment.organization.name,
        "is_own": comment.author == membership,
        "is_own_org": comment.organization_id == membership.organization_id,
        "created_at": comment.created_at.isoformat(),
        "visibility": comment.visibility,
        "visibility_display": comment.get_visibility_display(),
        "origin": None,
        "source": "paper_comment",
    }


def serialize_agenda_note(note, membership, origin_meeting=None):
    """AgendaItemNote für den Diskussions-Thread serialisieren."""
    origin = None
    if origin_meeting is not None:
        label = origin_meeting.get_display_name()
        if origin_meeting.start:
            label = f"{label}, {timezone.localtime(origin_meeting.start).strftime('%d.%m.%Y')}"
        origin = {"meeting_id": str(origin_meeting.id), "label": label}
    return {
        "id": str(note.id),
        "content": note.get_content_decrypted(),
        "is_decision": note.is_decision,
        "is_recommendation": note.is_decision,
        "is_pinned": note.is_pinned,
        "author": note.author.user.get_display_name(),
        "is_own": note.author == membership,
        "created_at": note.created_at.isoformat(),
        "visibility": note.visibility,
        "visibility_display": note.get_visibility_display(),
        "origin": origin,
        "source": "agenda_note",
    }


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

        # Echtzeit-Broadcast an (org, item) und ggf. (org, paper)
        broadcast_preparation_event(
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
            broadcast_preparation_event(
                organization_id,
                {"type": "comment", "event": "deleted", "comment_id": str(note_id)},
                agenda_item_id=agenda_item_id,
            )
            return JsonResponse({"success": True})

        comment = get_object_or_404(PaperComment, id=note_id, author=membership)
        paper_id = comment.paper_id
        organization_id = comment.organization_id
        comment.delete()
        broadcast_preparation_event(
            organization_id,
            {"type": "comment", "event": "deleted", "comment_id": str(note_id)},
            paper_id=paper_id,
        )
        return JsonResponse({"success": True})


class SupplementaryDocumentAPIView(WorkViewMixin, View):
    """API: Ergänzende Dokumente (Links + Uploads + OParl-Referenzen)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        item_id = self.kwargs.get("item_id")
        organization = self.organization

        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id)
        # TOP-Anhänge + über Gremien geteilte Vorlagen-Anhänge der eigenen Org
        docs = AgendaSupplementaryDocument.visible_for_item(organization, agenda_item)

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
                        "paper_id": str(d.paper_id) if d.paper_id else None,
                        "share_across_committees": d.share_across_committees,
                        "is_from_other_item": d.agenda_item_id != agenda_item.id,
                    }
                    for d in docs
                ]
            }
        )

    @staticmethod
    def _resolve_paper_anchor(agenda_item, paper_id, share_flag):
        """
        Vorlagen-Anker validieren: Die Vorlage muss tatsächlich von diesem
        TOP beraten werden (sonst könnte man beliebige Papers verknüpfen).

        Returns (paper, share_across_committees) oder (None, False).
        """
        if not paper_id:
            return None, False
        papers = {str(p.id): p for p in agenda_item.get_papers()}
        paper = papers.get(str(paper_id))
        if paper is None:
            return None, False
        return paper, share_flag in [True, "true", "1", "on"]

    def post(self, request, *args, **kwargs):
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        bodies = organization.get_all_bodies() if organization else None
        if bodies is None or not bodies.exists() or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body__in=bodies)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        # Unterstützt sowohl JSON (Links) als auch Multipart (Uploads)
        if request.content_type and "multipart" in request.content_type:
            return self._handle_file_upload(request, organization, membership, agenda_item)

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST
        doc_type = data.get("document_type", "link")
        title = data.get("title", "").strip()

        if not title:
            return JsonResponse({"error": "Titel erforderlich"}, status=400)

        # Optionaler Anker an der VORLAGE (statt nur am TOP)
        paper, share_across = self._resolve_paper_anchor(
            agenda_item, data.get("paper_id"), data.get("share_across_committees", False)
        )

        doc = AgendaSupplementaryDocument.objects.create(
            organization=organization,
            added_by=membership,
            agenda_item=agenda_item,
            paper=paper,
            share_across_committees=share_across,
            document_type=doc_type,
            title=title,
            url=data.get("url", ""),
            description=data.get("description", ""),
        )

        MeetingPreparation.record_activity(organization, meeting, membership)

        return JsonResponse(
            {
                "success": True,
                "document": {
                    "id": str(doc.id),
                    "title": doc.title,
                    "url": doc.display_url,
                    "document_type": doc.document_type,
                    "paper_id": str(doc.paper_id) if doc.paper_id else None,
                    "share_across_committees": doc.share_across_committees,
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

        # Optionaler Anker an der VORLAGE (statt nur am TOP)
        paper, share_across = self._resolve_paper_anchor(
            agenda_item, request.POST.get("paper_id"), request.POST.get("share_across_committees", False)
        )

        doc = AgendaSupplementaryDocument.objects.create(
            organization=organization,
            added_by=membership,
            agenda_item=agenda_item,
            paper=paper,
            share_across_committees=share_across,
            document_type="file",
            title=title,
            file=uploaded_file,
            filename=uploaded_file.name,
            mime_type=uploaded_file.content_type or "",
            file_size=uploaded_file.size,
            description=request.POST.get("description", ""),
        )

        MeetingPreparation.record_activity(organization, membership=membership, meeting=agenda_item.meeting)

        return JsonResponse(
            {
                "success": True,
                "document": {
                    "id": str(doc.id),
                    "title": doc.title,
                    "url": doc.display_url,
                    "document_type": "file",
                    "paper_id": str(doc.paper_id) if doc.paper_id else None,
                    "share_across_committees": doc.share_across_committees,
                },
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

        # Echtzeit-Broadcast in die (org, paper)-Gruppe; Polling bleibt Fallback
        broadcast_preparation_event(
            organization.id,
            {
                "type": "comment",
                "event": "created",
                "comment": serialize_paper_comment_as_note(comment, membership),
            },
            paper_id=paper.id,
        )

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
        paper_id = comment.paper_id
        organization_id = comment.organization_id
        comment.delete()
        broadcast_preparation_event(
            organization_id,
            {"type": "comment", "event": "deleted", "comment_id": str(comment_id)},
            paper_id=paper_id,
        )
        return JsonResponse({"success": True})


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

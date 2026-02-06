# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meeting preparation views for the Work module.
"""

import json
from datetime import datetime, timedelta

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from insight_core.models import OParlAgendaItem, OParlConsultation, OParlMeeting, OParlOrganization

from .models import (
    AgendaDocumentLink,
    AgendaItemNote,
    AgendaItemPosition,
    AgendaSpeechNote,
    MeetingPreparation,
)


def prefetch_papers_for_agenda_items(agenda_items):
    """
    Pre-fetch papers for a list of agenda items via consultations.
    Returns a dict mapping agenda_item.id to list of papers with their files.
    Papers are annotated with consultation_count to avoid N+1 queries.
    """
    from django.db.models import Count

    if not agenda_items:
        return {}

    # Collect all external_ids
    external_ids = [item.external_id for item in agenda_items if item.external_id]
    if not external_ids:
        return {}

    # Get consultations with papers for these agenda items
    consultations = (
        OParlConsultation.objects.filter(agenda_item_external_id__in=external_ids)
        .select_related("paper")
        .prefetch_related("paper__files", "paper__consultations")
    )

    # Collect unique paper IDs for annotation
    paper_ids = set()
    for consultation in consultations:
        if consultation.paper:
            paper_ids.add(consultation.paper.id)

    # Get papers with consultation count annotation
    paper_consultation_counts = {}
    if paper_ids:
        from insight_core.models import OParlPaper

        papers_with_counts = OParlPaper.objects.filter(id__in=paper_ids).annotate(
            consultation_count=Count("consultations")
        )
        paper_consultation_counts = {p.id: p.consultation_count for p in papers_with_counts}

    # Build mapping: external_id -> list of papers
    papers_by_ext_id = {}
    for consultation in consultations:
        if consultation.paper and consultation.agenda_item_external_id:
            ext_id = consultation.agenda_item_external_id
            if ext_id not in papers_by_ext_id:
                papers_by_ext_id[ext_id] = []
            # Avoid duplicates and attach consultation count
            if consultation.paper not in papers_by_ext_id[ext_id]:
                # Attach pre-computed consultation count to avoid N+1
                consultation.paper._prefetched_consultation_count = paper_consultation_counts.get(
                    consultation.paper.id, 0
                )
                papers_by_ext_id[ext_id].append(consultation.paper)

    # Build mapping: agenda_item.id -> list of papers
    papers_by_item_id = {}
    for item in agenda_items:
        if item.external_id and item.external_id in papers_by_ext_id:
            papers_by_item_id[item.id] = papers_by_ext_id[item.external_id]
        else:
            papers_by_item_id[item.id] = []

    return papers_by_item_id


class MeetingListView(WorkViewMixin, TemplateView):
    """
    List of OParl meetings for preparation.

    Shows meetings from committees the member is assigned to.
    Displays committee name instead of meeting name for clarity.
    """

    template_name = "work/meetings/list.html"
    permission_required = "meetings.view"

    @staticmethod
    def _meeting_matches_committees(meeting: OParlMeeting, committee_ext_ids: set) -> bool:
        """Check if meeting belongs to any of the given committees via raw_json."""
        raw = meeting.raw_json or {}
        org_field = raw.get("organization")
        if isinstance(org_field, list):
            return any(isinstance(value, str) and value in committee_ext_ids for value in org_field)
        if isinstance(org_field, str):
            return org_field in committee_ext_ids
        return False

    @staticmethod
    def _get_organization_name(meeting: OParlMeeting, org_cache: dict) -> str:
        """Get committee name from meeting's raw_json organization field."""
        raw = meeting.raw_json or {}
        org_url = raw.get("organization")

        # organization can be a URL (string) or a list of URLs
        if isinstance(org_url, list) and org_url:
            org_url = org_url[0]  # Use first committee

        if not org_url or not isinstance(org_url, str):
            return meeting.body.name if meeting.body else "Gremium"

        # Check cache
        if org_url in org_cache:
            return org_cache[org_url]

        # Load organization from DB
        try:
            org = OParlOrganization.objects.filter(external_id=org_url).first()
            if org:
                name = org.name or org.short_name or meeting.body.name
                org_cache[org_url] = name
                return name
        except Exception:
            pass

        # Fallback
        org_cache[org_url] = meeting.body.name if meeting.body else "Gremium"
        return org_cache[org_url]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        organization = self.organization
        membership = self.membership

        # Get OParl body linked to this organization
        body = organization.body if organization else None

        # Get filter parameters
        time_filter = self.request.GET.get("time", "upcoming")
        committee_filter = self.request.GET.get("committee", "")
        search_query = self.request.GET.get("q", "")
        view_mode = self.request.GET.get("view", "my")  # 'my' or 'all'

        meetings = []
        assigned_committees = []
        all_committees = []
        now = timezone.now()

        if body and membership:
            # Get committees assigned to this member (with external_id for matching)
            assigned_committees = list(membership.oparl_committees.filter(body=body))
            assigned_committee_ext_ids = {c.external_id for c in assigned_committees if c.external_id}

            # Get all committees for filter dropdown
            all_committees = (
                OParlOrganization.objects.filter(body=body)
                .exclude(organization_type__in=["Fraktion", "Partei"])
                .order_by("name")
            )

            # Build base queryset - filter by start date to limit results
            qs = (
                OParlMeeting.objects.filter(
                    body=body,
                    cancelled=False,
                    start__isnull=False,
                )
                .prefetch_related(
                    "agenda_items",
                )
                .select_related("body")
            )

            # Time filter
            if time_filter == "upcoming":
                future_cutoff = now + timedelta(days=180)
                qs = qs.filter(start__gte=now, start__lte=future_cutoff)
                qs = qs.order_by("start")
            elif time_filter == "past":
                past_cutoff = now - timedelta(days=180)
                qs = qs.filter(start__lt=now, start__gte=past_cutoff)
                qs = qs.order_by("-start")
            else:  # all
                qs = qs.order_by("-start")

            # Search
            if search_query:
                qs = qs.filter(name__icontains=search_query)

            # Convert to list for filtering
            all_meetings = list(qs)

            # Filter by assigned committees using raw_json (like old code)
            if view_mode == "my" and assigned_committee_ext_ids:
                meetings = [m for m in all_meetings if self._meeting_matches_committees(m, assigned_committee_ext_ids)]
            elif committee_filter:
                # Get the committee's external_id for filtering
                filter_committee = OParlOrganization.objects.filter(id=committee_filter).first()
                if filter_committee and filter_committee.external_id:
                    meetings = [
                        m for m in all_meetings if self._meeting_matches_committees(m, {filter_committee.external_id})
                    ]
                else:
                    meetings = all_meetings
            else:
                meetings = all_meetings

            # Limit results
            meetings = meetings[:100]

            # Get preparation status for current user
            prepared_meeting_ids = set(
                MeetingPreparation.objects.filter(membership=membership, is_prepared=True).values_list(
                    "meeting_id", flat=True
                )
            )

            # Organization name cache
            org_cache = {}

            # Enhance meeting objects with display info
            for meeting in meetings:
                meeting.user_prepared = meeting.id in prepared_meeting_ids
                # Use len() on prefetched queryset instead of count() to avoid N+1
                meeting.agenda_count = len(meeting.agenda_items.all())
                # Get committee name from raw_json
                meeting.committee_name = self._get_organization_name(meeting, org_cache)

        context["meetings"] = meetings
        context["assigned_committees"] = assigned_committees
        context["all_committees"] = all_committees
        context["time_filter"] = time_filter
        context["committee_filter"] = committee_filter
        context["search_query"] = search_query
        context["view_mode"] = view_mode
        context["has_body"] = body is not None
        context["has_assignments"] = len(assigned_committees) > 0
        context["now"] = now

        return context


class MeetingCalendarView(WorkViewMixin, TemplateView):
    """Calendar view of meetings."""

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
        """Return meeting events as JSON for FullCalendar."""
        organization = self.organization

        # Get OParl body linked to this organization
        body = organization.body if organization else None
        if not body:
            return JsonResponse([], safe=False)

        # Parse date range from FullCalendar
        start_str = request.GET.get("start")
        end_str = request.GET.get("end")

        qs = OParlMeeting.objects.filter(body=body, cancelled=False).prefetch_related("organizations")

        if start_str:
            try:
                start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                qs = qs.filter(start__gte=start)
            except ValueError:
                pass

        if end_str:
            try:
                end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                qs = qs.filter(start__lte=end)
            except ValueError:
                pass

        events = []
        for meeting in qs:
            title = meeting.get_display_name()
            # Truncate long titles for calendar display
            if len(title) > 40:
                title = title[:37] + "..."

            events.append(
                {
                    "id": str(meeting.id),
                    "title": title,
                    "start": meeting.start.isoformat() if meeting.start else None,
                    "end": meeting.end.isoformat() if meeting.end else None,
                    "url": f"/work/{organization.slug}/meetings/{meeting.id}/",
                    "extendedProps": {
                        "location": meeting.location_name,
                        "fullTitle": meeting.get_display_name(),
                    },
                }
            )

        return JsonResponse(events, safe=False)


class MeetingDetailView(WorkViewMixin, TemplateView):
    """Detail view of a single meeting."""

    template_name = "work/meetings/detail.html"
    permission_required = "meetings.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization

        # Get OParl body linked to this organization
        body = organization.body if organization else None
        if not body:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(
            OParlMeeting.objects.prefetch_related(
                "agenda_items",
            ),
            id=meeting_id,
            body=body,
        )

        if meeting:
            # Add committee name from raw_json
            meeting.committee_name = MeetingListView._get_organization_name(meeting, {})

            # Sort agenda items by number
            agenda_items = sorted(meeting.agenda_items.all(), key=lambda x: (x.number or "999"))

            # Pre-fetch papers for agenda items
            papers_by_item = prefetch_papers_for_agenda_items(agenda_items)

            # Attach papers to agenda items for template access
            for item in agenda_items:
                item.papers_list = papers_by_item.get(item.id, [])
                item.primary_paper = item.papers_list[0] if item.papers_list else None

            # Check user preparation status
            membership = self.membership
            preparation = None
            if membership:
                preparation = MeetingPreparation.objects.filter(membership=membership, meeting=meeting).first()

            context["meeting"] = meeting
            context["agenda_items"] = agenda_items
            context["preparation"] = preparation
            context["is_upcoming"] = meeting.start and meeting.start > timezone.now()

        return context


class MeetingPrepareView(WorkViewMixin, TemplateView):
    """Preparation view for a meeting."""

    template_name = "work/meetings/prepare.html"
    permission_required = "meetings.prepare"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "meetings"

        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization
        membership = self.membership

        # Get OParl body linked to this organization
        body = organization.body if organization else None
        if not body:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(
            OParlMeeting.objects.prefetch_related(
                "organizations",
                "agenda_items",
            ),
            id=meeting_id,
            body=body,
        )

        # Add committee name from raw_json
        meeting.committee_name = MeetingListView._get_organization_name(meeting, {})
        context["meeting"] = meeting

        if meeting and membership:
            # Get or create preparation
            preparation, created = MeetingPreparation.objects.get_or_create(
                organization=organization, membership=membership, meeting=meeting, defaults={}
            )

            # Sort agenda items with natural number sorting
            def natural_sort_key(item):
                """Sort agenda items naturally: 1, 2, 10, 11 instead of 1, 10, 11, 2."""
                import re

                number = item.number or "999"
                # Split into numeric and non-numeric parts
                parts = re.split(r"(\d+)", str(number))
                # Convert numeric parts to integers for proper sorting
                return [int(p) if p.isdigit() else p.lower() for p in parts if p]

            agenda_items = sorted(meeting.agenda_items.all(), key=natural_sort_key)

            # Pre-fetch papers for agenda items
            papers_by_item = prefetch_papers_for_agenda_items(agenda_items)

            # Get positions for each agenda item
            positions_by_item = {}
            for pos in AgendaItemPosition.objects.filter(preparation=preparation).select_related("agenda_item"):
                positions_by_item[pos.agenda_item_id] = pos

            # Get notes/comments for agenda items (visible to current user)
            notes_by_item = {}
            all_notes = AgendaItemNote.objects.filter(agenda_item__meeting=meeting).select_related(
                "author", "author__user"
            )

            for note in all_notes:
                if note.is_visible_to(membership):
                    if note.agenda_item_id not in notes_by_item:
                        notes_by_item[note.agenda_item_id] = []
                    notes_by_item[note.agenda_item_id].append(note)

            # Prepare agenda items with their data
            prepared_items = []
            for item in agenda_items:
                position = positions_by_item.get(item.id)
                notes = notes_by_item.get(item.id, [])
                papers = papers_by_item.get(item.id, [])
                primary_paper = papers[0] if papers else None
                has_files = any(p.files.exists() for p in papers) if papers else False
                prepared_items.append(
                    {
                        "item": item,
                        "position": position,
                        "notes": notes,
                        "papers": papers,
                        "primary_paper": primary_paper,
                        "has_files": has_files,
                    }
                )

            # Position choices for template
            position_choices = AgendaItemPosition.POSITION_CHOICES
            visibility_choices = AgendaItemNote.VISIBILITY_CHOICES

            # Get speech notes for this meeting
            speech_notes = AgendaSpeechNote.objects.filter(meeting=meeting, author=membership).select_related(
                "agenda_item"
            )
            speech_notes_by_item = {sn.agenda_item_id: sn for sn in speech_notes}

            # Add speech notes to prepared items
            for item in prepared_items:
                item["speech_note"] = speech_notes_by_item.get(item["item"].id)

            # Get document links for this meeting
            document_links = AgendaDocumentLink.objects.filter(
                agenda_item__meeting=meeting, organization=organization
            ).select_related("agenda_item", "added_by__user")
            doc_links_by_item = {}
            for link in document_links:
                if link.agenda_item_id not in doc_links_by_item:
                    doc_links_by_item[link.agenda_item_id] = []
                doc_links_by_item[link.agenda_item_id].append(link)

            # Add document links to prepared items
            for item in prepared_items:
                item["document_links"] = doc_links_by_item.get(item["item"].id, [])

            # Summary stats
            stats = {
                "total_items": len(agenda_items),
                "positioned": len([i for i in prepared_items if i["position"] and i["position"].position != "open"]),
                "want_to_speak": len([i for i in prepared_items if i["speech_note"]]),
                "with_notes": len([i for i in prepared_items if i["position"] and i["position"].notes_encrypted]),
            }

            context["meeting"] = meeting
            context["preparation"] = preparation
            context["prepared_items"] = prepared_items
            context["position_choices"] = position_choices
            context["visibility_choices"] = visibility_choices
            context["stats"] = stats

            # JSON data for Alpine.js two-column layout
            prepared_items_json = json.dumps(
                [
                    {
                        "id": str(item["item"].id),
                        "number": item["item"].number or str(idx + 1),
                        "name": item["item"].name or "Ohne Titel",
                        "position": item["position"].position if item["position"] else "open",
                        "notes": item["position"].get_notes_decrypted() if item["position"] else "",
                        "discussionNote": item["position"].discussion_note if item["position"] else "",
                        "isFinal": item["position"].is_final if item["position"] else False,
                        "hasSpeechNote": bool(item["speech_note"]),
                        "speechTitle": item["speech_note"].title if item["speech_note"] else "",
                        "speechContent": item["speech_note"].content if item["speech_note"] else "",
                        "speechDuration": item["speech_note"].estimated_duration if item["speech_note"] else 0,
                        "speechShared": item["speech_note"].is_shared if item["speech_note"] else False,
                        # Paper details
                        "paper": {
                            "id": str(item["primary_paper"].id),
                            "name": item["primary_paper"].name or "Ohne Titel",
                            "reference": item["primary_paper"].reference or "",
                            "paperType": item["primary_paper"].paper_type or "",
                            # Use prefetched count to avoid N+1 queries
                            "consultationCount": getattr(item["primary_paper"], "_prefetched_consultation_count", 0)
                            if item["primary_paper"]
                            else 0,
                        }
                        if item["primary_paper"]
                        else None,
                        "hasFiles": item["has_files"],
                        "files": [
                            {
                                "name": f.name or f.file_name or "Dokument",
                                "url": f.access_url or f.download_url,
                            }
                            for p in item["papers"]
                            for f in p.files.all()
                            if f.access_url or f.download_url  # Only include files with URLs
                        ],
                        "documentLinks": [
                            {"id": str(link.id), "title": link.title, "url": link.url}
                            for link in item["document_links"]
                        ],
                        "notesCount": len(item["notes"]),
                    }
                    for idx, item in enumerate(prepared_items)
                ]
            )
            context["prepared_items_json"] = prepared_items_json

        return context

    def post(self, request, *args, **kwargs):
        """Handle form submissions (mark as prepared)."""
        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body:
            return redirect("work:meetings", org_slug=organization.slug)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)

        if membership:
            preparation = MeetingPreparation.objects.filter(membership=membership, meeting=meeting).first()

            if preparation:
                action = request.POST.get("action")
                if action == "mark_prepared":
                    preparation.is_prepared = True
                    preparation.prepared_at = timezone.now()
                    preparation.save()
                elif action == "unmark_prepared":
                    preparation.is_prepared = False
                    preparation.prepared_at = None
                    preparation.save()
                elif action == "save_notes":
                    notes = request.POST.get("notes", "")
                    preparation.set_notes_encrypted(notes)
                    preparation.save()

        return redirect("work:meeting_prepare", org_slug=organization.slug, meeting_id=meeting_id)


class AgendaPositionAPIView(WorkViewMixin, View):
    """API endpoint for saving agenda item positions (HTMX)."""

    permission_required = "meetings.prepare"

    def post(self, request, *args, **kwargs):
        """Save or update a position."""
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        # Get or create preparation
        preparation, _ = MeetingPreparation.objects.get_or_create(
            organization=organization, membership=membership, meeting=meeting, defaults={}
        )

        # Get or create position
        position, created = AgendaItemPosition.objects.get_or_create(
            preparation=preparation, agenda_item=agenda_item, defaults={}
        )

        # Update position
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        if "position" in data:
            position.position = data["position"]
        if "notes" in data:
            position.set_notes_encrypted(data["notes"])
        if "discussion_note" in data:
            position.discussion_note = data["discussion_note"]
        if "is_final" in data:
            position.is_final = data["is_final"] in [True, "true", "1", "on"]

        position.save()

        return JsonResponse(
            {
                "success": True,
                "position": position.position,
                "position_display": position.get_position_display(),
                "has_notes": bool(position.notes_encrypted),
                "has_discussion_note": bool(position.discussion_note),
                "is_final": position.is_final,
            }
        )


class AgendaNotesAPIView(WorkViewMixin, View):
    """API endpoint for collaborative notes on agenda items (HTMX)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        """Get notes for an agenda item."""
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        # Get visible notes
        all_notes = (
            AgendaItemNote.objects.filter(agenda_item=agenda_item)
            .select_related("author", "author__user")
            .order_by("-is_pinned", "-is_decision", "-created_at")
        )

        visible_notes = [n for n in all_notes if n.is_visible_to(membership)]

        notes_data = []
        for note in visible_notes:
            notes_data.append(
                {
                    "id": str(note.id),
                    "content": note.get_content_decrypted(),
                    "visibility": note.visibility,
                    "visibility_display": note.get_visibility_display(),
                    "is_decision": note.is_decision,
                    "is_pinned": note.is_pinned,
                    "author": note.author.user.get_display_name(),
                    "is_own": note.author == membership,
                    "created_at": note.created_at.isoformat(),
                }
            )

        return JsonResponse({"notes": notes_data})

    def post(self, request, *args, **kwargs):
        """Add a new note."""
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        content = data.get("content", "").strip()
        if not content:
            return JsonResponse({"error": "Content required"}, status=400)

        visibility = data.get("visibility", "organization")
        is_decision = data.get("is_decision", False) in [True, "true", "1", "on"]

        note = AgendaItemNote(
            organization=organization,
            agenda_item=agenda_item,
            author=membership,
            visibility=visibility,
            is_decision=is_decision,
        )
        note.set_content_encrypted(content)
        note.save()

        return JsonResponse(
            {
                "success": True,
                "note": {
                    "id": str(note.id),
                    "content": content,  # Return original content
                    "visibility_display": note.get_visibility_display(),
                    "is_decision": note.is_decision,
                    "author": membership.user.get_display_name(),
                },
            }
        )

    def delete(self, request, *args, **kwargs):
        """Delete a note (only own notes)."""
        note_id = self.kwargs.get("note_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        note = get_object_or_404(AgendaItemNote, id=note_id, author=membership)
        note.delete()

        return JsonResponse({"success": True})


class PreparationSummaryView(WorkViewMixin, TemplateView):
    """Summary view of all positions for a meeting."""

    template_name = "work/meetings/_summary.html"
    permission_required = "meetings.prepare"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        meeting_id = self.kwargs.get("meeting_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body:
            context["error"] = "Keine OParl-Körperschaft verknüpft"
            return context

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        context["meeting"] = meeting

        if membership:
            preparation = MeetingPreparation.objects.filter(membership=membership, meeting=meeting).first()

            if preparation:
                positions = (
                    AgendaItemPosition.objects.filter(preparation=preparation)
                    .exclude(position="open")
                    .select_related("agenda_item")
                )

                # Group by position type
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

                # Get speech notes for this meeting
                speeches = (
                    AgendaSpeechNote.objects.filter(meeting=meeting, author=membership)
                    .select_related("agenda_item")
                    .order_by("agenda_item__number")
                )

                context["positions_by_type"] = positions_by_type
                context["speeches"] = speeches
                context["preparation"] = preparation
        return context


class SpeechNoteAPIView(WorkViewMixin, View):
    """API endpoint for managing speech notes (teleprompter content)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        """Get speech note for an agenda item."""
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        speech_note = AgendaSpeechNote.objects.filter(
            meeting=meeting, agenda_item=agenda_item, author=membership
        ).first()

        if speech_note:
            return JsonResponse(
                {
                    "exists": True,
                    "id": str(speech_note.id),
                    "title": speech_note.title,
                    "content": speech_note.content,
                    "estimated_duration": speech_note.estimated_duration,
                    "is_shared": speech_note.is_shared,
                }
            )
        return JsonResponse({"exists": False})

    def post(self, request, *args, **kwargs):
        """Create or update speech note."""
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        speech_note, created = AgendaSpeechNote.objects.update_or_create(
            organization=organization,
            author=membership,
            meeting=meeting,
            agenda_item=agenda_item,
            defaults={
                "title": data.get("title", ""),
                "content": data.get("content", ""),
                "estimated_duration": int(data.get("estimated_duration", 0)),
                "is_shared": data.get("is_shared", False) in [True, "true", "1", "on"],
            },
        )

        return JsonResponse(
            {
                "success": True,
                "id": str(speech_note.id),
                "created": created,
            }
        )

    def delete(self, request, *args, **kwargs):
        """Delete speech note."""
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        AgendaSpeechNote.objects.filter(meeting_id=meeting_id, agenda_item_id=item_id, author=membership).delete()

        return JsonResponse({"success": True})


class TeleprompterView(WorkViewMixin, TemplateView):
    """Full-screen teleprompter view for speech notes."""

    template_name = "work/meetings/teleprompter.html"
    permission_required = "meetings.prepare"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if body and membership:
            meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
            agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

            speech_note = AgendaSpeechNote.objects.filter(
                meeting=meeting, agenda_item=agenda_item, author=membership
            ).first()

            context["meeting"] = meeting
            context["agenda_item"] = agenda_item
            context["speech_note"] = speech_note

        return context


class DocumentLinkAPIView(WorkViewMixin, View):
    """API endpoint for managing document links on agenda items."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        """Get document links for an agenda item."""
        item_id = self.kwargs.get("item_id")
        organization = self.organization

        links = AgendaDocumentLink.objects.filter(agenda_item_id=item_id, organization=organization).select_related(
            "added_by__user"
        )

        return JsonResponse(
            {
                "links": [
                    {
                        "id": str(link.id),
                        "title": link.title,
                        "url": link.url,
                        "description": link.description,
                        "added_by": link.added_by.user.get_display_name(),
                        "created_at": link.created_at.isoformat(),
                    }
                    for link in links
                ]
            }
        )

    def post(self, request, *args, **kwargs):
        """Add a document link."""
        meeting_id = self.kwargs.get("meeting_id")
        item_id = self.kwargs.get("item_id")
        organization = self.organization
        membership = self.membership

        body = organization.body if organization else None
        if not body or not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        meeting = get_object_or_404(OParlMeeting, id=meeting_id, body=body)
        agenda_item = get_object_or_404(OParlAgendaItem, id=item_id, meeting=meeting)

        data = json.loads(request.body) if request.content_type == "application/json" else request.POST

        title = data.get("title", "").strip()
        url = data.get("url", "").strip()

        if not title or not url:
            return JsonResponse({"error": "Titel und URL erforderlich"}, status=400)

        link = AgendaDocumentLink.objects.create(
            organization=organization,
            added_by=membership,
            agenda_item=agenda_item,
            title=title,
            url=url,
            description=data.get("description", ""),
        )

        return JsonResponse(
            {
                "success": True,
                "link": {
                    "id": str(link.id),
                    "title": link.title,
                    "url": link.url,
                },
            }
        )

    def delete(self, request, *args, **kwargs):
        """Delete a document link (only own links)."""
        link_id = self.kwargs.get("link_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        link = get_object_or_404(AgendaDocumentLink, id=link_id, added_by=membership)
        link.delete()

        return JsonResponse({"success": True})


class PaperCommentAPIView(WorkViewMixin, View):
    """API endpoint for comments on OParl Papers (cross-committee collaboration)."""

    permission_required = "meetings.prepare"

    def get(self, request, *args, **kwargs):
        """Get comments for a paper visible to the current user."""
        from insight_core.models import OParlPaper

        from .models import PaperComment

        paper_id = self.kwargs.get("paper_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        paper = get_object_or_404(OParlPaper, id=paper_id)

        # Get visible comments
        visible_comments = PaperComment.get_visible_comments_for_paper(paper, membership)

        comments_data = []
        for comment in visible_comments:
            comments_data.append(
                {
                    "id": str(comment.id),
                    "content": comment.get_content_decrypted(),
                    "visibility": comment.visibility,
                    "visibility_display": comment.get_visibility_display(),
                    "is_recommendation": comment.is_recommendation,
                    "author": comment.author.user.get_display_name(),
                    "organization": comment.organization.name,
                    "is_own": comment.author == membership,
                    "is_own_org": comment.organization == membership.organization,
                    "created_at": comment.created_at.isoformat(),
                }
            )

        return JsonResponse({"comments": comments_data})

    def post(self, request, *args, **kwargs):
        """Add a new comment on a paper."""
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
        if visibility not in ["private", "organization", "consulting"]:
            visibility = "organization"

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
        """Delete a comment (only own comments)."""
        from .models import PaperComment

        comment_id = self.kwargs.get("comment_id")
        membership = self.membership

        if not membership:
            return JsonResponse({"error": "Unauthorized"}, status=403)

        comment = get_object_or_404(PaperComment, id=comment_id, author=membership)
        comment.delete()

        return JsonResponse({"success": True})

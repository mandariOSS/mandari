# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Faction meeting views for the Work module.

Simplified architecture: 4 views instead of 13.
- FactionMeetingListView: List + Create (POST)
- FactionMeetingDetailView: Detail/Protocol page
- FactionActionView: Central HTMX action handler
- FactionSettingsView: Legacy redirect to organization settings
"""

import json
import logging

from django.db import models
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from ..models import (
    FactionAgendaItem,
    FactionAgendaItemAttachment,
    FactionDecision,
    FactionMeeting,
    FactionProtocolEntry,
)

logger = logging.getLogger(__name__)


class FactionItemPanelView(WorkViewMixin, TemplateView):
    """GET: Render agenda item slide-over panel content."""

    template_name = "work/faction/_agenda_item_panel.html"
    permission_required = "faction.view_public"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meeting = get_object_or_404(FactionMeeting, id=kwargs["meeting_id"], organization=self.organization)
        item = get_object_or_404(
            FactionAgendaItem.objects.select_related(
                "meeting", "parent", "approves_meeting", "related_agenda_item"
            ).prefetch_related(
                "protocol_entries__speaker__user",
                "protocol_entries__created_by__user",
                "attachments__uploaded_by__user",
                "tasks__assigned_to__user",
                "tasks__created_by__user",
                "related_motions",
                "children",
            ),
            id=kwargs["item_id"],
            meeting=meeting,
        )

        # Try to get decision (OneToOne, may not exist)
        try:
            decision = item.decision
        except FactionDecision.DoesNotExist:
            decision = None

        can_edit = (
            meeting.created_by == self.membership or self.membership.has_permission("faction.manage")
        ) and meeting.status in ["draft", "planned", "invited", "ongoing"]

        is_protocol_phase = meeting.status in ["ongoing", "completed"] and not meeting.protocol_approved
        can_protocol = is_protocol_phase and (
            meeting.created_by == self.membership
            or self.membership.has_permission("faction.manage")
            or self.membership.has_permission("protocols.create")
            or self.membership.has_permission("protocols.edit")
        )

        # Attendances for speaker select
        attendances = meeting.attendances.select_related("membership__user")

        # Available motions for linking
        from apps.work.motions.models import Motion

        available_motions = (
            Motion.objects.filter(organization=self.organization)
            .exclude(id__in=item.related_motions.values_list("id", flat=True))
            .order_by("-created_at")[:50]
        )

        # Available members for task assignment
        from apps.tenants.models import Membership

        available_members = (
            Membership.objects.filter(organization=self.organization, is_active=True)
            .select_related("user")
            .order_by("user__last_name", "user__first_name")
        )

        # Linked RIS papers
        linked_papers = item.related_papers.all().order_by("-date")

        context.update(
            {
                "item": item,
                "meeting": meeting,
                "decision": decision,
                "can_edit": can_edit,
                "is_protocol_phase": is_protocol_phase,
                "can_protocol": can_protocol,
                "attendances": attendances,
                "protocol_entries": item.protocol_entries.select_related("speaker__user", "created_by__user").order_by(
                    "order", "created_at"
                ),
                "attachments": item.attachments.select_related("uploaded_by__user").order_by("-created_at"),
                "tasks": item.tasks.select_related("assigned_to__user", "created_by__user"),
                "linked_motions": item.related_motions.all(),
                "linked_papers": linked_papers,
                "available_motions": available_motions,
                "available_members": available_members,
                "reference_links": item.reference_links or [],
                "organization": self.organization,
                "org_slug": self.organization.slug,
                "membership": self.membership,
            }
        )
        return context


class FactionItemPanelActionView(WorkViewMixin, View):
    """Central POST handler for agenda item panel actions."""

    permission_required = "faction.view_public"

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs["meeting_id"], organization=self.organization)
        item = get_object_or_404(FactionAgendaItem, id=kwargs["item_id"], meeting=meeting)

        action = request.POST.get("action")
        can_edit = (
            meeting.created_by == self.membership or self.membership.has_permission("faction.manage")
        ) and meeting.status in ["draft", "planned", "invited", "ongoing"]

        handlers = {
            "update": self._update,
            "add_entry": self._add_entry,
            "edit_entry": self._edit_entry,
            "delete_entry": self._delete_entry,
            "record_decision": self._record_decision,
            "clear_decision": self._clear_decision,
            "upload_attachment": self._upload_attachment,
            "delete_attachment": self._delete_attachment,
            "link_motion": self._link_motion,
            "unlink_motion": self._unlink_motion,
            "link_paper": self._link_paper,
            "unlink_paper": self._unlink_paper,
            "search_papers": self._search_papers,
            "create_task": self._create_task,
            "add_link": self._add_link,
            "remove_link": self._remove_link,
        }

        handler = handlers.get(action)
        if not handler:
            return HttpResponse(status=400)

        return handler(request, meeting, item, can_edit)

    def _can_protocol(self, meeting):
        """Wer darf Protokolleinträge/Beschlüsse erfassen (solange Protokoll nicht genehmigt)."""
        if meeting.protocol_approved:
            return False
        return (
            meeting.created_by == self.membership
            or self.membership.has_permission("faction.manage")
            or self.membership.has_permission("protocols.create")
            or self.membership.has_permission("protocols.edit")
        )

    def _render_panel(self, request, meeting, item):
        """Re-render the full panel."""
        view = FactionItemPanelView()
        view.request = request
        view.organization = self.organization
        view.membership = self.membership
        view.kwargs = {"meeting_id": meeting.id, "item_id": item.id}
        context = view.get_context_data(meeting_id=meeting.id, item_id=item.id)
        return render_to_string("work/faction/_agenda_item_panel.html", context, request=request)

    def _render_section(self, template, context, request):
        """Render a single section partial."""
        return render_to_string(template, context, request=request)

    def _success_response(self, html, message=None):
        """Build response with optional toast trigger."""
        response = HttpResponse(html)
        triggers = {"agenda-refresh": True}
        if message:
            triggers["show-toast"] = {"message": message, "type": "success"}
        response["HX-Trigger"] = json.dumps(triggers)
        return response

    def _panel_response(self, request, meeting, item, message=None):
        """Re-render the full panel and return."""
        # Reload item to get fresh data
        item = FactionAgendaItem.objects.get(id=item.id)
        html = self._render_panel(request, meeting, item)
        resp = self._success_response(html, message)
        return resp

    def _none_response(self, message=None):
        """Return empty response for hx-swap=none (auto-save)."""
        response = HttpResponse(status=204)
        triggers = {"agenda-refresh": True}
        if message:
            triggers["panel-autosaved"] = True
        response["HX-Trigger"] = json.dumps(triggers)
        return response

    # -- Action handlers ---------------------------------------------------

    def _update(self, request, meeting, item, can_edit):
        """Auto-save: title, description, visibility."""
        if not can_edit:
            return HttpResponse(status=403)

        title = request.POST.get("title", "").strip()
        if title:
            item.title = title

        description = request.POST.get("description", "")
        if description is not None:
            if description.strip():
                item.set_description_encrypted(description.strip())
            else:
                item.description_encrypted = None

        visibility = request.POST.get("visibility")
        if visibility in ("public", "internal"):
            item.visibility = visibility

        item.save()
        return self._none_response(message="Gespeichert")

    def _add_entry(self, request, meeting, item, can_edit):
        """Add protocol entry."""
        if not can_edit and not self._can_protocol(meeting):
            return HttpResponse(status=403)

        entry_type = request.POST.get("entry_type", "note")
        content = request.POST.get("content", "").strip()

        if not content:
            return HttpResponse("Inhalt ist erforderlich.", status=400)

        entry = FactionProtocolEntry(
            meeting=meeting,
            agenda_item=item,
            entry_type=entry_type,
            created_by=self.membership,
            # Sitzungsweite Nummerierung — identisch zum Inline-Formular
            # (FactionActionView._add_entry), damit die Reihenfolge konsistent bleibt
            order=meeting.protocol_entries.count() + 1,
        )

        speaker_id = request.POST.get("speaker")
        if speaker_id and entry_type == "speech":
            entry.speaker_id = speaker_id

        if entry_type == "action":
            assignee_id = request.POST.get("action_assignee")
            due_date = request.POST.get("action_due_date")
            if assignee_id:
                entry.action_assignee_id = assignee_id
            if due_date:
                entry.action_due_date = due_date

        entry.save()
        entry.set_content_encrypted(content)
        entry.save()

        return self._panel_response(request, meeting, item, "Protokolleintrag hinzugefügt")

    def _edit_entry(self, request, meeting, item, can_edit):
        """Edit protocol entry."""
        entry_id = request.POST.get("entry_id")
        content = request.POST.get("content", "").strip()

        if not entry_id or not content:
            return HttpResponse(status=400)

        entry = get_object_or_404(FactionProtocolEntry, id=entry_id, meeting=meeting)
        is_own = entry.created_by == self.membership and not meeting.protocol_approved
        if not can_edit and not self._can_protocol(meeting) and not is_own:
            return HttpResponse(status=403)

        entry.set_content_encrypted(content)

        entry_type = request.POST.get("entry_type")
        if entry_type:
            entry.entry_type = entry_type

        speaker_id = request.POST.get("speaker")
        if speaker_id:
            entry.speaker_id = speaker_id

        entry.save()
        return self._panel_response(request, meeting, item, "Eintrag aktualisiert")

    def _delete_entry(self, request, meeting, item, can_edit):
        """Delete protocol entry."""
        entry_id = request.POST.get("entry_id")
        entry = FactionProtocolEntry.objects.filter(id=entry_id, meeting=meeting, agenda_item=item).first()
        if entry:
            # Gleiche Regel wie im Inline-Formular (FactionActionView._delete_entry):
            # Protokollberechtigte oder der Ersteller (solange Protokoll nicht genehmigt)
            is_own = entry.created_by == self.membership and not meeting.protocol_approved
            if not can_edit and not self._can_protocol(meeting) and not is_own:
                return HttpResponse(status=403)
            entry.delete()
        return self._panel_response(request, meeting, item, "Eintrag gelöscht")

    def _record_decision(self, request, meeting, item, can_edit):
        """Record or update a decision/vote."""
        if not can_edit and not self._can_protocol(meeting):
            return HttpResponse(status=403)

        try:
            votes_yes = int(request.POST.get("votes_yes", 0))
            votes_no = int(request.POST.get("votes_no", 0))
            votes_abstain = int(request.POST.get("votes_abstain", 0))
        except ValueError:
            return HttpResponse("Ungültige Stimmzahlen.", status=400)

        result = request.POST.get("result", "accepted")
        decision_text = request.POST.get("decision_text", "").strip()
        notes = request.POST.get("notes", "").strip()

        FactionDecision.objects.update_or_create(
            agenda_item=item,
            defaults={
                "votes_yes": votes_yes,
                "votes_no": votes_no,
                "votes_abstain": votes_abstain,
                "result": result,
                "decision_text": decision_text,
                "notes": notes,
                "recorded_by": self.membership,
            },
        )

        item.has_decision = True
        item.votes_for = votes_yes
        item.votes_against = votes_no
        item.votes_abstain = votes_abstain
        item.save()

        return self._panel_response(request, meeting, item, "Abstimmung erfasst")

    def _clear_decision(self, request, meeting, item, can_edit):
        """Remove decision from agenda item."""
        if not can_edit and not self._can_protocol(meeting):
            return HttpResponse(status=403)

        FactionDecision.objects.filter(agenda_item=item).delete()
        item.has_decision = False
        item.votes_for = 0
        item.votes_against = 0
        item.votes_abstain = 0
        item.save()

        return self._panel_response(request, meeting, item, "Abstimmung entfernt")

    def _upload_attachment(self, request, meeting, item, can_edit):
        """Upload file attachment."""
        if not can_edit:
            return HttpResponse(status=403)

        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            return HttpResponse("Keine Datei ausgewählt.", status=400)

        attachment = FactionAgendaItemAttachment.objects.create(
            agenda_item=item,
            file=uploaded_file,
            filename=uploaded_file.name,
            mime_type=uploaded_file.content_type or "",
            file_size=uploaded_file.size,
            uploaded_by=self.membership,
        )

        return self._panel_response(request, meeting, item, f'"{attachment.filename}" hochgeladen')

    def _delete_attachment(self, request, meeting, item, can_edit):
        """Delete file attachment."""
        if not can_edit:
            return HttpResponse(status=403)

        attachment_id = request.POST.get("attachment_id")
        attachment = FactionAgendaItemAttachment.objects.filter(id=attachment_id, agenda_item=item).first()
        if attachment:
            attachment.file.delete(save=False)
            attachment.delete()

        return self._panel_response(request, meeting, item, "Anhang entfernt")

    def _link_motion(self, request, meeting, item, can_edit):
        """Link a motion to this agenda item."""
        if not can_edit:
            return HttpResponse(status=403)

        motion_id = request.POST.get("motion_id")
        if not motion_id:
            return HttpResponse(status=400)

        from apps.work.motions.models import Motion

        motion = get_object_or_404(Motion, id=motion_id, organization=self.organization)
        item.related_motions.add(motion)

        return self._panel_response(request, meeting, item, f'Antrag "{motion.title[:50]}" verknüpft')

    def _unlink_motion(self, request, meeting, item, can_edit):
        """Unlink a motion from this agenda item."""
        if not can_edit:
            return HttpResponse(status=403)

        motion_id = request.POST.get("motion_id")
        if motion_id:
            item.related_motions.remove(motion_id)

        return self._panel_response(request, meeting, item, "Verknüpfung gelöst")

    def _link_paper(self, request, meeting, item, can_edit):
        """Link an OParl paper (RIS-Vorlage) to this agenda item."""
        if not can_edit:
            return HttpResponse(status=403)

        from insight_core.models import OParlPaper

        paper_id = request.POST.get("paper_id")
        if paper_id:
            bodies = self.organization.get_all_bodies()
            if bodies.exists():
                paper = OParlPaper.objects.filter(id=paper_id, body__in=bodies).first()
                if paper:
                    item.related_papers.add(paper)

        return self._panel_response(request, meeting, item, "RIS-Vorlage verknüpft")

    def _unlink_paper(self, request, meeting, item, can_edit):
        """Unlink an OParl paper from this agenda item."""
        if not can_edit:
            return HttpResponse(status=403)

        paper_id = request.POST.get("paper_id")
        if paper_id:
            item.related_papers.remove(paper_id)

        return self._panel_response(request, meeting, item, "Verknüpfung gelöst")

    def _search_papers(self, request, meeting, item, can_edit):
        """Search OParl papers for linking — returns JSON results."""
        query = request.POST.get("q", "").strip()
        if not query or len(query) < 2:
            return JsonResponse({"results": []})

        from insight_core.models import OParlPaper

        bodies = self.organization.get_all_bodies()
        if not bodies.exists():
            return JsonResponse({"results": []})

        papers = (
            OParlPaper.objects.filter(body__in=bodies)
            .filter(models.Q(name__icontains=query) | models.Q(reference__icontains=query))
            .exclude(id__in=item.related_papers.values_list("id", flat=True))
            .order_by("-date")[:15]
        )

        results = [
            {
                "id": str(p.id),
                "name": p.name,
                "reference": p.reference or "",
                "paper_type": p.paper_type or "",
                "date": p.date.strftime("%d.%m.%Y") if p.date else "",
            }
            for p in papers
        ]

        return JsonResponse({"results": results})

    def _create_task(self, request, meeting, item, can_edit):
        """Create a task linked to this agenda item."""
        if not can_edit:
            return HttpResponse(status=403)

        title = request.POST.get("title", "").strip()
        if not title:
            return HttpResponse("Titel ist erforderlich.", status=400)

        from apps.work.tasks.models import Task

        assigned_to_id = request.POST.get("assigned_to")
        due_date = request.POST.get("due_date") or None

        task = Task.objects.create(
            organization=self.organization,
            title=title,
            description=f"Aus Fraktionssitzung: {meeting.title}\nTOP: {item.number} {item.title}",
            assigned_to_id=assigned_to_id if assigned_to_id else None,
            due_date=due_date,
            created_by=self.membership,
            related_faction_meeting=meeting,
            related_faction_agenda_item=item,
        )

        return self._panel_response(request, meeting, item, f'Aufgabe "{task.title[:50]}" erstellt')

    def _add_link(self, request, meeting, item, can_edit):
        """Add reference link."""
        if not can_edit:
            return HttpResponse(status=403)

        label = request.POST.get("link_label", "").strip()
        url = request.POST.get("link_url", "").strip()

        if not label or not url:
            return HttpResponse("Label und URL sind erforderlich.", status=400)

        links = item.reference_links or []
        links.append({"label": label, "url": url})
        item.reference_links = links
        item.save(update_fields=["reference_links"])

        return self._panel_response(request, meeting, item, "Link hinzugefügt")

    def _remove_link(self, request, meeting, item, can_edit):
        """Remove reference link by index."""
        if not can_edit:
            return HttpResponse(status=403)

        try:
            index = int(request.POST.get("link_index", -1))
        except ValueError:
            return HttpResponse(status=400)

        links = item.reference_links or []
        if 0 <= index < len(links):
            links.pop(index)
            item.reference_links = links
            item.save(update_fields=["reference_links"])

        return self._panel_response(request, meeting, item, "Link entfernt")

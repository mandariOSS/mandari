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
from datetime import datetime, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from .models import (
    FactionAgendaItem,
    FactionAgendaItemAttachment,
    FactionAttendance,
    FactionDecision,
    FactionMeeting,
    FactionProtocolEntry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_meeting_context(view, meeting):
    """Build shared context dict for detail page and partials."""
    from apps.common.permissions import PermissionChecker

    checker = PermissionChecker(view.membership)

    # Agenda items (top-level only, children via prefetch)
    agenda_items = (
        meeting.agenda_items.filter(parent__isnull=True)
        .select_related("related_agenda_item", "approves_meeting")
        .prefetch_related("protocol_entries", "protocol_entries__speaker__user", "children")
        .order_by("order", "number")
    )

    public_items = [i for i in agenda_items if i.visibility == "public"]
    can_view_internal = checker.can_access_non_public()
    internal_items = [i for i in agenda_items if i.visibility == "internal"] if can_view_internal else []

    # Attendance
    attendances = meeting.attendances.select_related("membership__user")

    try:
        my_attendance = meeting.attendances.get(membership=view.membership)
    except FactionAttendance.DoesNotExist:
        my_attendance = None

    attendance_stats = {
        "confirmed": sum(1 for a in attendances if a.status == "confirmed"),
        "declined": sum(1 for a in attendances if a.status == "declined"),
        "tentative": sum(1 for a in attendances if a.status == "tentative"),
        "pending": sum(1 for a in attendances if a.status == "invited"),
        "present": sum(1 for a in attendances if a.status == "present"),
        "absent": sum(1 for a in attendances if a.status == "absent"),
        "excused": sum(1 for a in attendances if a.status == "excused"),
    }

    # Permissions
    can_edit = (
        meeting.created_by == view.membership or view.membership.has_permission("faction.manage")
    ) and meeting.status in ["draft", "planned", "invited", "ongoing"]

    start_allowed_from = meeting.start - timedelta(minutes=30)
    can_start = (
        view.membership.has_permission("faction.start")
        and meeting.status in ["planned", "invited"]
        and start_allowed_from <= timezone.now()
    )

    can_manage_attendance = view.membership.has_permission("faction.manage")
    can_propose = checker.can_propose_agenda_items()
    can_create_directly = checker.can_create_agenda_items_directly()

    # Available members (for adding attendees)
    existing_ids = list(attendances.filter(membership__isnull=False).values_list("membership_id", flat=True))
    from apps.tenants.models import Membership
    available_members = (
        Membership.objects.filter(organization=view.organization, is_active=True)
        .exclude(id__in=existing_ids)
        .select_related("user")
        .order_by("user__last_name", "user__first_name")
    )

    # Protocol entries (sidebar summary)
    protocol_entries = meeting.protocol_entries.select_related(
        "agenda_item", "speaker__user", "created_by__user"
    ).order_by("-created_at")[:10]

    protocol_entry_count = meeting.protocol_entries.count()

    return {
        "meeting": meeting,
        "agenda_items": agenda_items,
        "public_agenda_items": public_items,
        "internal_agenda_items": internal_items,
        "can_view_internal": can_view_internal,
        "attendances": attendances,
        "my_attendance": my_attendance,
        "attendance_stats": attendance_stats,
        "can_edit": can_edit,
        "can_start": can_start,
        "can_manage_attendance": can_manage_attendance,
        "can_propose_agenda": can_propose and not can_create_directly,
        "can_approve_proposals": checker.can_approve_agenda_items() or view.membership.has_permission("agenda.manage"),
        "pending_proposals": meeting.agenda_items.filter(proposal_status="proposed"),
        "protocol_entries": protocol_entries,
        "protocol_entry_count": protocol_entry_count,
        "available_members": available_members,
        "status_choices": FactionMeeting.STATUS_CHOICES,
        "is_creator": meeting.created_by == view.membership,
        "organization": view.organization,
        "org_slug": view.organization.slug,
        "membership": view.membership,
    }


def _render_partial(template_name, context, request=None):
    """Render a template partial to string."""
    return render_to_string(template_name, context, request=request)


def _htmx_response(html, trigger=None, refresh=False):
    """Build an HTMX response with optional triggers."""
    response = HttpResponse(html)
    if trigger:
        response["HX-Trigger"] = trigger
    if refresh:
        response["HX-Refresh"] = "true"
    return response


def _renumber_items(meeting, visibility):
    """Renumber items after reordering to maintain consistent numbering."""
    items = meeting.agenda_items.filter(
        visibility=visibility, parent__isnull=True, is_approval_item=False
    ).order_by("order")

    prefix = "NÖ " if visibility == "internal" else ""
    start_num = 1

    if visibility == "public" and meeting.agenda_items.filter(is_approval_item=True).exists():
        start_num = 2

    for i, item in enumerate(items, start=start_num):
        new_number = f"{prefix}{i}"
        if item.number != new_number:
            item.number = new_number
            item.save(update_fields=["number"])

        for j, child in enumerate(item.children.order_by("order"), start=1):
            child_number = f"{new_number}.{j}"
            if child.number != child_number:
                child.number = child_number
                child.save(update_fields=["number"])


# ---------------------------------------------------------------------------
# 1. List + Create
# ---------------------------------------------------------------------------

class FactionMeetingListView(WorkViewMixin, TemplateView):
    """List of faction meetings. POST creates a new meeting (from modal)."""

    template_name = "work/faction/list.html"
    permission_required = "faction.view_public"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "faction"

        meetings = FactionMeeting.objects.filter(
            organization=self.organization
        ).select_related("created_by__user", "schedule")

        # Filter by status
        status = self.request.GET.get("status")
        if status:
            meetings = meetings.filter(status=status)
            context["selected_status"] = status

        # Filter by time
        time_filter = self.request.GET.get("time", "upcoming")
        now = timezone.now()
        if time_filter == "upcoming":
            meetings = meetings.filter(start__gte=now)
            context["selected_time"] = "upcoming"
        elif time_filter == "past":
            meetings = meetings.filter(start__lt=now)
            context["selected_time"] = "past"
        else:
            context["selected_time"] = "all"

        # Search
        search = self.request.GET.get("q", "").strip()
        if search:
            meetings = meetings.filter(Q(title__icontains=search) | Q(description__icontains=search))
            context["search_query"] = search

        # Order
        if time_filter == "upcoming":
            meetings = meetings.order_by("start")
        else:
            meetings = meetings.order_by("-start")

        # Annotate
        meetings = meetings.annotate(
            attendee_count=Count("attendances", filter=Q(attendances__status__in=["confirmed", "present"]))
        )

        # Pagination
        paginator = Paginator(meetings, 15)
        page = self.request.GET.get("page", 1)
        context["meetings"] = paginator.get_page(page)

        # Statistics
        all_meetings = FactionMeeting.objects.filter(organization=self.organization)
        context["stats"] = {
            "total": all_meetings.count(),
            "upcoming": all_meetings.filter(start__gte=now).count(),
            "pending_protocol": all_meetings.filter(status="completed", protocol_approved=False).count(),
        }

        context["status_choices"] = FactionMeeting.STATUS_CHOICES

        return context

    def post(self, request, *args, **kwargs):
        """Create a new meeting from the modal form."""
        if not self.membership.has_permission("faction.create"):
            messages.error(request, "Keine Berechtigung zum Erstellen von Sitzungen.")
            return redirect("work:faction", org_slug=self.organization.slug)

        # Combine date and time
        start_date = request.POST.get("start_date")
        start_time = request.POST.get("start_time", "18:00")

        if not start_date:
            messages.error(request, "Datum ist erforderlich.")
            return redirect("work:faction", org_slug=self.organization.slug)

        title = request.POST.get("title", "").strip()
        if not title:
            messages.error(request, "Titel ist erforderlich.")
            return redirect("work:faction", org_slug=self.organization.slug)

        try:
            start_datetime = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
            start_datetime = timezone.make_aware(start_datetime) if timezone.is_naive(start_datetime) else start_datetime
        except ValueError:
            messages.error(request, "Ungültiges Datum oder Uhrzeit.")
            return redirect("work:faction", org_slug=self.organization.slug)

        # Create meeting
        meeting = FactionMeeting(
            organization=self.organization,
            created_by=self.membership,
            title=title,
            start=start_datetime,
            location=request.POST.get("location", ""),
            is_virtual=request.POST.get("is_virtual") == "on",
            video_link=request.POST.get("video_link", "") if request.POST.get("is_virtual") == "on" else "",
            description=request.POST.get("description", ""),
            status="draft" if request.POST.get("save_as") == "draft" else "planned",
            meeting_number=FactionMeeting.get_next_meeting_number(self.organization),
        )

        # Find and link previous meeting
        previous = FactionMeeting.find_previous_meeting(self.organization, before_date=meeting.start)
        meeting.previous_meeting = previous
        meeting.save()

        # Create attendance records for all active members
        for member in self.organization.memberships.filter(is_active=True):
            FactionAttendance.objects.create(meeting=meeting, membership=member, status="invited")

        # Auto-create approval agenda item if enabled
        faction_settings = meeting.get_faction_settings()
        if faction_settings.get("auto_create_approval_item", True):
            meeting.create_approval_agenda_item()

        messages.success(request, "Sitzung erfolgreich erstellt.")
        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)


# ---------------------------------------------------------------------------
# 2. Detail (= Protocol page)
# ---------------------------------------------------------------------------

class FactionMeetingDetailView(WorkViewMixin, TemplateView):
    """Combined detail + protocol page."""

    template_name = "work/faction/detail.html"
    permission_required = "faction.view_public"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "faction"

        meeting = get_object_or_404(
            FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization
        )

        context.update(_get_meeting_context(self, meeting))
        return context


# ---------------------------------------------------------------------------
# 3. Central Action Handler
# ---------------------------------------------------------------------------

class FactionActionView(WorkViewMixin, View):
    """Central HTMX action handler for all meeting interactions."""

    permission_required = "faction.view_public"

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(
            FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization
        )

        action = request.POST.get("action")
        is_htmx = request.headers.get("HX-Request")

        handlers = {
            # Status
            "start": self._start,
            "end": self._end,
            "cancel": self._cancel,
            "delete": self._delete,
            "update_status": self._update_status,
            # Meeting
            "update": self._update_meeting,
            "invite": self._invite,
            # Agenda
            "add_item": self._add_item,
            "edit_item": self._edit_item,
            "delete_item": self._delete_item,
            "move_item": self._move_item,
            # Protocol
            "add_entry": self._add_entry,
            "edit_entry": self._edit_entry,
            "delete_entry": self._delete_entry,
            "record_decision": self._record_decision,
            "approve_protocol": self._approve_protocol,
            # Attendance
            "respond": self._respond,
            "check_in": self._check_in,
            "check_out": self._check_out,
            "add_attendee": self._add_attendee,
            # Proposals
            "propose": self._propose,
            "accept_proposal": self._accept_proposal,
            "reject_proposal": self._reject_proposal,
            # Tasks
            "create_task": self._create_task,
        }

        handler = handlers.get(action)
        if handler:
            return handler(request, meeting)

        if is_htmx:
            return HttpResponse(status=400)
        messages.error(request, "Ungültige Aktion.")
        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

    # -- Render helpers ------------------------------------------------

    def _render_agenda(self, request, meeting):
        try:
            ctx = _get_meeting_context(self, meeting)
            html = _render_partial("work/faction/_agenda.html", ctx, request=request)
            return html
        except Exception:
            logger.exception("Fehler beim Rendern der Agenda für Meeting %s", meeting.id)
            raise

    def _render_sidebar(self, request, meeting):
        try:
            ctx = _get_meeting_context(self, meeting)
            html = _render_partial("work/faction/_sidebar.html", ctx, request=request)
            return html
        except Exception:
            logger.exception("Fehler beim Rendern der Sidebar für Meeting %s", meeting.id)
            raise

    def _render_attendance(self, request, meeting):
        try:
            ctx = _get_meeting_context(self, meeting)
            html = _render_partial("work/faction/_attendance_list.html", ctx, request=request)
            return html
        except Exception:
            logger.exception("Fehler beim Rendern der Attendance-Liste für Meeting %s", meeting.id)
            raise

    def _redirect_detail(self, meeting):
        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

    def _refresh_or_redirect(self, request, meeting, msg=None):
        """Return HX-Refresh for HTMX requests, redirect otherwise."""
        if msg:
            messages.success(request, msg)
        if request.headers.get("HX-Request"):
            resp = HttpResponse(status=200)
            resp["HX-Refresh"] = "true"
            return resp
        return self._redirect_detail(meeting)

    # -- Status handlers -----------------------------------------------

    def _start(self, request, meeting):
        if not self.membership.has_permission("faction.start"):
            messages.error(request, "Keine Berechtigung zum Starten.")
            return self._redirect_detail(meeting)
        if meeting.status in ["planned", "invited"]:
            meeting.status = "ongoing"
            meeting.save()
        return self._refresh_or_redirect(request, meeting, "Sitzung gestartet.")

    def _end(self, request, meeting):
        if meeting.status == "ongoing":
            meeting.status = "completed"
            meeting.end = timezone.now()
            meeting.save()
        return self._refresh_or_redirect(request, meeting, "Sitzung beendet.")

    def _cancel(self, request, meeting):
        if meeting.status not in ["completed", "cancelled"]:
            meeting.status = "cancelled"
            meeting.save()
        return self._refresh_or_redirect(request, meeting, "Sitzung abgesagt.")

    def _delete(self, request, meeting):
        can_delete = meeting.created_by == self.membership or self.membership.has_permission("faction.manage")
        if not can_delete:
            messages.error(request, "Keine Berechtigung zum Löschen.")
            return self._redirect_detail(meeting)

        title = meeting.title
        meeting.delete()
        messages.success(request, f"Sitzung '{title}' wurde gelöscht.")
        return redirect("work:faction", org_slug=self.organization.slug)

    def _update_status(self, request, meeting):
        new_status = request.POST.get("status")
        if new_status and new_status in dict(FactionMeeting.STATUS_CHOICES):
            meeting.status = new_status
            meeting.save()
        return self._refresh_or_redirect(request, meeting, "Status geändert.")

    # -- Meeting update ------------------------------------------------

    def _update_meeting(self, request, meeting):
        can_edit = (
            meeting.created_by == self.membership or self.membership.has_permission("faction.manage")
        ) and meeting.status in ["draft", "planned", "invited", "ongoing"]

        if not can_edit:
            messages.error(request, "Keine Berechtigung zum Bearbeiten.")
            return self._redirect_detail(meeting)

        meeting.title = request.POST.get("title", meeting.title)
        meeting.description = request.POST.get("description", "")

        start_date = request.POST.get("start_date")
        start_time = request.POST.get("start_time", "18:00")
        if start_date:
            try:
                start_dt = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
                meeting.start = timezone.make_aware(start_dt) if timezone.is_naive(start_dt) else start_dt
            except ValueError:
                pass

        meeting.location = request.POST.get("location", "")
        meeting.is_virtual = request.POST.get("is_virtual") == "on"
        meeting.video_link = request.POST.get("video_link", "") if meeting.is_virtual else ""

        new_status = request.POST.get("status")
        if new_status and new_status in dict(FactionMeeting.STATUS_CHOICES):
            meeting.status = new_status

        meeting.save()
        return self._refresh_or_redirect(request, meeting, "Änderungen gespeichert.")

    def _invite(self, request, meeting):
        if not self.membership.has_permission("faction.invite"):
            messages.error(request, "Keine Berechtigung zum Einladen.")
            return self._redirect_detail(meeting)

        if meeting.invitation_sent:
            messages.warning(request, "Einladungen wurden bereits versendet.")
        else:
            from .services import FactionMeetingEmailService
            email_service = FactionMeetingEmailService()
            sent_count = email_service.send_invitations(meeting)

            meeting.invitation_sent = True
            meeting.invitation_sent_at = timezone.now()
            meeting.status = "invited"
            meeting.save()

            if sent_count > 0:
                messages.success(request, f"Einladungen an {sent_count} Mitglieder versendet.")
            else:
                messages.warning(request, "Einladungsstatus aktualisiert. Keine E-Mails versendet.")

        return self._refresh_or_redirect(request, meeting)

    # -- Agenda handlers -----------------------------------------------

    def _add_item(self, request, meeting):
        if not self.membership.has_permission("faction.manage"):
            return HttpResponse(status=403)

        title = request.POST.get("title", "").strip()
        visibility = request.POST.get("visibility", "public")
        parent_id = request.POST.get("parent_id", "").strip()

        if not title:
            if request.headers.get("HX-Request"):
                return HttpResponse("Titel ist erforderlich.", status=400)
            messages.error(request, "Titel ist erforderlich.")
            return self._redirect_detail(meeting)

        parent = None
        if parent_id:
            parent = get_object_or_404(FactionAgendaItem, id=parent_id, meeting=meeting)
            visibility = parent.visibility

        # Auto-generate number
        if parent:
            child_count = parent.children.count() + 1
            number = f"{parent.number}.{child_count}"
        else:
            existing = meeting.agenda_items.filter(
                visibility=visibility, parent__isnull=True
            ).exclude(is_approval_item=True)
            next_num = existing.count() + 1

            if visibility == "public" and meeting.agenda_items.filter(is_approval_item=True).exists():
                next_num += 1

            number = f"NÖ {next_num}" if visibility == "internal" else str(next_num)

        item = FactionAgendaItem(
            meeting=meeting,
            title=title,
            number=number,
            visibility=visibility,
            order=meeting.agenda_items.count() + 1,
            parent=parent,
        )
        item.save()

        description = request.POST.get("description", "").strip()
        if description:
            item.set_description_encrypted(description)
            item.save()

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        messages.success(request, f"TOP {item.number} hinzugefügt.")
        return self._redirect_detail(meeting)

    def _edit_item(self, request, meeting):
        if not self.membership.has_permission("faction.manage"):
            return HttpResponse(status=403)

        item_id = request.POST.get("item_id")
        title = request.POST.get("title", "").strip()

        if not title:
            if request.headers.get("HX-Request"):
                return HttpResponse("Titel ist erforderlich.", status=400)
            messages.error(request, "Titel ist erforderlich.")
            return self._redirect_detail(meeting)

        item = get_object_or_404(FactionAgendaItem, id=item_id, meeting=meeting)
        item.title = title

        description = request.POST.get("description", "").strip()
        if description:
            item.set_description_encrypted(description)
        else:
            item.description_encrypted = None

        item.save()

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        messages.success(request, f"TOP {item.number} aktualisiert.")
        return self._redirect_detail(meeting)

    def _delete_item(self, request, meeting):
        if not self.membership.has_permission("faction.manage"):
            return HttpResponse(status=403)

        item_id = request.POST.get("item_id")
        item = FactionAgendaItem.objects.filter(
            id=item_id, meeting=meeting, is_approval_item=False
        ).first()

        if item:
            item.delete()
            _renumber_items(meeting, item.visibility if item else "public")

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        messages.success(request, "TOP gelöscht.")
        return self._redirect_detail(meeting)

    def _move_item(self, request, meeting):
        if not self.membership.has_permission("faction.manage"):
            return HttpResponse(status=403)

        item_id = request.POST.get("item_id")
        direction = request.POST.get("direction")

        item = FactionAgendaItem.objects.filter(
            id=item_id, meeting=meeting, is_approval_item=False, parent__isnull=True
        ).first()

        if item and direction in ("up", "down"):
            siblings = list(
                meeting.agenda_items.filter(
                    visibility=item.visibility, parent__isnull=True, is_approval_item=False
                ).order_by("order")
            )

            current_index = None
            for i, s in enumerate(siblings):
                if s.id == item.id:
                    current_index = i
                    break

            if current_index is not None:
                swap_target = None
                if direction == "up" and current_index > 0:
                    swap_target = siblings[current_index - 1]
                elif direction == "down" and current_index < len(siblings) - 1:
                    swap_target = siblings[current_index + 1]

                if swap_target:
                    item.order, swap_target.order = swap_target.order, item.order
                    item.save()
                    swap_target.save()
                    _renumber_items(meeting, item.visibility)

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        return self._redirect_detail(meeting)

    # -- Protocol handlers ---------------------------------------------

    def _add_entry(self, request, meeting):
        entry_type = request.POST.get("entry_type", "note")
        content = request.POST.get("content", "").strip()
        agenda_item_id = request.POST.get("agenda_item_id")

        if not content:
            if request.headers.get("HX-Request"):
                return HttpResponse("Inhalt ist erforderlich.", status=400)
            return self._redirect_detail(meeting)

        entry = FactionProtocolEntry(
            meeting=meeting,
            entry_type=entry_type,
            created_by=self.membership,
            order=meeting.protocol_entries.count() + 1,
        )

        if agenda_item_id:
            entry.agenda_item_id = agenda_item_id

        if entry_type == "speech":
            speaker_id = request.POST.get("speaker")
            if speaker_id:
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

        # If decision, update agenda item
        if entry_type == "decision" and agenda_item_id:
            try:
                votes_yes = int(request.POST.get("votes_yes", 0))
                votes_no = int(request.POST.get("votes_no", 0))
                votes_abstain = int(request.POST.get("votes_abstain", 0))

                agenda_item = FactionAgendaItem.objects.get(id=agenda_item_id)
                agenda_item.has_decision = True
                agenda_item.votes_for = votes_yes
                agenda_item.votes_against = votes_no
                agenda_item.votes_abstain = votes_abstain
                agenda_item.save()
            except (ValueError, FactionAgendaItem.DoesNotExist):
                pass

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        messages.success(request, "Protokolleintrag gespeichert.")
        return self._redirect_detail(meeting)

    def _edit_entry(self, request, meeting):
        entry_id = request.POST.get("entry_id")
        content = request.POST.get("content", "").strip()

        if not entry_id or not content:
            return HttpResponse(status=400)

        entry = get_object_or_404(FactionProtocolEntry, id=entry_id, meeting=meeting)
        entry.set_content_encrypted(content)

        entry_type = request.POST.get("entry_type")
        if entry_type:
            entry.entry_type = entry_type

        speaker_id = request.POST.get("speaker")
        if speaker_id:
            entry.speaker_id = speaker_id

        entry.save()

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        return self._redirect_detail(meeting)

    def _delete_entry(self, request, meeting):
        entry_id = request.POST.get("entry_id")
        entry = FactionProtocolEntry.objects.filter(id=entry_id, meeting=meeting).first()
        if entry:
            entry.delete()

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        return self._redirect_detail(meeting)

    def _record_decision(self, request, meeting):
        agenda_item_id = request.POST.get("agenda_item_id")
        agenda_item = get_object_or_404(FactionAgendaItem, id=agenda_item_id, meeting=meeting)

        try:
            votes_yes = int(request.POST.get("votes_yes", 0))
            votes_no = int(request.POST.get("votes_no", 0))
            votes_abstain = int(request.POST.get("votes_abstain", 0))
        except ValueError:
            return HttpResponse("Ungültige Stimmzahlen.", status=400)

        result = request.POST.get("result", "accepted")
        decision_text = request.POST.get("decision_text", "").strip()
        notes = request.POST.get("notes", "").strip()

        # Create or update decision
        decision, created = FactionDecision.objects.update_or_create(
            agenda_item=agenda_item,
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

        # Update agenda item
        agenda_item.has_decision = True
        agenda_item.votes_for = votes_yes
        agenda_item.votes_against = votes_no
        agenda_item.votes_abstain = votes_abstain
        agenda_item.save()

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        messages.success(request, "Abstimmung erfasst.")
        return self._redirect_detail(meeting)

    def _approve_protocol(self, request, meeting):
        if meeting.status == "completed" and not meeting.protocol_approved:
            meeting.protocol_approved = True
            meeting.protocol_approved_at = timezone.now()
            meeting.protocol_approved_by = self.membership
            meeting.save()

        return self._refresh_or_redirect(request, meeting, "Protokoll genehmigt.")

    # -- Attendance handlers -------------------------------------------

    def _respond(self, request, meeting):
        try:
            attendance = meeting.attendances.get(membership=self.membership)
        except FactionAttendance.DoesNotExist:
            if request.headers.get("HX-Request"):
                return HttpResponse('<p class="text-red-600 text-sm">Keine Einladung gefunden</p>')
            return self._redirect_detail(meeting)

        new_status = request.POST.get("status")
        if new_status in ["confirmed", "declined", "tentative"]:
            attendance.status = new_status
            attendance.response_message = request.POST.get("response_message", "")
            attendance.responded_at = timezone.now()
            attendance.save()

            if request.headers.get("HX-Request"):
                ctx = _get_meeting_context(self, meeting)
                html = _render_partial("work/faction/_sidebar.html", ctx, request=request)
                return _htmx_response(html)

        return self._redirect_detail(meeting)

    def _check_in(self, request, meeting):
        member_id = request.POST.get("member_id")
        try:
            attendance = meeting.attendances.get(membership_id=member_id)
            attendance.status = "present"
            attendance.checked_in_at = timezone.now()
            attendance.save()
        except FactionAttendance.DoesNotExist:
            pass

        if request.headers.get("HX-Request"):
            html = self._render_attendance(request, meeting)
            return _htmx_response(html)

        return self._redirect_detail(meeting)

    def _check_out(self, request, meeting):
        member_id = request.POST.get("member_id")
        try:
            attendance = meeting.attendances.get(membership_id=member_id)
            attendance.checked_out_at = timezone.now()
            attendance.save()
        except FactionAttendance.DoesNotExist:
            pass

        if request.headers.get("HX-Request"):
            html = self._render_attendance(request, meeting)
            return _htmx_response(html)

        return self._redirect_detail(meeting)

    def _add_attendee(self, request, meeting):
        if not self.membership.has_permission("faction.manage"):
            return HttpResponse(status=403)

        attendee_type = request.POST.get("attendee_type")
        status = request.POST.get("status", "present")

        if attendee_type == "guest":
            guest_name = request.POST.get("guest_name", "").strip()
            if not guest_name:
                messages.error(request, "Bitte einen Namen für den Gast angeben.")
                return self._redirect_detail(meeting)

            attendance = FactionAttendance.objects.create(
                meeting=meeting, is_guest=True, guest_name=guest_name, status=status,
            )
            if status == "present":
                attendance.checked_in_at = timezone.now()
                attendance.save()

        elif attendee_type == "member":
            membership_id = request.POST.get("membership_id")
            if not membership_id:
                messages.error(request, "Bitte ein Mitglied auswählen.")
                return self._redirect_detail(meeting)

            from apps.tenants.models import Membership
            membership = get_object_or_404(Membership, id=membership_id, organization=self.organization)

            if FactionAttendance.objects.filter(meeting=meeting, membership=membership).exists():
                messages.warning(request, f"{membership.user.get_display_name()} ist bereits in der Teilnehmerliste.")
                return self._redirect_detail(meeting)

            attendance = FactionAttendance.objects.create(
                meeting=meeting, membership=membership, is_guest=False, status=status,
            )
            if status == "present":
                attendance.checked_in_at = timezone.now()
                attendance.save()

        if request.headers.get("HX-Request"):
            html = self._render_attendance(request, meeting)
            return _htmx_response(html)

        return self._refresh_or_redirect(request, meeting, "Teilnehmer hinzugefügt.")

    # -- Proposal handlers ---------------------------------------------

    def _propose(self, request, meeting):
        title = request.POST.get("title", "").strip()
        description = request.POST.get("description", "").strip()
        visibility = request.POST.get("visibility", "public")

        if not title:
            messages.error(request, "Bitte einen Titel angeben.")
            return self._redirect_detail(meeting)

        from .services import AgendaProposalService
        AgendaProposalService.create_proposal(
            meeting=meeting, title=title, description=description,
            proposed_by=self.membership, visibility=visibility,
        )

        messages.success(request, f"Vorschlag '{title}' eingereicht.")
        return self._refresh_or_redirect(request, meeting)

    def _accept_proposal(self, request, meeting):
        if not self.membership.has_permission("agenda.manage"):
            messages.error(request, "Keine Berechtigung zum Annehmen von Vorschlägen.")
            return self._redirect_detail(meeting)

        item_id = request.POST.get("item_id")
        item = get_object_or_404(FactionAgendaItem, id=item_id, meeting=meeting, proposal_status="proposed")

        from .services import AgendaProposalService
        assign_number = request.POST.get("number", "").strip()
        AgendaProposalService.accept_proposal(item, self.membership, assign_number or None)

        messages.success(request, f"Vorschlag '{item.title}' angenommen.")
        return self._refresh_or_redirect(request, meeting)

    def _reject_proposal(self, request, meeting):
        if not self.membership.has_permission("agenda.manage"):
            messages.error(request, "Keine Berechtigung zum Ablehnen von Vorschlägen.")
            return self._redirect_detail(meeting)

        item_id = request.POST.get("item_id")
        reason = request.POST.get("reason", "").strip()
        item = get_object_or_404(FactionAgendaItem, id=item_id, meeting=meeting, proposal_status="proposed")

        from .services import AgendaProposalService
        AgendaProposalService.reject_proposal(item, self.membership, reason)

        messages.success(request, f"Vorschlag '{item.title}' abgelehnt.")
        return self._refresh_or_redirect(request, meeting)

    # -- Task handler --------------------------------------------------

    def _create_task(self, request, meeting):
        entry_id = request.POST.get("entry_id")
        entry = get_object_or_404(FactionProtocolEntry, id=entry_id, meeting=meeting)

        if entry.entry_type != "action":
            messages.error(request, "Nur Aufgaben-Einträge können ins Task-Board übernommen werden.")
            return self._redirect_detail(meeting)

        from apps.work.tasks.models import Task

        content = entry.get_content_decrypted() or ""
        task_title = content[:200] if content else f"Aufgabe aus Fraktionssitzung {meeting.title}"

        description_parts = [f"Aus Fraktionssitzung: {meeting.title}"]
        if entry.agenda_item:
            description_parts.append(f"TOP: {entry.agenda_item.title}")
        else:
            description_parts.append("TOP: Allgemein")

        Task.objects.create(
            organization=self.organization,
            title=task_title,
            description="\n".join(description_parts),
            assigned_to=entry.action_assignee,
            due_date=entry.action_due_date,
            created_by=self.membership,
            related_faction_meeting=meeting,
        )

        entry.action_completed = True
        entry.save()

        short_title = task_title[:50] + "..." if len(task_title) > 50 else task_title
        messages.success(request, f"Aufgabe '{short_title}' erstellt.")
        return self._redirect_detail(meeting)


# ---------------------------------------------------------------------------
# 4. Agenda Item Panel (Slide-Over)
# ---------------------------------------------------------------------------

class FactionItemPanelView(WorkViewMixin, TemplateView):
    """GET: Render agenda item slide-over panel content."""

    template_name = "work/faction/_agenda_item_panel.html"
    permission_required = "faction.view_public"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meeting = get_object_or_404(
            FactionMeeting, id=kwargs["meeting_id"], organization=self.organization
        )
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
            meeting.created_by == self.membership
            or self.membership.has_permission("faction.manage")
        ) and meeting.status in ["draft", "planned", "invited", "ongoing"]

        is_protocol_phase = meeting.status in ["ongoing", "completed"] and not meeting.protocol_approved

        # Attendances for speaker select
        attendances = meeting.attendances.select_related("membership__user")

        # Available motions for linking
        from apps.work.motions.models import Motion
        available_motions = Motion.objects.filter(
            organization=self.organization
        ).exclude(
            id__in=item.related_motions.values_list("id", flat=True)
        ).order_by("-created_at")[:50]

        # Available members for task assignment
        from apps.tenants.models import Membership
        available_members = (
            Membership.objects.filter(organization=self.organization, is_active=True)
            .select_related("user")
            .order_by("user__last_name", "user__first_name")
        )

        context.update({
            "item": item,
            "meeting": meeting,
            "decision": decision,
            "can_edit": can_edit,
            "is_protocol_phase": is_protocol_phase,
            "attendances": attendances,
            "protocol_entries": item.protocol_entries.select_related(
                "speaker__user", "created_by__user"
            ).order_by("order", "created_at"),
            "attachments": item.attachments.select_related("uploaded_by__user").order_by("-created_at"),
            "tasks": item.tasks.select_related("assigned_to__user", "created_by__user"),
            "linked_motions": item.related_motions.all(),
            "available_motions": available_motions,
            "available_members": available_members,
            "reference_links": item.reference_links or [],
            "organization": self.organization,
            "org_slug": self.organization.slug,
            "membership": self.membership,
        })
        return context


class FactionItemPanelActionView(WorkViewMixin, View):
    """Central POST handler for agenda item panel actions."""

    permission_required = "faction.view_public"

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(
            FactionMeeting, id=kwargs["meeting_id"], organization=self.organization
        )
        item = get_object_or_404(
            FactionAgendaItem, id=kwargs["item_id"], meeting=meeting
        )

        action = request.POST.get("action")
        can_edit = (
            meeting.created_by == self.membership
            or self.membership.has_permission("faction.manage")
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
            "create_task": self._create_task,
            "add_link": self._add_link,
            "remove_link": self._remove_link,
        }

        handler = handlers.get(action)
        if not handler:
            return HttpResponse(status=400)

        return handler(request, meeting, item, can_edit)

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
        if message:
            response["HX-Trigger"] = json.dumps({"show-toast": {"message": message, "type": "success"}})
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
        if message:
            response["HX-Trigger"] = json.dumps({"panel-autosaved": True})
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
        entry_type = request.POST.get("entry_type", "note")
        content = request.POST.get("content", "").strip()

        if not content:
            return HttpResponse("Inhalt ist erforderlich.", status=400)

        entry = FactionProtocolEntry(
            meeting=meeting,
            agenda_item=item,
            entry_type=entry_type,
            created_by=self.membership,
            order=item.protocol_entries.count() + 1,
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
        if not can_edit:
            return HttpResponse(status=403)

        entry_id = request.POST.get("entry_id")
        FactionProtocolEntry.objects.filter(id=entry_id, meeting=meeting, agenda_item=item).delete()
        return self._panel_response(request, meeting, item, "Eintrag gelöscht")

    def _record_decision(self, request, meeting, item, can_edit):
        """Record or update a decision/vote."""
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
        if not can_edit:
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
        attachment = FactionAgendaItemAttachment.objects.filter(
            id=attachment_id, agenda_item=item
        ).first()
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


# ---------------------------------------------------------------------------
# 5. Settings (legacy redirect)
# ---------------------------------------------------------------------------

class FactionSettingsView(WorkViewMixin, View):
    """Legacy redirect - settings are now in organization settings."""

    permission_required = "faction.manage"

    def get(self, request, *args, **kwargs):
        return redirect("work:organization_faction_settings", org_slug=self.organization.slug)

    def post(self, request, *args, **kwargs):
        return redirect("work:organization_faction_settings", org_slug=self.organization.slug)

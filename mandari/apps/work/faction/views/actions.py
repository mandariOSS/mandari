# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Faction meeting views for the Work module.

Simplified architecture: 4 views instead of 13.
- FactionMeetingListView: List + Create (POST)
- FactionMeetingDetailView: Detail/Protocol page
- FactionActionView: Central HTMX action handler
- FactionSettingsView: Legacy redirect to organization settings
"""

import logging
from datetime import datetime

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import View

from apps.common.mixins import WorkViewMixin

from ..models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionDecision,
    FactionMeeting,
    FactionProtocolEntry,
)

logger = logging.getLogger(__name__)
from ._helpers import _get_meeting_context, _htmx_response, _render_partial, _renumber_items


class FactionActionView(WorkViewMixin, View):
    """Central HTMX action handler for all meeting interactions."""

    permission_required = "faction.view_public"

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

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
            # Partial refresh (z. B. nach Panel-Aktionen)
            "refresh_agenda": self._refresh_agenda_action,
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

    def _refresh_agenda_action(self, request, meeting):
        """Agenda-Liste neu rendern (getriggert nach Panel-Aktionen, damit Badges aktuell bleiben)."""
        html = self._render_agenda(request, meeting)
        return _htmx_response(html)

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

    # -- Permission helpers ----------------------------------------------

    def _can_manage_agenda(self, meeting):
        """Ersteller der Sitzung oder faction.manage — entspricht can_edit in der UI."""
        return meeting.created_by == self.membership or self.membership.has_permission("faction.manage")

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

    def _can_manage_meeting(self, meeting):
        """Ersteller der Sitzung oder faction.manage — für Status-/Verwaltungsaktionen."""
        return meeting.created_by == self.membership or self.membership.has_permission("faction.manage")

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
        if not self.membership.has_permission("faction.start"):
            messages.error(request, "Keine Berechtigung zum Beenden.")
            return self._redirect_detail(meeting)
        if meeting.status == "ongoing":
            meeting.status = "completed"
            meeting.end = timezone.now()
            meeting.save()
        return self._refresh_or_redirect(request, meeting, "Sitzung beendet.")

    def _cancel(self, request, meeting):
        if not self._can_manage_meeting(meeting):
            messages.error(request, "Keine Berechtigung zum Absagen.")
            return self._redirect_detail(meeting)
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
        if not self._can_manage_meeting(meeting):
            messages.error(request, "Keine Berechtigung zum Ändern des Status.")
            return self._redirect_detail(meeting)
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
            from ..services import FactionMeetingEmailService

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
        if not self._can_manage_agenda(meeting) and not self.membership.has_permission("agenda.create"):
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
            existing = meeting.agenda_items.filter(visibility=visibility, parent__isnull=True).exclude(
                is_approval_item=True
            )
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
        if not self._can_manage_agenda(meeting):
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
        if not self._can_manage_agenda(meeting):
            return HttpResponse(status=403)

        item_id = request.POST.get("item_id")
        item = FactionAgendaItem.objects.filter(id=item_id, meeting=meeting, is_approval_item=False).first()

        if item:
            item.delete()
            _renumber_items(meeting, item.visibility if item else "public")

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        messages.success(request, "TOP gelöscht.")
        return self._redirect_detail(meeting)

    def _move_item(self, request, meeting):
        if not self._can_manage_agenda(meeting):
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
        if not self._can_protocol(meeting):
            return HttpResponse(status=403)

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

        # TOP nur akzeptieren, wenn er zu DIESER Sitzung gehört - sonst könnte
        # ein Protokollant per fremder agenda_item_id einen Eintrag an einen TOP
        # einer anderen (auch org-fremden) Sitzung hängen bzw. dessen
        # Abstimmungsdaten überschreiben (IDOR).
        agenda_item = None
        if agenda_item_id:
            agenda_item = FactionAgendaItem.objects.filter(id=agenda_item_id, meeting=meeting).first()
            if agenda_item is not None:
                entry.agenda_item = agenda_item

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

        # If decision, update agenda item (nur der zu dieser Sitzung gehörende TOP)
        if entry_type == "decision" and agenda_item is not None:
            try:
                votes_yes = int(request.POST.get("votes_yes", 0))
                votes_no = int(request.POST.get("votes_no", 0))
                votes_abstain = int(request.POST.get("votes_abstain", 0))

                agenda_item.has_decision = True
                agenda_item.votes_for = votes_yes
                agenda_item.votes_against = votes_no
                agenda_item.votes_abstain = votes_abstain
                agenda_item.save()
            except ValueError:
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
        is_own = entry.created_by == self.membership and not meeting.protocol_approved
        if not self._can_protocol(meeting) and not is_own:
            return HttpResponse(status=403)
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
            is_own = entry.created_by == self.membership and not meeting.protocol_approved
            if not self._can_protocol(meeting) and not is_own:
                return HttpResponse(status=403)
            entry.delete()

        if request.headers.get("HX-Request"):
            html = self._render_agenda(request, meeting)
            return _htmx_response(html)

        return self._redirect_detail(meeting)

    def _record_decision(self, request, meeting):
        if not self._can_protocol(meeting):
            return HttpResponse(status=403)

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
        if not self.membership.has_permission("protocols.approve"):
            messages.error(request, "Keine Berechtigung zur Protokollgenehmigung.")
            return self._redirect_detail(meeting)
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

    def _get_attendance(self, request, meeting):
        """Attendance über attendance_id (auch Gäste) oder member_id (Legacy) auflösen."""
        attendance_id = request.POST.get("attendance_id")
        if attendance_id:
            return meeting.attendances.filter(id=attendance_id).first()
        member_id = request.POST.get("member_id")
        if member_id:
            return meeting.attendances.filter(membership_id=member_id).first()
        return None

    def _check_in(self, request, meeting):
        if not self.membership.has_permission("faction.manage"):
            return HttpResponse(status=403)

        attendance = self._get_attendance(request, meeting)
        if attendance:
            attendance.status = "present"
            attendance.checked_in_at = timezone.now()
            attendance.save()

        if request.headers.get("HX-Request"):
            html = self._render_attendance(request, meeting)
            return _htmx_response(html)

        return self._redirect_detail(meeting)

    def _check_out(self, request, meeting):
        if not self.membership.has_permission("faction.manage"):
            return HttpResponse(status=403)

        attendance = self._get_attendance(request, meeting)
        if attendance:
            attendance.checked_out_at = timezone.now()
            attendance.save()

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
                meeting=meeting,
                is_guest=True,
                guest_name=guest_name,
                status=status,
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
                meeting=meeting,
                membership=membership,
                is_guest=False,
                status=status,
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
        # Serverseitige Prüfung — die UI blendet den Vorschlags-Dialog nur für
        # Berechtigte ein, das ersetzt aber keine Prüfung im Handler.
        if not self.membership.has_permission("agenda.propose") and not self.membership.has_permission("agenda.create"):
            messages.error(request, "Keine Berechtigung zum Einreichen von Vorschlägen.")
            return self._redirect_detail(meeting)

        title = request.POST.get("title", "").strip()
        description = request.POST.get("description", "").strip()
        visibility = request.POST.get("visibility", "public")

        if not title:
            messages.error(request, "Bitte einen Titel angeben.")
            return self._redirect_detail(meeting)

        from ..services import AgendaProposalService

        AgendaProposalService.create_proposal(
            meeting=meeting,
            title=title,
            description=description,
            proposed_by=self.membership,
            visibility=visibility,
        )

        messages.success(request, f"Vorschlag '{title}' eingereicht.")
        return self._refresh_or_redirect(request, meeting)

    def _accept_proposal(self, request, meeting):
        if not self.membership.has_permission("agenda.manage"):
            messages.error(request, "Keine Berechtigung zum Annehmen von Vorschlägen.")
            return self._redirect_detail(meeting)

        item_id = request.POST.get("item_id")
        item = get_object_or_404(FactionAgendaItem, id=item_id, meeting=meeting, proposal_status="proposed")

        from ..services import AgendaProposalService

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

        from ..services import AgendaProposalService

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

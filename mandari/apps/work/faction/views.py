# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Faction meeting views for the Work module.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from .forms import (
    FactionAttendanceResponseForm,
    FactionDecisionForm,
    FactionMeetingForm,
    FactionProtocolEntryForm,
    FactionScheduleForm,
)
from .models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionMeeting,
    FactionMeetingSchedule,
    FactionProtocolEntry,
)


class FactionMeetingListView(WorkViewMixin, TemplateView):
    """List of faction meetings."""

    template_name = "work/faction/list.html"
    permission_required = "faction.view_public"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "faction"

        # Base queryset
        meetings = FactionMeeting.objects.filter(organization=self.organization).select_related(
            "created_by__user", "schedule"
        )

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

        # Annotate with attendance count
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


class FactionMeetingCreateView(WorkViewMixin, TemplateView):
    """Create a new faction meeting."""

    template_name = "work/faction/create.html"
    permission_required = "faction.create"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "faction"
        context["form"] = FactionMeetingForm(organization=self.organization)
        context["schedules"] = FactionMeetingSchedule.objects.filter(organization=self.organization, is_active=True)
        return context

    def post(self, request, *args, **kwargs):
        from datetime import datetime

        # Combine date and time fields into start datetime
        post_data = request.POST.copy()
        start_date = request.POST.get("start_date")
        start_time = request.POST.get("start_time", "18:00")

        if start_date:
            try:
                start_datetime = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
                post_data["start"] = start_datetime.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                pass

        form = FactionMeetingForm(post_data, organization=self.organization)

        if form.is_valid():
            meeting = form.save(commit=False)
            meeting.organization = self.organization
            meeting.created_by = self.membership
            meeting.status = "draft"

            # Set meeting number
            meeting.meeting_number = FactionMeeting.get_next_meeting_number(self.organization)

            # Find and link previous meeting
            previous = FactionMeeting.find_previous_meeting(self.organization, before_date=meeting.start)
            meeting.previous_meeting = previous

            meeting.save()

            # Create attendance records for all members
            for member in self.organization.memberships.filter(is_active=True):
                FactionAttendance.objects.create(meeting=meeting, membership=member, status="invited")

            # Get faction settings
            faction_settings = meeting.get_faction_settings()
            auto_create_approval = faction_settings.get("auto_create_approval_item", True)

            # Start public count at 1 or 2 depending on whether we auto-create approval item
            public_count = 0
            internal_count = 0

            # Create approval agenda item if enabled
            if auto_create_approval:
                meeting.create_approval_agenda_item()
                public_count = 1  # Approval item is TOP 1

            # Create agenda items from form data (agenda_0, agenda_1, etc.)
            agenda_index = 0
            while True:
                agenda_title = request.POST.get(f"agenda_{agenda_index}", "").strip()
                visibility = request.POST.get(f"agenda_visibility_{agenda_index}", "public")

                if agenda_title:
                    if visibility == "internal":
                        internal_count += 1
                        number = f"NÖ {internal_count}"
                    else:
                        public_count += 1
                        number = str(public_count)

                    FactionAgendaItem.objects.create(
                        meeting=meeting,
                        title=agenda_title,
                        number=number,
                        visibility=visibility,
                        order=public_count + internal_count,  # Order after approval item
                    )
                    agenda_index += 1
                elif agenda_index == 0:
                    # Check if there's any agenda field at all
                    agenda_index += 1
                    continue
                else:
                    break
                if agenda_index > 50:  # Safety limit
                    break

            messages.success(request, "Sitzung erfolgreich erstellt.")
            return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

        context = self.get_context_data()
        context["form"] = form
        return self.render_to_response(context)


class FactionMeetingDetailView(WorkViewMixin, TemplateView):
    """Detail view of a faction meeting."""

    template_name = "work/faction/detail.html"
    permission_required = "faction.view_public"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "faction"

        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        context["meeting"] = meeting
        context["is_creator"] = meeting.created_by == self.membership

        # Agenda items - only top-level (parent=None), children are accessed via item.children.all
        agenda_items = (
            meeting.agenda_items.filter(parent__isnull=True)
            .select_related("related_agenda_item", "approves_meeting")
            .prefetch_related(
                "protocol_entries",
                "children",  # Prefetch sub-items
            )
            .order_by("order", "number")
        )

        context["agenda_items"] = agenda_items
        context["public_agenda_items"] = [i for i in agenda_items if i.visibility == "public"]

        # Check if user can view non-public content (requires permission + sworn-in)
        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        can_view_internal = checker.can_access_non_public()
        context["can_view_internal"] = can_view_internal

        # Only pass internal items if user is allowed to see them
        if can_view_internal:
            context["internal_agenda_items"] = [i for i in agenda_items if i.visibility == "internal"]
        else:
            context["internal_agenda_items"] = []

        # Attendance (uses model's default ordering: is_guest, membership__user__last_name, guest_name)
        context["attendances"] = meeting.attendances.select_related("membership__user")

        # Current user's attendance
        try:
            context["my_attendance"] = meeting.attendances.get(membership=self.membership)
        except FactionAttendance.DoesNotExist:
            context["my_attendance"] = None

        # Attendance statistics
        context["attendance_stats"] = {
            "confirmed": meeting.attendances.filter(status="confirmed").count(),
            "declined": meeting.attendances.filter(status="declined").count(),
            "tentative": meeting.attendances.filter(status="tentative").count(),
            "pending": meeting.attendances.filter(status="invited").count(),
            "present": meeting.attendances.filter(status="present").count(),
            "absent": meeting.attendances.filter(status="absent").count(),
            "excused": meeting.attendances.filter(status="excused").count(),
        }

        # Available members for adding to meeting (those not already attending)
        existing_member_ids = meeting.attendances.filter(membership__isnull=False).values_list(
            "membership_id", flat=True
        )

        from apps.tenants.models import Membership

        context["available_members"] = (
            Membership.objects.filter(organization=self.organization, is_active=True)
            .exclude(id__in=existing_member_ids)
            .select_related("user")
            .order_by("user__last_name", "user__first_name")
        )

        # Can edit agenda (more permissive - also during ongoing for some roles)
        context["can_edit"] = (
            meeting.created_by == self.membership or self.membership.has_permission("faction.manage")
        ) and meeting.status in ["draft", "planned", "invited", "ongoing"]

        # Can start meeting (allow starting up to 30 minutes early)
        from datetime import timedelta

        start_allowed_from = meeting.start - timedelta(minutes=30)
        context["can_start"] = (
            self.membership.has_permission("faction.start")
            and meeting.status in ["planned", "invited"]
            and start_allowed_from <= timezone.now()
        )

        # Can manage attendance (users with faction.manage permission)
        context["can_manage_attendance"] = self.membership.has_permission("faction.manage")

        # Can propose agenda items (for Sachkundige Bürger*innen)
        # Only show propose button if user can propose but NOT create directly
        can_propose = checker.can_propose_agenda_items()
        can_create_directly = checker.can_create_agenda_items_directly()
        context["can_propose_agenda"] = can_propose and not can_create_directly
        context["can_approve_proposals"] = checker.can_approve_agenda_items() or self.membership.has_permission(
            "agenda.manage"
        )

        # Pending proposals count (for managers)
        context["pending_proposals"] = meeting.agenda_items.filter(proposal_status="proposed")

        # Protocol entries for live protocol view
        context["protocol_entries"] = meeting.protocol_entries.select_related(
            "agenda_item", "speaker__user", "created_by__user"
        ).order_by("-created_at")[:10]

        context["response_form"] = FactionAttendanceResponseForm()

        # Status choices for inline edit modal
        context["status_choices"] = FactionMeeting.STATUS_CHOICES

        return context

    def post(self, request, *args, **kwargs):
        """Handle actions from the detail page."""
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        action = request.POST.get("action")

        if action == "start_meeting":
            if meeting.status in ["planned", "invited"]:
                meeting.status = "ongoing"
                meeting.save()
                messages.success(request, "Sitzung gestartet.")

        elif action == "end_meeting":
            if meeting.status == "ongoing":
                meeting.status = "completed"
                meeting.end = timezone.now()
                meeting.save()
                messages.success(request, "Sitzung beendet.")

        elif action == "cancel":
            if meeting.status not in ["completed", "cancelled"]:
                meeting.status = "cancelled"
                meeting.save()
                messages.success(request, "Sitzung abgesagt.")

        elif action == "check_in":
            member_id = request.POST.get("member_id")
            try:
                attendance = meeting.attendances.get(membership_id=member_id)
                attendance.status = "present"
                attendance.checked_in_at = timezone.now()
                attendance.save()
            except FactionAttendance.DoesNotExist:
                pass

        elif action == "check_out":
            member_id = request.POST.get("member_id")
            try:
                attendance = meeting.attendances.get(membership_id=member_id)
                attendance.checked_out_at = timezone.now()
                attendance.save()
            except FactionAttendance.DoesNotExist:
                pass

        elif action == "add_entry":
            # Add protocol entry
            entry_type = request.POST.get("entry_type", "note")
            content = request.POST.get("content", "").strip()
            agenda_item_id = request.POST.get("agenda_item_id")

            if content:
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
                # Set encrypted content after save (needs organization)
                entry.set_content_encrypted(content)
                entry.save()

                # If decision, also update the agenda item
                if entry_type == "decision" and agenda_item_id:
                    votes_yes = int(request.POST.get("votes_yes", 0))
                    votes_no = int(request.POST.get("votes_no", 0))
                    votes_abstain = int(request.POST.get("votes_abstain", 0))

                    agenda_item = FactionAgendaItem.objects.get(id=agenda_item_id)
                    agenda_item.has_decision = True
                    agenda_item.votes_for = votes_yes
                    agenda_item.votes_against = votes_no
                    agenda_item.votes_abstain = votes_abstain
                    agenda_item.save()

                messages.success(request, "Protokolleintrag gespeichert.")

        elif action == "update_status":
            # Quick status change (e.g., from draft to planned)
            new_status = request.POST.get("status")
            if new_status and new_status in dict(FactionMeeting.STATUS_CHOICES):
                meeting.status = new_status
                meeting.save()
                status_display = dict(FactionMeeting.STATUS_CHOICES).get(new_status, new_status)
                messages.success(request, f"Status geändert zu '{status_display}'.")

        elif action == "update_meeting":
            # Full meeting edit (inline modal)
            from datetime import datetime

            # Check permission
            can_edit = (
                meeting.created_by == self.membership or self.membership.has_permission("faction.manage")
            ) and meeting.status in ["draft", "planned", "invited", "ongoing"]

            if not can_edit:
                messages.error(request, "Keine Berechtigung zum Bearbeiten.")
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            # Update basic details
            meeting.title = request.POST.get("title", meeting.title)
            meeting.description = request.POST.get("description", "")

            # Update datetime
            start_date = request.POST.get("start_date")
            start_time = request.POST.get("start_time", "18:00")
            if start_date:
                try:
                    start_datetime = datetime.strptime(f"{start_date} {start_time}", "%Y-%m-%d %H:%M")
                    meeting.start = (
                        timezone.make_aware(start_datetime) if timezone.is_naive(start_datetime) else start_datetime
                    )
                except ValueError:
                    pass

            # Update location
            meeting.location = request.POST.get("location", "")
            meeting.is_virtual = request.POST.get("is_virtual") == "on"
            meeting.video_link = request.POST.get("video_link", "") if meeting.is_virtual else ""

            # Update status
            new_status = request.POST.get("status")
            if new_status and new_status in dict(FactionMeeting.STATUS_CHOICES):
                meeting.status = new_status

            meeting.save()
            messages.success(request, "Änderungen gespeichert.")

        elif action == "delete":
            # Check permission
            can_delete = meeting.created_by == self.membership or self.membership.has_permission("faction.manage")
            if not can_delete:
                messages.error(request, "Keine Berechtigung zum Löschen.")
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            meeting_title = meeting.title
            meeting.delete()
            messages.success(request, f"Sitzung '{meeting_title}' wurde gelöscht.")
            return redirect("work:faction", org_slug=self.organization.slug)

        elif action == "create_task":
            # Create a task from a protocol entry
            entry_id = request.POST.get("entry_id")
            entry = get_object_or_404(FactionProtocolEntry, id=entry_id, meeting=meeting)

            if entry.entry_type != "action":
                messages.error(request, "Nur Aufgaben-Einträge können ins Task-Board übernommen werden.")
            else:
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

                # Mark entry as task created
                entry.action_completed = True
                entry.save()

                short_title = task_title[:50] + "..." if len(task_title) > 50 else task_title
                messages.success(request, f"Aufgabe '{short_title}' erstellt.")

        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)


class FactionMeetingEditView(WorkViewMixin, View):
    """Legacy redirect - editing is now inline in detail view."""

    permission_required = "faction.manage"

    def get(self, request, *args, **kwargs):
        """Redirect to detail page where inline editing is available."""
        return redirect(
            "work:faction_detail",
            org_slug=self.organization.slug,
            meeting_id=kwargs.get("meeting_id"),
        )

    def post(self, request, *args, **kwargs):
        """Redirect POST requests to detail page."""
        return redirect(
            "work:faction_detail",
            org_slug=self.organization.slug,
            meeting_id=kwargs.get("meeting_id"),
        )


class FactionProtocolView(WorkViewMixin, TemplateView):
    """Live protocol view for a faction meeting."""

    template_name = "work/faction/protocol.html"
    permission_required = "protocols.create"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "faction"

        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        context["meeting"] = meeting

        # Agenda items
        context["agenda_items"] = meeting.agenda_items.order_by("order", "number")

        # Protocol entries grouped by agenda item
        entries = meeting.protocol_entries.select_related(
            "agenda_item", "speaker__user", "action_assignee__user", "created_by__user"
        ).order_by("order", "created_at")
        context["protocol_entries"] = entries

        # Attendees present
        context["present_members"] = meeting.attendances.filter(status="present").select_related("membership__user")

        # All members for speaker selection
        context["all_members"] = self.organization.memberships.filter(is_active=True).select_related("user")

        # Forms
        context["entry_form"] = FactionProtocolEntryForm()
        context["decision_form"] = FactionDecisionForm()

        # Can edit protocol
        context["can_edit"] = meeting.status in ["ongoing", "completed"] and not meeting.protocol_approved

        return context

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        action = request.POST.get("action")

        if action == "add_entry":
            form = FactionProtocolEntryForm(request.POST)
            if form.is_valid():
                entry = form.save(commit=False)
                entry.meeting = meeting
                entry.created_by = self.membership
                entry.order = meeting.protocol_entries.count() + 1
                entry.save()

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse(
                        {
                            "success": True,
                            "entry": {
                                "id": str(entry.id),
                                "type": entry.entry_type,
                                "content": entry.get_content_decrypted(),
                                "speaker": entry.speaker.user.get_display_name() if entry.speaker else None,
                            },
                        }
                    )

                messages.success(request, "Eintrag hinzugefügt.")

        elif action == "record_decision":
            agenda_item_id = request.POST.get("agenda_item_id")
            agenda_item = get_object_or_404(FactionAgendaItem, id=agenda_item_id, meeting=meeting)

            form = FactionDecisionForm(request.POST)
            if form.is_valid():
                decision = form.save(commit=False)
                decision.agenda_item = agenda_item
                decision.recorded_by = self.membership
                decision.save()

                # Update agenda item
                agenda_item.has_decision = True
                agenda_item.votes_for = decision.votes_yes
                agenda_item.votes_against = decision.votes_no
                agenda_item.votes_abstain = decision.votes_abstain
                agenda_item.save()

                messages.success(request, "Abstimmung erfasst.")

        elif action == "check_in":
            member_id = request.POST.get("member_id")
            try:
                attendance = meeting.attendances.get(membership_id=member_id)
                attendance.status = "present"
                attendance.checked_in_at = timezone.now()
                attendance.save()

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"success": True})

            except FactionAttendance.DoesNotExist:
                pass

        elif action == "check_out":
            member_id = request.POST.get("member_id")
            try:
                attendance = meeting.attendances.get(membership_id=member_id)
                attendance.checked_out_at = timezone.now()
                attendance.save()

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"success": True})

            except FactionAttendance.DoesNotExist:
                pass

        elif action == "approve_protocol":
            if meeting.status == "completed" and not meeting.protocol_approved:
                meeting.protocol_approved = True
                meeting.protocol_approved_at = timezone.now()
                meeting.protocol_approved_by = self.membership
                meeting.save()
                messages.success(request, "Protokoll genehmigt.")

        elif action == "start_meeting":
            if meeting.status in ["planned", "invited"]:
                meeting.status = "ongoing"
                meeting.save()
                messages.success(request, "Sitzung gestartet.")

        elif action == "end_meeting":
            if meeting.status == "ongoing":
                meeting.status = "completed"
                meeting.end = timezone.now()
                meeting.save()
                messages.success(request, "Sitzung beendet.")

        return redirect("work:faction_protocol", org_slug=self.organization.slug, meeting_id=meeting.id)


class FactionScheduleListView(WorkViewMixin, TemplateView):
    """List of recurring meeting schedules."""

    template_name = "work/faction/schedules.html"
    permission_required = "faction.manage"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Show under organization nav when accessed from organization settings
        if "/organization/" in self.request.path:
            context["active_nav"] = "organization"
            context["active_tab"] = "faction_schedules"
        else:
            context["active_nav"] = "faction"

        context["schedules"] = (
            FactionMeetingSchedule.objects.filter(organization=self.organization)
            .annotate(meeting_count=Count("meetings"))
            .order_by("weekday", "time")
        )

        context["form"] = FactionScheduleForm()

        return context

    def post(self, request, *args, **kwargs):
        form = FactionScheduleForm(request.POST)

        if form.is_valid():
            schedule = form.save(commit=False)
            schedule.organization = self.organization
            schedule.save()

            messages.success(request, "Sitzungsplan erstellt.")
            # Redirect to the URL used to access this view
            if "/organization/" in request.path:
                return redirect("work:organization_faction_schedules", org_slug=self.organization.slug)
            return redirect("work:faction_schedules", org_slug=self.organization.slug)

        context = self.get_context_data()
        context["form"] = form
        return self.render_to_response(context)


class FactionAttendanceResponseView(WorkViewMixin, View):
    """API endpoint for attendance response (RSVP)."""

    permission_required = "faction.view_public"

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        try:
            attendance = meeting.attendances.get(membership=self.membership)
        except FactionAttendance.DoesNotExist:
            if request.headers.get("HX-Request"):
                return HttpResponse('<p class="text-red-600 text-sm">Keine Einladung gefunden</p>')
            return JsonResponse({"error": "Keine Einladung gefunden"}, status=404)

        new_status = request.POST.get("status")
        if new_status in ["confirmed", "declined", "tentative"]:
            attendance.status = new_status
            attendance.response_message = request.POST.get("response_message", "")
            attendance.responded_at = timezone.now()
            attendance.save()

            # HTMX request - return HTML partial
            if request.headers.get("HX-Request"):
                status_classes = {
                    "confirmed": "bg-green-100 text-green-700",
                    "declined": "bg-red-100 text-red-700",
                    "tentative": "bg-yellow-100 text-yellow-700",
                }
                css_class = status_classes.get(new_status, "bg-gray-100 text-gray-700")
                html = f"""
                    <h4 class="text-sm font-medium text-gray-900 dark:text-white mb-3">Meine Teilnahme</h4>
                    <div class="text-center">
                        <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm {css_class}">
                            {attendance.get_status_display()}
                        </span>
                    </div>
                """
                return HttpResponse(html)

            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse(
                    {
                        "success": True,
                        "status": new_status,
                        "status_display": attendance.get_status_display(),
                    }
                )

            messages.success(request, f"Antwort gespeichert: {attendance.get_status_display()}")
        else:
            if request.headers.get("HX-Request"):
                return HttpResponse('<p class="text-red-600 text-sm">Ungültiger Status</p>')
            return JsonResponse({"error": "Ungültiger Status"}, status=400)

        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)


class FactionAgendaItemView(WorkViewMixin, View):
    """API endpoint for agenda item management."""

    permission_required = "faction.manage"

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        action = request.POST.get("action")

        if action == "add":
            title = request.POST.get("title", "").strip()
            visibility = request.POST.get("visibility", "public")
            parent_id = request.POST.get("parent_id", "").strip()

            if not title:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"error": "Titel ist erforderlich"}, status=400)
                messages.error(request, "Titel ist erforderlich.")
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            parent = None
            if parent_id:
                parent = get_object_or_404(FactionAgendaItem, id=parent_id, meeting=meeting)
                visibility = parent.visibility  # Inherit visibility from parent

            # Auto-generate number based on hierarchy
            if parent:
                # Sub-item: get next child number (e.g., 1.1, 1.2)
                child_count = parent.children.count() + 1
                number = f"{parent.number}.{child_count}"
            else:
                # Top-level: count existing top-level items in same visibility
                existing_items = meeting.agenda_items.filter(visibility=visibility, parent__isnull=True).exclude(
                    is_approval_item=True
                )
                next_number = existing_items.count() + 1

                # Account for approval item (TOP 1)
                if visibility == "public":
                    has_approval = meeting.agenda_items.filter(is_approval_item=True).exists()
                    if has_approval:
                        next_number += 1

                # Format number based on visibility
                if visibility == "internal":
                    number = f"NÖ {next_number}"
                else:
                    number = str(next_number)

            next_order = meeting.agenda_items.count() + 1

            description = request.POST.get("description", "").strip()

            item = FactionAgendaItem(
                meeting=meeting,
                title=title,
                number=number,
                visibility=visibility,
                order=next_order,
                parent=parent,
            )
            item.save()

            # Set encrypted description after save (needs organization relationship)
            if description:
                item.set_description_encrypted(description)
                item.save()

            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse(
                    {
                        "success": True,
                        "item": {
                            "id": str(item.id),
                            "number": item.number,
                            "title": item.title,
                            "visibility": item.visibility,
                            "parent_id": str(parent.id) if parent else None,
                        },
                    }
                )

            messages.success(request, f"TOP {item.number} hinzugefügt.")
            return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

        elif action == "edit":
            item_id = request.POST.get("item_id")
            title = request.POST.get("title", "").strip()
            description = request.POST.get("description", "").strip()

            if not title:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"error": "Titel ist erforderlich"}, status=400)
                messages.error(request, "Titel ist erforderlich.")
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            item = get_object_or_404(FactionAgendaItem, id=item_id, meeting=meeting)
            item.title = title

            # Update encrypted description
            if description:
                item.set_description_encrypted(description)
            else:
                item.description_encrypted = None

            item.save()

            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse(
                    {
                        "success": True,
                        "item": {
                            "id": str(item.id),
                            "number": item.number,
                            "title": item.title,
                        },
                    }
                )

            messages.success(request, f"TOP {item.number} aktualisiert.")
            return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

        elif action == "reorder":
            import json

            order = json.loads(request.POST.get("order", "[]"))
            for idx, item_id in enumerate(order):
                FactionAgendaItem.objects.filter(id=item_id, meeting=meeting).update(order=idx)

            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"success": True})

            return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

        elif action == "delete":
            item_id = request.POST.get("item_id")
            item = FactionAgendaItem.objects.filter(
                id=item_id,
                meeting=meeting,
                is_approval_item=False,  # Can't delete approval item
            ).first()

            if item:
                item_number = item.number
                item.delete()

                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"success": True})

                messages.success(request, f"TOP {item_number} gelöscht.")
            else:
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return JsonResponse({"error": "TOP nicht gefunden oder kann nicht gelöscht werden"}, status=400)
                messages.error(request, "TOP nicht gefunden oder kann nicht gelöscht werden.")

            return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

        elif action == "move":
            item_id = request.POST.get("item_id")
            direction = request.POST.get("direction")  # "up" or "down"

            item = FactionAgendaItem.objects.filter(
                id=item_id,
                meeting=meeting,
                is_approval_item=False,
                parent__isnull=True,  # Only move top-level items
            ).first()

            if item and direction in ("up", "down"):
                # Get items in same visibility group
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
                    # Calculate swap target
                    if direction == "up" and current_index > 0:
                        swap_target = siblings[current_index - 1]
                    elif direction == "down" and current_index < len(siblings) - 1:
                        swap_target = siblings[current_index + 1]
                    else:
                        swap_target = None

                    if swap_target:
                        # Swap order values
                        item.order, swap_target.order = swap_target.order, item.order
                        item.save()
                        swap_target.save()

                        # Renumber all items in this visibility
                        self._renumber_items(meeting, item.visibility)

            return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"error": "Ungültige Aktion"}, status=400)

        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

    def _renumber_items(self, meeting, visibility):
        """Renumber items after reordering to maintain consistent numbering."""
        items = meeting.agenda_items.filter(
            visibility=visibility, parent__isnull=True, is_approval_item=False
        ).order_by("order")

        prefix = "NÖ " if visibility == "internal" else ""
        start_num = 1

        # Account for approval item in public section
        if visibility == "public" and meeting.agenda_items.filter(is_approval_item=True).exists():
            start_num = 2

        for i, item in enumerate(items, start=start_num):
            new_number = f"{prefix}{i}"
            if item.number != new_number:
                item.number = new_number
                item.save(update_fields=["number"])

            # Also renumber children
            for j, child in enumerate(item.children.order_by("order"), start=1):
                child_number = f"{new_number}.{j}"
                if child.number != child_number:
                    child.number = child_number
                    child.save(update_fields=["number"])


class FactionInviteView(WorkViewMixin, View):
    """Send invitations for a meeting."""

    permission_required = "faction.invite"

    def get(self, request, *args, **kwargs):
        """Handle GET (e.g., after login redirect) - redirect to detail page."""
        return redirect(
            "work:faction_detail",
            org_slug=self.organization.slug,
            meeting_id=kwargs.get("meeting_id"),
        )

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        if meeting.invitation_sent:
            messages.warning(request, "Einladungen wurden bereits versendet.")
        else:
            # Send email invitations
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
                messages.warning(
                    request,
                    "Einladungsstatus aktualisiert. Keine E-Mails versendet (keine E-Mail-Adressen hinterlegt).",
                )

        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)


class FactionSettingsView(WorkViewMixin, View):
    """Legacy redirect - settings are now in organization settings."""

    permission_required = "faction.manage"

    def get(self, request, *args, **kwargs):
        """Redirect to organization faction settings."""
        return redirect("work:organization_faction_settings", org_slug=self.organization.slug)

    def post(self, request, *args, **kwargs):
        """Redirect POST requests to organization faction settings."""
        return redirect("work:organization_faction_settings", org_slug=self.organization.slug)


class FactionAttendanceStatusView(WorkViewMixin, View):
    """Update attendance status for a member or guest."""

    permission_required = "faction.manage"

    def get(self, request, *args, **kwargs):
        """Handle GET (e.g., after login redirect) - redirect to detail page."""
        return redirect(
            "work:faction_detail",
            org_slug=self.organization.slug,
            meeting_id=kwargs.get("meeting_id"),
        )

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        attendance_id = request.POST.get("attendance_id")
        new_status = request.POST.get("status")

        valid_statuses = [
            "invited",
            "confirmed",
            "declined",
            "tentative",
            "present",
            "absent",
            "excused",
        ]

        if not attendance_id or new_status not in valid_statuses:
            messages.error(request, "Ungültige Anfrage.")
            return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

        attendance = get_object_or_404(FactionAttendance, id=attendance_id, meeting=meeting)
        attendance.status = new_status

        # Track check-in/out times
        if new_status == "present" and not attendance.checked_in_at:
            attendance.checked_in_at = timezone.now()
        elif (
            new_status in ("absent", "excused", "declined")
            and attendance.checked_in_at
            and not attendance.checked_out_at
        ):
            attendance.checked_out_at = timezone.now()

        attendance.save()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"success": True, "status": new_status})

        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)


class FactionAddAttendeeView(WorkViewMixin, View):
    """Add a guest or member to meeting attendance."""

    permission_required = "faction.manage"

    def get(self, request, *args, **kwargs):
        """Handle GET (e.g., after login redirect) - redirect to detail page."""
        return redirect(
            "work:faction_detail",
            org_slug=self.organization.slug,
            meeting_id=kwargs.get("meeting_id"),
        )

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        attendee_type = request.POST.get("attendee_type")  # "guest" or "member"
        status = request.POST.get("status", "present")
        if attendee_type == "guest":
            guest_name = request.POST.get("guest_name", "").strip()
            if not guest_name:
                messages.error(request, "Bitte einen Namen für den Gast angeben.")
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            # Create guest attendance
            attendance = FactionAttendance.objects.create(
                meeting=meeting,
                is_guest=True,
                guest_name=guest_name,
                status=status,
            )

            if status == "present":
                attendance.checked_in_at = timezone.now()
                attendance.save()

            messages.success(request, f"Gast '{guest_name}' hinzugefügt.")

        elif attendee_type == "member":
            membership_id = request.POST.get("membership_id")
            if not membership_id:
                messages.error(request, "Bitte ein Mitglied auswählen.")
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            from apps.tenants.models import Membership

            membership = get_object_or_404(Membership, id=membership_id, organization=self.organization)

            # Check if attendance already exists
            existing = FactionAttendance.objects.filter(meeting=meeting, membership=membership).first()
            if existing:
                messages.warning(
                    request,
                    f"{membership.user.get_display_name()} ist bereits in der Teilnehmerliste.",
                )
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            # Create member attendance
            attendance = FactionAttendance.objects.create(
                meeting=meeting,
                membership=membership,
                is_guest=False,
                status=status,
            )

            if status == "present":
                attendance.checked_in_at = timezone.now()
                attendance.save()

            messages.success(request, f"{membership.user.get_display_name()} hinzugefügt.")

        else:
            messages.error(request, "Ungültiger Teilnehmertyp.")

        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)


class FactionAgendaProposalView(WorkViewMixin, View):
    """Handle agenda item proposals from Sachkundige Bürger*innen."""

    permission_required = "agenda.propose"

    def post(self, request, *args, **kwargs):
        meeting = get_object_or_404(FactionMeeting, id=kwargs.get("meeting_id"), organization=self.organization)

        action = request.POST.get("action")

        if action == "propose":
            # Create a new proposal
            title = request.POST.get("title", "").strip()
            description = request.POST.get("description", "").strip()
            visibility = request.POST.get("visibility", "public")

            if not title:
                messages.error(request, "Bitte einen Titel angeben.")
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            from .services import AgendaProposalService

            item = AgendaProposalService.create_proposal(
                meeting=meeting,
                title=title,
                description=description,
                proposed_by=self.membership,
                visibility=visibility,
            )

            messages.success(request, f"Vorschlag '{title}' eingereicht.")

        elif action == "accept":
            # Accept a proposal (requires agenda.manage permission)
            if not self.membership.has_permission("agenda.manage"):
                messages.error(request, "Keine Berechtigung zum Annehmen von Vorschlägen.")
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            item_id = request.POST.get("item_id")
            item = get_object_or_404(FactionAgendaItem, id=item_id, meeting=meeting, proposal_status="proposed")

            from .services import AgendaProposalService

            assign_number = request.POST.get("number", "").strip()
            AgendaProposalService.accept_proposal(item, self.membership, assign_number or None)

            messages.success(request, f"Vorschlag '{item.title}' angenommen.")

        elif action == "reject":
            # Reject a proposal (requires agenda.manage permission)
            if not self.membership.has_permission("agenda.manage"):
                messages.error(request, "Keine Berechtigung zum Ablehnen von Vorschlägen.")
                return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

            item_id = request.POST.get("item_id")
            reason = request.POST.get("reason", "").strip()
            item = get_object_or_404(FactionAgendaItem, id=item_id, meeting=meeting, proposal_status="proposed")

            from .services import AgendaProposalService

            AgendaProposalService.reject_proposal(item, self.membership, reason)

            messages.success(request, f"Vorschlag '{item.title}' abgelehnt.")

        return redirect("work:faction_detail", org_slug=self.organization.slug, meeting_id=meeting.id)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import (
    CreateView,
    UpdateView,
)

from ..models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionMeeting,
    SessionPaper,
)
from ..permissions import SessionViewMixin

# =============================================================================
# API ENDPOINTS (HTMX)
# =============================================================================


class AgendaItemCreateView(SessionViewMixin, CreateView):
    """Create a new agenda item via HTMX."""

    model = SessionAgendaItem
    template_name = "session/partials/agenda_item_form.html"
    fields = ["number", "name", "is_public", "paper"]
    permission_required = "edit_meetings"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["paper"].queryset = SessionPaper.objects.filter(tenant=self.session_tenant)
        return form

    def form_valid(self, form):
        meeting = get_object_or_404(
            SessionMeeting,
            pk=self.kwargs["meeting_id"],
            tenant=self.session_tenant,
        )
        form.instance.meeting = meeting
        form.instance.order = meeting.agenda_items.count() + 1
        self.object = form.save()

        if self.is_htmx:
            return HttpResponse(
                status=204,
                headers={"HX-Trigger": "agendaItemCreated"},
            )
        return redirect(
            "session:meeting_detail",
            tenant_slug=self.session_tenant.slug,
            meeting_id=meeting.id,
        )


class AttendanceUpdateView(SessionViewMixin, UpdateView):
    """Update attendance status via HTMX."""

    model = SessionAttendance
    template_name = "session/partials/attendance_row.html"
    fields = ["status", "arrival_time", "departure_time", "notes"]
    pk_url_kwarg = "attendance_id"
    permission_required = "manage_attendance"

    def form_valid(self, form):
        self.object = form.save()

        if self.is_htmx:
            context = {"attendance": self.object}
            return self.render_to_response(context)
        return redirect(
            "session:meeting_detail",
            tenant_slug=self.session_tenant.slug,
            meeting_id=self.object.meeting_id,
        )

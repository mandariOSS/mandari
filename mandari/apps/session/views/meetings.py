# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from django.contrib import messages
from django.db.models import Q
from django.urls import reverse
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    UpdateView,
)

from ..models import (
    SessionMeeting,
    SessionOrganization,
)
from ..permissions import SessionViewMixin

# =============================================================================
# MEETINGS
# =============================================================================


class MeetingListView(SessionViewMixin, ListView):
    """List of meetings."""

    model = SessionMeeting
    template_name = "session/meetings/list.html"
    context_object_name = "meetings"
    paginate_by = 20
    permission_required = "view_meetings"

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.select_related("organization").order_by("-start")

        # Filter by organization
        org_id = self.request.GET.get("organization")
        if org_id:
            qs = qs.filter(organization_id=org_id)

        # Filter by state
        state = self.request.GET.get("state")
        if state:
            qs = qs.filter(meeting_state=state)

        # Filter by date range
        date_from = self.request.GET.get("from")
        date_to = self.request.GET.get("to")
        if date_from:
            qs = qs.filter(start__date__gte=date_from)
        if date_to:
            qs = qs.filter(start__date__lte=date_to)

        # Search
        search = self.request.GET.get("q")
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(organization__name__icontains=search))

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["organizations"] = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        ).order_by("name")
        context["meeting_states"] = SessionMeeting._meta.get_field("meeting_state").choices
        return context


class MeetingDetailView(SessionViewMixin, DetailView):
    """Meeting detail view."""

    model = SessionMeeting
    template_name = "session/meetings/detail.html"
    context_object_name = "meeting"
    pk_url_kwarg = "meeting_id"
    permission_required = "view_meetings"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.select_related("organization", "created_by__user")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meeting = self.object

        # Agenda items
        context["agenda_items"] = meeting.agenda_items.select_related("paper").order_by("order", "number")

        # Attendances
        context["attendances"] = meeting.attendances.select_related("person").order_by("person__family_name")

        # Files — NÖ-Anlagen nur für Berechtigte sichtbar
        files = meeting.files.order_by("name")
        if not self.has_permission("view_non_public_meetings"):
            files = files.filter(is_public=True)
        context["files"] = list(files)
        context["file_can_edit"] = self.has_permission("edit_meetings")

        # Protocol
        context["protocol"] = getattr(meeting, "protocol", None)

        return context


class MeetingCreateView(SessionViewMixin, CreateView):
    """Create a new meeting."""

    model = SessionMeeting
    template_name = "session/meetings/form.html"
    fields = [
        "name",
        "organization",
        "start",
        "end",
        "location",
        "room",
        "is_public",
        "invitation_text",
    ]
    permission_required = "create_meetings"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # Limit organization choices to current tenant
        form.fields["organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        return form

    def form_valid(self, form):
        form.instance.tenant = self.session_tenant
        form.instance.created_by = self.session_user
        messages.success(self.request, "Sitzung wurde erstellt.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "session:meeting_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "meeting_id": self.object.id,
            },
        )


class MeetingUpdateView(SessionViewMixin, UpdateView):
    """Update a meeting."""

    model = SessionMeeting
    template_name = "session/meetings/form.html"
    fields = [
        "name",
        "organization",
        "start",
        "end",
        "location",
        "room",
        "is_public",
        "meeting_state",
        "invitation_text",
        "cancelled",
        "cancellation_reason",
    ]
    pk_url_kwarg = "meeting_id"
    permission_required = "edit_meetings"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        return form

    def form_valid(self, form):
        messages.success(self.request, "Sitzung wurde aktualisiert.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "session:meeting_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "meeting_id": self.object.id,
            },
        )

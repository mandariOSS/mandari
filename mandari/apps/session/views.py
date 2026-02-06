# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)

from .models import (
    SessionAgendaItem,
    SessionApplication,
    SessionAttendance,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionPerson,
    SessionUser,
)
from .permissions import SessionViewMixin

# =============================================================================
# DASHBOARD
# =============================================================================


class DashboardView(SessionViewMixin, TemplateView):
    """Main dashboard for Session RIS."""

    template_name = "session/dashboard.html"
    permission_required = "view_dashboard"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenant = self.session_tenant
        today = timezone.now().date()

        # Upcoming meetings (next 30 days)
        context["upcoming_meetings"] = (
            SessionMeeting.objects.filter(
                tenant=tenant,
                start__date__gte=today,
                start__date__lte=today + timedelta(days=30),
                cancelled=False,
            )
            .select_related("organization")
            .order_by("start")[:5]
        )

        # Recent papers
        context["recent_papers"] = (
            SessionPaper.objects.filter(
                tenant=tenant,
            )
            .select_related("main_organization", "originator_organization")
            .order_by("-created_at")[:5]
        )

        # Pending applications
        context["pending_applications"] = SessionApplication.objects.filter(
            tenant=tenant,
            status__in=["submitted", "received", "in_review"],
        ).order_by("-submitted_at")[:5]

        # Statistics
        context["stats"] = {
            "meetings_total": SessionMeeting.objects.filter(tenant=tenant).count(),
            "meetings_upcoming": SessionMeeting.objects.filter(
                tenant=tenant,
                start__date__gte=today,
                cancelled=False,
            ).count(),
            "papers_total": SessionPaper.objects.filter(tenant=tenant).count(),
            "papers_draft": SessionPaper.objects.filter(tenant=tenant, status="draft").count(),
            "applications_pending": SessionApplication.objects.filter(
                tenant=tenant,
                status__in=["submitted", "received", "in_review"],
            ).count(),
            "organizations_count": SessionOrganization.objects.filter(tenant=tenant, is_active=True).count(),
            "persons_count": SessionPerson.objects.filter(tenant=tenant, is_active=True).count(),
        }

        return context


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

        # Files
        context["files"] = meeting.files.order_by("name")

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


# =============================================================================
# PAPERS
# =============================================================================


class PaperListView(SessionViewMixin, ListView):
    """List of papers."""

    model = SessionPaper
    template_name = "session/papers/list.html"
    context_object_name = "papers"
    paginate_by = 20
    permission_required = "view_papers"

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.select_related("main_organization", "originator_organization", "originator_person").order_by(
            "-date", "-created_at"
        )

        # Filter by type
        paper_type = self.request.GET.get("type")
        if paper_type:
            qs = qs.filter(paper_type=paper_type)

        # Filter by status
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)

        # Filter by organization
        org_id = self.request.GET.get("organization")
        if org_id:
            qs = qs.filter(Q(main_organization_id=org_id) | Q(originator_organization_id=org_id))

        # Search
        search = self.request.GET.get("q")
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(reference__icontains=search) | Q(main_text__icontains=search))

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["organizations"] = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        ).order_by("name")
        context["paper_types"] = SessionPaper._meta.get_field("paper_type").choices
        context["paper_statuses"] = SessionPaper._meta.get_field("status").choices
        return context


class PaperDetailView(SessionViewMixin, DetailView):
    """Paper detail view."""

    model = SessionPaper
    template_name = "session/papers/detail.html"
    context_object_name = "paper"
    pk_url_kwarg = "paper_id"
    permission_required = "view_papers"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.select_related(
            "main_organization",
            "originator_organization",
            "originator_person",
            "created_by__user",
            "approved_by__user",
            "source_application",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        paper = self.object

        # Files
        context["files"] = paper.files.order_by("name")

        # Agenda items (where this paper was discussed)
        context["agenda_items"] = paper.agenda_items.select_related("meeting__organization").order_by("-meeting__start")

        return context


class PaperCreateView(SessionViewMixin, CreateView):
    """Create a new paper."""

    model = SessionPaper
    template_name = "session/papers/form.html"
    fields = [
        "reference",
        "name",
        "paper_type",
        "main_text",
        "resolution_text",
        "is_public",
        "date",
        "deadline",
        "main_organization",
        "originator_organization",
        "originator_person",
    ]
    permission_required = "create_papers"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["main_organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        form.fields["originator_organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        form.fields["originator_person"].queryset = SessionPerson.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        return form

    def form_valid(self, form):
        form.instance.tenant = self.session_tenant
        form.instance.created_by = self.session_user
        messages.success(self.request, "Vorlage wurde erstellt.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "session:paper_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "paper_id": self.object.id,
            },
        )


class PaperUpdateView(SessionViewMixin, UpdateView):
    """Update a paper."""

    model = SessionPaper
    template_name = "session/papers/form.html"
    fields = [
        "reference",
        "name",
        "paper_type",
        "main_text",
        "resolution_text",
        "is_public",
        "status",
        "date",
        "deadline",
        "main_organization",
        "originator_organization",
        "originator_person",
    ]
    pk_url_kwarg = "paper_id"
    permission_required = "edit_papers"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["main_organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        form.fields["originator_organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        form.fields["originator_person"].queryset = SessionPerson.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        return form

    def form_valid(self, form):
        messages.success(self.request, "Vorlage wurde aktualisiert.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "session:paper_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "paper_id": self.object.id,
            },
        )


# =============================================================================
# APPLICATIONS
# =============================================================================


class ApplicationListView(SessionViewMixin, ListView):
    """List of applications from parties."""

    model = SessionApplication
    template_name = "session/applications/list.html"
    context_object_name = "applications"
    paginate_by = 20
    permission_required = "view_applications"

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.select_related("submitting_organization", "target_organization", "received_by__user").order_by(
            "-submitted_at"
        )

        # Filter by status
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)

        # Filter by type
        app_type = self.request.GET.get("type")
        if app_type:
            qs = qs.filter(application_type=app_type)

        # Search
        search = self.request.GET.get("q")
        if search:
            qs = qs.filter(
                Q(title__icontains=search) | Q(reference__icontains=search) | Q(submitter_name__icontains=search)
            )

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["application_statuses"] = SessionApplication._meta.get_field("status").choices
        context["application_types"] = SessionApplication._meta.get_field("application_type").choices
        return context


class ApplicationDetailView(SessionViewMixin, DetailView):
    """Application detail view."""

    model = SessionApplication
    template_name = "session/applications/detail.html"
    context_object_name = "application"
    pk_url_kwarg = "application_id"
    permission_required = "view_applications"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.select_related(
            "submitting_organization",
            "target_organization",
            "received_by__user",
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Created papers from this application
        context["created_papers"] = self.object.created_papers.all()
        return context


class ApplicationProcessView(SessionViewMixin, UpdateView):
    """Process an application (change status, add notes)."""

    model = SessionApplication
    template_name = "session/applications/process.html"
    fields = [
        "status",
        "target_organization",
        "processing_notes",
    ]
    pk_url_kwarg = "application_id"
    permission_required = "process_applications"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields["target_organization"].queryset = SessionOrganization.objects.filter(
            tenant=self.session_tenant, is_active=True
        )
        return form

    def form_valid(self, form):
        # Set received info if marking as received
        if form.instance.status == "received" and not form.instance.received_at:
            form.instance.received_at = timezone.now()
            form.instance.received_by = self.session_user
        messages.success(self.request, "Antrag wurde aktualisiert.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse(
            "session:application_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "application_id": self.object.id,
            },
        )


class ApplicationConvertView(SessionViewMixin, TemplateView):
    """Convert an application to a paper."""

    template_name = "session/applications/convert.html"
    permission_required = ["process_applications", "create_papers"]
    permission_require_all = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        application = get_object_or_404(
            SessionApplication,
            pk=self.kwargs["application_id"],
            tenant=self.session_tenant,
        )
        context["application"] = application
        context["organizations"] = SessionOrganization.objects.filter(tenant=self.session_tenant, is_active=True)
        return context

    def post(self, request, *args, **kwargs):
        application = get_object_or_404(
            SessionApplication,
            pk=self.kwargs["application_id"],
            tenant=self.session_tenant,
        )

        # Create paper from application
        paper = SessionPaper.objects.create(
            tenant=self.session_tenant,
            name=application.title,
            paper_type="motion",
            main_text=application.justification,
            resolution_text=application.resolution_proposal,
            is_public=True,
            date=timezone.now().date(),
            main_organization_id=request.POST.get("main_organization"),
            source_application=application,
            created_by=self.session_user,
        )

        # Update application status
        application.status = "converted"
        application.save(update_fields=["status", "updated_at"])

        messages.success(
            request,
            f'Antrag wurde in Vorlage "{paper.reference}" umgewandelt.',
        )

        return redirect(
            "session:paper_detail",
            tenant_slug=self.session_tenant.slug,
            paper_id=paper.id,
        )


# =============================================================================
# ORGANIZATIONS
# =============================================================================


class OrganizationListView(SessionViewMixin, ListView):
    """List of organizations/committees."""

    model = SessionOrganization
    template_name = "session/organizations/list.html"
    context_object_name = "organizations"
    paginate_by = 20
    permission_required = "view_meetings"  # Anyone who can view meetings can see orgs

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.annotate(member_count=Count("memberships", filter=Q(memberships__end_date__isnull=True))).order_by(
            "name"
        )

        # Filter by type
        org_type = self.request.GET.get("type")
        if org_type:
            qs = qs.filter(organization_type=org_type)

        # Filter by active status
        if self.request.GET.get("active") == "1":
            qs = qs.filter(is_active=True)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["organization_types"] = SessionOrganization._meta.get_field("organization_type").choices
        return context


class OrganizationDetailView(SessionViewMixin, DetailView):
    """Organization detail view."""

    model = SessionOrganization
    template_name = "session/organizations/detail.html"
    context_object_name = "organization"
    pk_url_kwarg = "organization_id"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org = self.object

        # Members
        context["memberships"] = (
            org.memberships.select_related("person").filter(end_date__isnull=True).order_by("person__family_name")
        )

        # Recent meetings
        context["recent_meetings"] = org.meetings.order_by("-start")[:5]

        # Recent papers
        context["recent_papers"] = SessionPaper.objects.filter(
            Q(main_organization=org) | Q(originator_organization=org)
        ).order_by("-date")[:5]

        return context


# =============================================================================
# PERSONS
# =============================================================================


class PersonListView(SessionViewMixin, ListView):
    """List of persons."""

    model = SessionPerson
    template_name = "session/persons/list.html"
    context_object_name = "persons"
    paginate_by = 50
    permission_required = "view_meetings"  # Basic access

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.order_by("family_name", "given_name")

        # Filter by active status
        if self.request.GET.get("active") != "0":
            qs = qs.filter(is_active=True)

        # Search
        search = self.request.GET.get("q")
        if search:
            qs = qs.filter(
                Q(given_name__icontains=search) | Q(family_name__icontains=search) | Q(email__icontains=search)
            )

        return qs


class PersonDetailView(SessionViewMixin, DetailView):
    """Person detail view."""

    model = SessionPerson
    template_name = "session/persons/detail.html"
    context_object_name = "person"
    pk_url_kwarg = "person_id"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        person = self.object

        # Memberships
        context["memberships"] = person.memberships.select_related("organization").order_by("-start_date")

        # Recent attendances
        context["recent_attendances"] = person.attendances.select_related("meeting__organization").order_by(
            "-meeting__start"
        )[:10]

        return context


# =============================================================================
# SETTINGS
# =============================================================================


class SettingsView(SessionViewMixin, TemplateView):
    """Tenant settings view."""

    template_name = "session/settings/index.html"
    permission_required = "manage_settings"


class UserListView(SessionViewMixin, ListView):
    """List of session users."""

    model = SessionUser
    template_name = "session/settings/users.html"
    context_object_name = "session_users"
    paginate_by = 50
    permission_required = "manage_users"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.select_related("user").prefetch_related("roles").order_by("user__email")


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

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import (
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)

from ..models import (
    SessionApplication,
    SessionOrganization,
    SessionPaper,
)
from ..permissions import SessionViewMixin

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

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
    SessionOrganization,
    SessionPaper,
    SessionPerson,
)
from ..permissions import SessionViewMixin

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

        # Ö/NÖ: Nichtöffentliche Vorlagen nur für Berechtigte
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)

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
        # Ö/NÖ: Nichtöffentliche Vorlagen nur für Berechtigte
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)
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

        # Files — NÖ-Anlagen nur für Berechtigte sichtbar
        files = paper.files.order_by("name")
        if not self.has_permission("view_non_public_papers"):
            files = files.filter(is_public=True)
        context["files"] = list(files)
        context["file_can_edit"] = self.has_permission("edit_papers")

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

    def get_queryset(self):
        qs = super().get_queryset()
        # Ö/NÖ: Nichtöffentliche Vorlagen nur für Berechtigte bearbeitbar
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)
        return qs

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

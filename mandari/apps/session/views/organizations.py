# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
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

        # Members (aktive Besetzung)
        context["memberships"] = (
            org.memberships.select_related("person", "substitute_for")
            .filter(end_date__isnull=True)
            .order_by("person__family_name")
        )

        # Beendete Mitgliedschaften (Historie)
        context["ended_memberships"] = (
            org.memberships.select_related("person").filter(end_date__isnull=False).order_by("-end_date")[:10]
        )

        # Recent meetings
        context["recent_meetings"] = org.meetings.order_by("-start")[:5]

        # Recent papers
        context["recent_papers"] = SessionPaper.objects.filter(
            Q(main_organization=org) | Q(originator_organization=org)
        ).order_by("-date")[:5]

        # Besetzungs-Verwaltung
        context["can_manage"] = self.has_permission("manage_organizations")
        if context["can_manage"]:
            context["available_persons"] = SessionPerson.objects.filter(
                tenant=self.session_tenant, is_active=True
            ).order_by("family_name", "given_name")
            context["membership_roles"] = org.memberships.model._meta.get_field("role").choices

        return context


ORGANIZATION_FORM_FIELDS = [
    "name",
    "short_name",
    "organization_type",
    "parent",
    "meeting_frequency",
    "invitation_period_days",
    "target_member_count",
    "default_meeting_location",
    "default_meeting_start_time",
    "allowance_amount",
    "start_date",
    "end_date",
    "is_active",
]


class OrganizationFormMixin:
    """Gemeinsame Logik für Gremien-Formulare."""

    model = SessionOrganization
    template_name = "session/organizations/form.html"
    fields = ORGANIZATION_FORM_FIELDS
    permission_required = "manage_organizations"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        parent_qs = SessionOrganization.objects.filter(tenant=self.session_tenant, is_active=True)
        obj = getattr(self, "object", None)
        if obj is not None and obj.pk:
            parent_qs = parent_qs.exclude(pk=obj.pk)
        form.fields["parent"].queryset = parent_qs
        return form

    def get_success_url(self):
        return reverse(
            "session:organization_detail",
            kwargs={
                "tenant_slug": self.session_tenant.slug,
                "organization_id": self.object.id,
            },
        )


class OrganizationCreateView(OrganizationFormMixin, SessionViewMixin, CreateView):
    """Gremium anlegen (Issue #27)."""

    def form_valid(self, form):
        form.instance.tenant = self.session_tenant
        messages.success(self.request, f"Gremium „{form.instance.name}“ wurde angelegt.")
        return super().form_valid(form)


class OrganizationUpdateView(OrganizationFormMixin, SessionViewMixin, UpdateView):
    """Gremium bearbeiten (inkl. Sitzungsturnus, Ladungsfrist, Mitgliederzahl)."""

    pk_url_kwarg = "organization_id"

    def form_valid(self, form):
        messages.success(self.request, f"Gremium „{form.instance.name}“ wurde aktualisiert.")
        return super().form_valid(form)


class OrganizationDeactivateView(SessionViewMixin, View):
    """Gremium deaktivieren/reaktivieren (statt Löschen — Historie bleibt)."""

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, organization_id):
        org = get_object_or_404(SessionOrganization, pk=organization_id, tenant=self.session_tenant)
        org.is_active = not org.is_active
        org.save()
        state = "reaktiviert" if org.is_active else "deaktiviert"
        messages.success(request, f"Gremium „{org.name}“ wurde {state}.")
        return redirect(
            "session:organization_detail",
            tenant_slug=tenant_slug,
            organization_id=org.id,
        )

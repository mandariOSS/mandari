# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db.models import Count, Exists, OuterRef, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    UpdateView,
)

from apps.common.params import uuid_param

from ..models import (
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
)
from ..permissions import SessionViewMixin
from ..services import membership_service

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
        # Aktive Mitglieder nach derselben Regel wie Ladung und Anwesenheit (laufend heute, Person aktiv)
        active = membership_service.active_q(timezone.localdate(), "memberships__")
        qs = qs.annotate(member_count=Count("memberships", filter=active, distinct=True)).order_by("name")

        # Filter by type
        org_type = self.request.GET.get("type")
        if org_type:
            qs = qs.filter(organization_type=org_type)

        # Filter by active status
        if self.request.GET.get("active") == "1":
            qs = qs.filter(is_active=True)

        # Perioden-Filter (Issue #39): Gremien mit Besetzungen in der Periode. Als Unterabfrage, nicht als
        # zweiter Join über die Besetzungen – der würde die Mitgliederzahl oben vervielfachen.
        term_id = self.request.GET.get("term")
        if term_id:
            # Ungültige Kennung: kein Treffer statt Serverfehler
            term_uuid = uuid_param(term_id)
            in_term = SessionOrganizationMembership.objects.filter(
                organization=OuterRef("pk"), legislative_term_id=term_uuid
            )
            qs = qs.filter(Exists(in_term)) if term_uuid else qs.none()

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["organization_types"] = SessionOrganization._meta.get_field("organization_type").choices

        # Perioden-Filter (Issue #39)
        context["legislative_terms"] = SessionLegislativeTerm.objects.filter(tenant=self.session_tenant)
        context["selected_term"] = self.request.GET.get("term", "")
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

        # Besetzung je Wahlperiode (Issue #39): Standard ist die aktive
        # Besetzung; über ?term=<id> lassen sich vergangene Perioden einsehen
        context["legislative_terms"] = SessionLegislativeTerm.objects.filter(tenant=self.session_tenant)
        # Ungültige Kennung (?term=abc): aktuelle Besetzung statt Serverfehler
        selected_term_id = uuid_param(self.request.GET.get("term", ""))
        selected_term = None
        if selected_term_id:
            selected_term = SessionLegislativeTerm.objects.filter(
                tenant=self.session_tenant, pk=selected_term_id
            ).first()
        context["selected_term"] = selected_term

        # Laufend heute und Person aktiv – dieselbe Regel wie Ladung und Anwesenheit
        today = timezone.localdate()
        active = membership_service.active_q(today)
        memberships_qs = org.memberships.select_related("person", "substitute_for")
        if selected_term is not None:
            context["memberships"] = memberships_qs.filter(legislative_term=selected_term).order_by(
                "person__family_name"
            )
        else:
            context["memberships"] = memberships_qs.filter(active).order_by("person__family_name")

        # Künftige Besetzungen (z. B. nach einem Periodenwechsel mit künftigem Stichtag)
        context["upcoming_memberships"] = (
            org.memberships.select_related("person").filter(start_date__gt=today).order_by("start_date")
        )

        # Beendete Mitgliedschaften (Historie) und offene Besetzungen deaktivierter Personen
        context["ended_memberships"] = (
            org.memberships.select_related("person")
            .filter(Q(end_date__lt=today) | Q(person__is_active=False, end_date__isnull=True))
            .order_by("-end_date")[:10]
        )

        # Recent meetings – nichtöffentliche nur mit NÖ-Sichtrecht
        # Auch gemeinsame Sitzungen, an denen das Gremium beteiligt ist (Issue #317)
        permissions = self.session_permissions
        context["recent_meetings"] = (
            SessionMeeting.objects.filter(SessionMeeting.organization_q(org))
            .visible_to(permissions)
            .distinct()
            .order_by("-start")[:5]
        )

        # Recent papers – nur mit dem Vorlagenrecht, nichtöffentliche nur mit NÖ-Sichtrecht
        context["recent_papers"] = (
            SessionPaper.objects.filter(Q(main_organization=org) | Q(originator_organization=org))
            .visible_to(permissions)
            .order_by("-date")[:5]
            if "view_papers" in permissions
            else []
        )

        # Besetzungs-Verwaltung
        context["can_manage"] = self.has_permission("manage_organizations")
        if context["can_manage"]:
            context["available_persons"] = SessionPerson.objects.filter(
                tenant=self.session_tenant, is_active=True
            ).order_by("family_name", "given_name")
            context["membership_roles"] = org.memberships.model._meta.get_field("role").choices
            # Nachrücken: Wer schon laufend Mitglied ist, kommt als Nachrücker/in nicht in Frage
            context["current_member_ids"] = set(org.memberships.filter(active).values_list("person_id", flat=True))

        return context


ORGANIZATION_FORM_FIELDS = [
    "name",
    "short_name",
    "organization_type",
    # Gesetzliche Ausschussart für Sitzungsformate (Issue #138)
    "committee_kind",
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


def descendant_ids(organization) -> set:
    """Kennungen aller untergeordneten Gremien (Kinder, Enkel, …) – als Übergeordnetes ausgeschlossen."""
    children: dict = {}
    for pk, parent_id in SessionOrganization.objects.filter(tenant_id=organization.tenant_id).values_list(
        "pk", "parent_id"
    ):
        children.setdefault(parent_id, []).append(pk)
    found: set = set()
    pending = list(children.get(organization.pk, []))
    while pending:
        pk = pending.pop()
        if pk not in found:
            found.add(pk)
            pending.extend(children.get(pk, []))
    return found


class SessionOrganizationForm(forms.ModelForm):
    """Gremien-Formular mit Plausibilitätsprüfung (Sitzungsgeld, Zeitraum, Hierarchie)."""

    class Meta:
        model = SessionOrganization
        fields = ORGANIZATION_FORM_FIELDS

    def clean_allowance_amount(self):
        amount = self.cleaned_data.get("allowance_amount")
        # Das Sitzungsgeld geht in Abrechnung und SEPA-Datei ein: nie negativ
        if amount is not None and amount < Decimal("0"):
            raise forms.ValidationError("Das Sitzungsgeld darf nicht negativ sein.")
        return amount

    def clean(self):
        cleaned = super().clean()
        error = membership_service.period_error(cleaned.get("start_date"), cleaned.get("end_date"))
        if error:
            self.add_error("end_date", error)
        parent = cleaned.get("parent")
        if (
            parent is not None
            and self.instance.pk
            and (parent.pk == self.instance.pk or parent.pk in descendant_ids(self.instance))
        ):
            self.add_error("parent", "Ein Gremium kann nicht sich selbst oder einem eigenen Untergremium unterstehen.")
        return cleaned


class OrganizationFormMixin:
    """Gemeinsame Logik für Gremien-Formulare."""

    model = SessionOrganization
    form_class = SessionOrganizationForm
    template_name = "session/organizations/form.html"
    permission_required = "manage_organizations"

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        parent_qs = SessionOrganization.objects.filter(tenant=self.session_tenant, is_active=True)
        obj = getattr(self, "object", None)
        if obj is not None and obj.pk:
            # Weder das Gremium selbst noch seine Untergremien (keine Zyklen)
            parent_qs = parent_qs.exclude(pk=obj.pk).exclude(pk__in=descendant_ids(obj))
        form.fields["parent"].queryset = parent_qs
        form.fields["committee_kind"].choices = [
            ("", "Nicht eingeordnet"),
            *SessionOrganization.COMMITTEE_KIND_CHOICES,
        ]
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Hinweis nach dem Namen, solange keine Ausschussart gesetzt ist (Sitzungsformate, Issue #138)
        obj = getattr(self, "object", None)
        if obj is not None and not obj.committee_kind:
            from ..services import meeting_format_service

            suspected = meeting_format_service.suspected_committee_kinds(obj)
            if suspected:
                labels = meeting_format_service.committee_kind_labels(suspected)
                context["committee_kind_hint"] = meeting_format_service.join_labels(labels, "oder")
        return context

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

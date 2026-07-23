# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Besetzungs-Verwaltung (Gremienmitgliedschaften) für das Session RIS (Issue #27).

Mitgliedschaften mit Funktion (Vorsitz, stellv. Vorsitz, Mitglied,
sachkundige/r Bürger/in, …), Stimmrecht, Vertreterregelung und Zeitraum —
inklusive Nachrücker-Flow (Mitgliedschaft beenden + Nachfolger anlegen
in einem Schritt). Alle Änderungen werden über die Audit-Signale
protokolliert.
"""

from datetime import date

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View

from ..models import (
    SessionLegislativeTerm,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
)
from ..permissions import SessionViewMixin

# =============================================================================
# HELPERS
# =============================================================================


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _get_membership(view, membership_id):
    return get_object_or_404(
        SessionOrganizationMembership.objects.select_related("organization", "person"),
        pk=membership_id,
        organization__tenant=view.session_tenant,
    )


def _org_redirect(view, organization):
    return redirect(
        "session:organization_detail",
        tenant_slug=view.session_tenant.slug,
        organization_id=organization.id,
    )


def _membership_from_post(view, request, organization) -> SessionOrganizationMembership | None:
    """Mitgliedschafts-Felder aus dem POST lesen (tenant-sicher)."""
    person = get_object_or_404(
        SessionPerson,
        pk=request.POST.get("person"),
        tenant=view.session_tenant,
    )
    substitute_for = None
    if request.POST.get("substitute_for"):
        substitute_for = get_object_or_404(
            SessionPerson,
            pk=request.POST["substitute_for"],
            tenant=view.session_tenant,
        )
    role = request.POST.get("role", "member")
    valid_roles = {c[0] for c in SessionOrganizationMembership._meta.get_field("role").choices}
    if role not in valid_roles:
        role = "member"
    start_date = _parse_date(request.POST.get("start_date")) or timezone.now().date()
    return SessionOrganizationMembership(
        organization=organization,
        person=person,
        role=role,
        has_voting_rights=request.POST.get("has_voting_rights") == "on",
        substitute_for=substitute_for,
        start_date=start_date,
        end_date=_parse_date(request.POST.get("end_date")),
        # Wahlperiode automatisch aus dem Beginn ableiten (Issue #39)
        legislative_term=SessionLegislativeTerm.for_date(view.session_tenant, start_date),
    )


# =============================================================================
# VIEWS
# =============================================================================


class MembershipCreateView(SessionViewMixin, View):
    """Besetzung anlegen (aus der Gremien-Detailseite)."""

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, organization_id):
        organization = get_object_or_404(SessionOrganization, pk=organization_id, tenant=self.session_tenant)
        membership = _membership_from_post(self, request, organization)

        exists = SessionOrganizationMembership.objects.filter(
            organization=organization,
            person=membership.person,
            end_date__isnull=True,
        ).exists()
        if exists:
            messages.error(request, f"{membership.person.display_name} ist bereits aktives Mitglied dieses Gremiums.")
            return _org_redirect(self, organization)

        membership.save()
        messages.success(
            request,
            f"{membership.person.display_name} wurde als {membership.get_role_display()} aufgenommen.",
        )
        return _org_redirect(self, organization)


class MembershipUpdateView(SessionViewMixin, View):
    """Besetzung bearbeiten (Funktion, Stimmrecht, Vertretung, Zeitraum)."""

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, membership_id):
        membership = _get_membership(self, membership_id)

        role = request.POST.get("role", membership.role)
        valid_roles = {c[0] for c in SessionOrganizationMembership._meta.get_field("role").choices}
        if role in valid_roles:
            membership.role = role
        membership.has_voting_rights = request.POST.get("has_voting_rights") == "on"
        if "start_date" in request.POST:
            membership.start_date = _parse_date(request.POST.get("start_date")) or membership.start_date
        if "end_date" in request.POST:
            membership.end_date = _parse_date(request.POST.get("end_date"))
        if "substitute_for" in request.POST:
            if request.POST["substitute_for"]:
                membership.substitute_for = get_object_or_404(
                    SessionPerson,
                    pk=request.POST["substitute_for"],
                    tenant=self.session_tenant,
                )
            else:
                membership.substitute_for = None
        membership.save()
        messages.success(request, f"Besetzung von {membership.person.display_name} wurde aktualisiert.")
        return _org_redirect(self, membership.organization)


class MembershipEndView(SessionViewMixin, View):
    """Mitgliedschaft beenden (Ausscheiden)."""

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, membership_id):
        membership = _get_membership(self, membership_id)
        membership.end_date = _parse_date(request.POST.get("end_date")) or timezone.now().date()
        membership.save()
        messages.success(
            request,
            f"Mitgliedschaft von {membership.person.display_name} wurde zum {membership.end_date:%d.%m.%Y} beendet.",
        )
        return _org_redirect(self, membership.organization)


class MembershipSuccessionView(SessionViewMixin, View):
    """
    Nachrücker-Flow: Mitgliedschaft beenden + Nachfolger anlegen in einem Schritt.
    """

    permission_required = "manage_organizations"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, membership_id):
        membership = _get_membership(self, membership_id)
        organization = membership.organization

        successor = get_object_or_404(
            SessionPerson,
            pk=request.POST.get("successor"),
            tenant=self.session_tenant,
        )
        if successor.pk == membership.person_id:
            messages.error(request, "Nachrücker/in darf nicht die ausscheidende Person sein.")
            return _org_redirect(self, organization)

        change_date = _parse_date(request.POST.get("change_date")) or timezone.now().date()

        # 1) Ausscheiden dokumentieren
        membership.end_date = change_date
        membership.save()

        # 2) Nachfolger mit gleicher Funktion/gleichem Stimmrecht anlegen
        SessionOrganizationMembership.objects.create(
            organization=organization,
            person=successor,
            role=membership.role,
            has_voting_rights=membership.has_voting_rights,
            start_date=change_date,
            # Wahlperiode aus dem Stichtag ableiten (Issue #39)
            legislative_term=SessionLegislativeTerm.for_date(self.session_tenant, change_date),
        )

        messages.success(
            request,
            f"{successor.display_name} rückt zum {change_date:%d.%m.%Y} für {membership.person.display_name} nach.",
        )
        return _org_redirect(self, organization)

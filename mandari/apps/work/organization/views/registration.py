# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


class RegistrationSettingsView(WorkViewMixin, TemplateView):
    """Selbstregistrierungs-Einstellungen für die Organisation."""

    template_name = "work/organization/registration_settings.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "registration"

        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

        # Domains als Text (eine pro Zeile)
        domains = self.organization.registration_email_domains or []
        context["email_domains_text"] = "\n".join(domains)

        # Rollen für Dropdown
        context["roles"] = self.organization.roles.order_by("priority", "name")

        return context

    def post(self, request, *args, **kwargs):
        org = self.organization

        org.registration_enabled = request.POST.get("registration_enabled") == "1"
        org.registration_auto_approve = request.POST.get("registration_auto_approve") == "1"

        # Domains parsen (eine pro Zeile, bereinigt)
        domains_text = request.POST.get("registration_email_domains", "")
        domains = [d.strip().lower().lstrip("@") for d in domains_text.splitlines() if d.strip()]
        org.registration_email_domains = domains

        # Standardrolle
        role_id = request.POST.get("registration_default_role", "")
        if role_id:
            from apps.tenants.models import Role

            role = Role.objects.filter(id=role_id, organization=org).first()
            org.registration_default_role = role
        else:
            org.registration_default_role = None

        org.save(
            update_fields=[
                "registration_enabled",
                "registration_email_domains",
                "registration_auto_approve",
                "registration_default_role",
            ]
        )

        messages.success(request, "Registrierungseinstellungen gespeichert.")
        return redirect("work:organization_registration", org_slug=org.slug)


class MemberApproveView(WorkViewMixin, View):
    """Ausstehende Selbstregistrierung freischalten."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Membership

        membership = get_object_or_404(
            Membership,
            id=kwargs["membership_id"],
            organization=self.organization,
            is_active=False,
        )
        membership.is_active = True
        membership.save(update_fields=["is_active"])

        messages.success(request, f"{membership.user.get_display_name()} wurde freigeschaltet.")
        return redirect("work:members", org_slug=self.organization.slug)


class MemberRejectView(WorkViewMixin, View):
    """Ausstehende Selbstregistrierung ablehnen."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Membership

        membership = get_object_or_404(
            Membership,
            id=kwargs["membership_id"],
            organization=self.organization,
            is_active=False,
        )
        name = membership.user.get_display_name()
        membership.delete()

        messages.success(request, f"Registrierungsanfrage von {name} wurde abgelehnt.")
        return redirect("work:members", org_slug=self.organization.slug)

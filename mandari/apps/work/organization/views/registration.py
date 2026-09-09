# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Selbstregistrierung: Einstellungen sowie Freischalten/Ablehnen ausstehender Anfragen.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services


class RegistrationSettingsView(WorkViewMixin, TemplateView):
    """Selbstregistrierungs-Einstellungen für die Organisation."""

    template_name = "work/organization/registration_settings.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "registration"
        context["can_manage_faction"] = selectors.permission_checker(self.membership).has_permission("faction.manage")
        # Domains als Text (eine pro Zeile)
        context["email_domains_text"] = "\n".join(self.organization.registration_email_domains or [])
        context["roles"] = selectors.registration_roles(self.organization)
        return context

    def post(self, request, *args, **kwargs):
        services.save_registration_settings(
            self.organization,
            enabled=request.POST.get("registration_enabled") == "1",
            auto_approve=request.POST.get("registration_auto_approve") == "1",
            domains_text=request.POST.get("registration_email_domains", ""),
            default_role_id=request.POST.get("registration_default_role", ""),
        )
        messages.success(request, "Registrierungseinstellungen gespeichert.")
        return redirect("work:organization_registration", org_slug=self.organization.slug)


class MemberApproveView(WorkViewMixin, View):
    """Ausstehende Selbstregistrierung freischalten."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        membership = selectors.get_member_or_404(self.organization, kwargs["membership_id"], is_active=False)
        services.approve_registration(membership)
        messages.success(request, f"{membership.user.get_display_name()} wurde freigeschaltet.")
        return redirect("work:members", org_slug=self.organization.slug)


class MemberRejectView(WorkViewMixin, View):
    """Ausstehende Selbstregistrierung ablehnen."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        membership = selectors.get_member_or_404(self.organization, kwargs["membership_id"], is_active=False)
        name = services.reject_registration(membership)
        messages.success(request, f"Registrierungsanfrage von {name} wurde abgelehnt.")
        return redirect("work:members", org_slug=self.organization.slug)

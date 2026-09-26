# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Selbstregistrierung: Einstellungen sowie Freischalten/Ablehnen offener Anfragen.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ._helpers import flash_error


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
        # Ungesetzte Checkboxen fehlen im POST ganz; die Anwesenheit des Feldes genügt
        try:
            services.save_registration_settings(
                self.organization,
                actor=self.membership,
                enabled="registration_enabled" in request.POST,
                auto_approve="registration_auto_approve" in request.POST,
                domains_text=request.POST.get("registration_email_domains", ""),
                default_role_id=request.POST.get("registration_default_role", ""),
            )
        except services.ServiceError as exc:
            flash_error(request, exc)
        else:
            messages.success(request, "Registrierungseinstellungen gespeichert.")
        return redirect("work:organization_registration", org_slug=self.organization.slug)


class MemberApproveView(WorkViewMixin, View):
    """Offene Registrierungsanfrage freischalten und die Person per E-Mail informieren."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        membership = selectors.get_pending_registration_or_404(self.organization, kwargs["membership_id"])
        name = membership.user.get_display_name()
        if services.approve_registration(membership, actor=self.membership):
            messages.success(request, f"{name} wurde freigeschaltet und per E-Mail informiert.")
        else:
            messages.warning(
                request,
                f"{name} wurde freigeschaltet, die E-Mail konnte aber nicht versendet werden. "
                "Bitte informiere die Person direkt.",
            )
        return redirect("work:members", org_slug=self.organization.slug)


class MemberRejectView(WorkViewMixin, View):
    """Offene Registrierungsanfrage ablehnen (optional mit Begründung) und die Person informieren."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        membership = selectors.get_pending_registration_or_404(self.organization, kwargs["membership_id"])
        name, mail_sent = services.reject_registration(membership, reason=request.POST.get("reason", ""))
        if mail_sent:
            messages.success(
                request, f"Die Anfrage von {name} wurde abgelehnt; die Person wurde per E-Mail informiert."
            )
        else:
            messages.warning(
                request,
                f"Die Anfrage von {name} wurde abgelehnt, die E-Mail konnte aber nicht versendet werden.",
            )
        return redirect("work:members", org_slug=self.organization.slug)

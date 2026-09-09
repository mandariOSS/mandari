# SPDX-License-Identifier: AGPL-3.0-or-later
"""
E-Mail-/Absender-Einstellungen der Organisation (Issue #65).

Die Organisation entscheidet, ob Fraktions-Mails (Einladungen,
Erinnerungen, Freigabe-Hinweise) über das eigene SMTP oder den
mandari-Standard versendet werden — inkl. konfigurierbarem
Fallback-Verhalten und Testmail-Versand.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error


class OrganizationEmailSettingsView(WorkViewMixin, TemplateView):
    """Absender-/SMTP-Einstellungen der Organisation (Issue #65)."""

    template_name = "work/organization/email_settings.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "email"
        context["can_manage_faction"] = selectors.permission_checker(self.membership).has_permission("faction.manage")

        org = self.organization
        context["mail_sender_mode"] = org.mail_sender_mode
        context["smtp_fallback_to_mandari"] = org.smtp_fallback_to_mandari
        context["smtp_host"] = org.smtp_host
        context["smtp_port"] = org.smtp_port
        context["smtp_username"] = org.smtp_username
        context["smtp_use_tls"] = org.smtp_use_tls
        context["smtp_from_email"] = org.smtp_from_email
        context["smtp_from_name"] = org.smtp_from_name
        # Passwort wird niemals ausgegeben — nur die Information, ob eines hinterlegt ist
        context["smtp_password_set"] = bool(org.smtp_password_encrypted)
        return context

    def post(self, request, *args, **kwargs):
        try:
            if request.POST.get("action", "save") == "send_test":
                messages.success(request, services.send_test_email(self.organization, request.user.email))
            else:
                self._save(request)
        except ServiceError as exc:
            flash_error(request, exc)
        return redirect("work:organization_email_settings", org_slug=self.organization.slug)

    def _save(self, request):
        data = services.EmailSettingsInput(
            mail_sender_mode=request.POST.get("mail_sender_mode", "mandari"),
            smtp_fallback_to_mandari=request.POST.get("smtp_fallback_to_mandari") == "on",
            smtp_host=request.POST.get("smtp_host", "").strip(),
            smtp_port_raw=request.POST.get("smtp_port", "587"),
            smtp_username=request.POST.get("smtp_username", "").strip(),
            smtp_use_tls=request.POST.get("smtp_use_tls") == "on",
            smtp_from_email=request.POST.get("smtp_from_email", "").strip(),
            smtp_from_name=request.POST.get("smtp_from_name", "").strip(),
            smtp_password=request.POST.get("smtp_password", ""),
            smtp_password_clear=request.POST.get("smtp_password_clear") == "on",
        )
        if services.save_email_settings(self.organization, data):
            messages.warning(
                request,
                "Eigenes SMTP ist aktiviert, aber kein Server hinterlegt — bis dahin läuft der Versand über mandari.",
            )
        messages.success(request, "E-Mail-Einstellungen gespeichert.")

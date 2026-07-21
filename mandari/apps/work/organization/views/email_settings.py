# SPDX-License-Identifier: AGPL-3.0-or-later
"""
E-Mail-/Absender-Einstellungen der Organisation (Issue #65).

Die Organisation entscheidet, ob Fraktions-Mails (Einladungen,
Erinnerungen, Freigabe-Hinweise) über das eigene SMTP oder den
mandari-Standard versendet werden — inkl. konfigurierbarem
Fallback-Verhalten und Testmail-Versand.
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


class OrganizationEmailSettingsView(WorkViewMixin, TemplateView):
    """Absender-/SMTP-Einstellungen der Organisation (Issue #65)."""

    template_name = "work/organization/email_settings.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "email"

        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

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
        action = request.POST.get("action", "save")
        if action == "send_test":
            return self._send_test(request)
        return self._save(request)

    def _save(self, request):
        from django.core.exceptions import ValidationError
        from django.core.validators import EmailValidator

        org = self.organization

        mode = request.POST.get("mail_sender_mode", "mandari")
        if mode not in dict(org.MAIL_SENDER_MODE_CHOICES):
            mode = "mandari"

        from_email = request.POST.get("smtp_from_email", "").strip()
        if from_email:
            try:
                EmailValidator()(from_email)
            except ValidationError:
                messages.error(request, "Ungültige Absender-Adresse.")
                return redirect("work:organization_email_settings", org_slug=org.slug)

        try:
            port = int(request.POST.get("smtp_port", "587") or 587)
            port = max(1, min(port, 65535))
        except (TypeError, ValueError):
            port = 587

        org.mail_sender_mode = mode
        org.smtp_fallback_to_mandari = request.POST.get("smtp_fallback_to_mandari") == "on"
        org.smtp_host = request.POST.get("smtp_host", "").strip()
        org.smtp_port = port
        org.smtp_username = request.POST.get("smtp_username", "").strip()
        org.smtp_use_tls = request.POST.get("smtp_use_tls") == "on"
        org.smtp_from_email = from_email
        org.smtp_from_name = request.POST.get("smtp_from_name", "").strip()

        # Passwort nur überschreiben, wenn ein neues eingegeben wurde —
        # Ablage ausschließlich über den verschlüsselnden Accessor
        password = request.POST.get("smtp_password", "")
        if password:
            org.set_smtp_password(password)
        elif request.POST.get("smtp_password_clear") == "on":
            org.set_smtp_password("")

        if mode == "smtp" and not org.smtp_host:
            messages.warning(
                request,
                "Eigenes SMTP ist aktiviert, aber kein Server hinterlegt — bis dahin läuft der Versand über mandari.",
            )

        org.save()
        messages.success(request, "E-Mail-Einstellungen gespeichert.")
        return redirect("work:organization_email_settings", org_slug=org.slug)

    def _send_test(self, request):
        """Testmail über den konfigurierten Versandweg senden (Issue #65)."""
        from apps.common.org_email import OrgMailError, send_org_email

        org = self.organization
        recipient = request.user.email
        if not recipient:
            messages.error(request, "Dein Benutzerkonto hat keine E-Mail-Adresse.")
            return redirect("work:organization_email_settings", org_slug=org.slug)

        route = "eigenes SMTP" if org.mail_sender_mode == "smtp" and org.smtp_host else "mandari-Standard"
        body = "\n".join(
            [
                "Hallo,",
                "",
                f"dies ist eine Testmail von {org.name} über den Versandweg: {route}.",
                "Wenn diese Nachricht ankommt, funktioniert der konfigurierte Versand.",
                "",
                "Viele Grüße,",
                "mandari",
            ]
        )

        try:
            ok = send_org_email(
                org,
                subject=f"Testmail: E-Mail-Versand von {org.name}",
                body=body,
                to=[recipient],
                fail_silently=False,
            )
        except OrgMailError as exc:
            logger.warning("Testmail über Organisations-SMTP fehlgeschlagen (org=%s): %s", org.slug, exc)
            messages.error(
                request,
                "Testmail fehlgeschlagen: Der Versand über das eigene SMTP war nicht möglich "
                "(kein Fallback konfiguriert). Bitte Zugangsdaten prüfen.",
            )
            return redirect("work:organization_email_settings", org_slug=org.slug)
        except Exception as exc:  # noqa: BLE001 — Fehler verständlich anzeigen
            logger.warning("Testmail fehlgeschlagen (org=%s): %s", org.slug, exc)
            messages.error(request, "Testmail fehlgeschlagen. Bitte Konfiguration prüfen.")
            return redirect("work:organization_email_settings", org_slug=org.slug)

        if ok:
            messages.success(request, f"Testmail an {recipient} versendet (Versandweg: {route}).")
        else:
            messages.error(request, "Testmail konnte nicht versendet werden. Bitte Konfiguration prüfen.")
        return redirect("work:organization_email_settings", org_slug=org.slug)

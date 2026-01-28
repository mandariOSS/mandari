# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin configuration for common app.

Includes SiteSettings admin for global configuration.
"""

from urllib.parse import urlparse

from django import forms
from django.contrib import admin
from django.core.mail import send_mail
from django.contrib import messages
from unfold.admin import ModelAdmin
from unfold.decorators import action

from .models import SiteSettings


def get_safe_admin_redirect(request):
    """
    Get a safe redirect URL from HTTP_REFERER for admin actions.

    Validates that the referer is from the same host to prevent Open Redirect attacks.
    Only allows paths starting with /admin/ for additional security.
    """
    referer = request.META.get("HTTP_REFERER", "")
    if referer:
        parsed = urlparse(referer)
        # Only allow same-host redirects that start with /admin/
        if (not parsed.netloc or parsed.netloc == request.get_host()) and parsed.path.startswith("/admin/"):
            return parsed.path + ("?" + parsed.query if parsed.query else "")
    return "/admin/"


class SiteSettingsAdminForm(forms.ModelForm):
    """Custom form for SiteSettings with password widget."""

    email_host_password = forms.CharField(
        widget=forms.PasswordInput(render_value=True),
        required=False,
        label="SMTP Passwort",
        help_text="Leer lassen, um vorhandenes Passwort beizubehalten"
    )

    class Meta:
        model = SiteSettings
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Don't require password to be re-entered if already set
        if self.instance and self.instance.pk and self.instance.email_host_password:
            self.fields["email_host_password"].help_text = "Passwort ist gesetzt. Leer lassen, um es beizubehalten."


@admin.register(SiteSettings)
class SiteSettingsAdmin(ModelAdmin):
    """
    Admin for global site settings.

    Provides a single-page configuration interface.
    """

    form = SiteSettingsAdminForm

    fieldsets = (
        ("E-Mail / SMTP Einstellungen", {
            "fields": (
                "email_host",
                ("email_port", "email_use_tls", "email_use_ssl"),
                "email_host_user",
                "email_host_password",
                "email_timeout",
                ("default_from_email", "default_from_name"),
            ),
            "description": (
                "Konfiguration des SMTP-Servers für den E-Mail-Versand. "
                "Wenn leer, werden die Umgebungsvariablen verwendet."
            ),
        }),
        ("Allgemeine Einstellungen", {
            "fields": (
                "site_name",
                "site_description",
            ),
            "classes": ("collapse",),
        }),
        ("Wartungsmodus", {
            "fields": (
                "maintenance_mode",
                "maintenance_message",
            ),
            "classes": ("collapse",),
        }),
    )

    actions_detail = ["test_email"]

    def has_add_permission(self, request):
        # Only allow one instance
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Prevent deletion
        return False

    def save_model(self, request, obj, form, change):
        # Keep existing password if not changed
        if change and not form.cleaned_data.get("email_host_password"):
            old_obj = SiteSettings.objects.get(pk=obj.pk)
            obj.email_host_password = old_obj.email_host_password
        super().save_model(request, obj, form, change)

    @action(description="Test-E-Mail senden")
    def test_email(self, request, object_id):
        """Send a test email to verify SMTP settings."""
        from django.core.mail import get_connection, EmailMessage

        settings = SiteSettings.get_settings()
        config = SiteSettings.get_email_config()

        try:
            # Create connection with current settings
            connection = get_connection(
                backend=config["EMAIL_BACKEND"],
                host=config["EMAIL_HOST"],
                port=config["EMAIL_PORT"],
                username=config["EMAIL_HOST_USER"],
                password=config["EMAIL_HOST_PASSWORD"],
                use_tls=config["EMAIL_USE_TLS"],
                use_ssl=config["EMAIL_USE_SSL"],
                timeout=config["EMAIL_TIMEOUT"],
            )

            # Create and send test email
            from_email = config["DEFAULT_FROM_EMAIL"]
            if settings.default_from_name:
                from_email = f"{settings.default_from_name} <{config['DEFAULT_FROM_EMAIL']}>"

            email = EmailMessage(
                subject="Mandari Test-E-Mail",
                body=(
                    "Dies ist eine Test-E-Mail von Mandari.\n\n"
                    f"SMTP-Server: {config['EMAIL_HOST']}:{config['EMAIL_PORT']}\n"
                    f"TLS: {config['EMAIL_USE_TLS']}, SSL: {config['EMAIL_USE_SSL']}\n\n"
                    "Wenn Sie diese E-Mail erhalten, funktioniert die Konfiguration."
                ),
                from_email=from_email,
                to=[request.user.email],
                connection=connection,
            )
            email.send()

            messages.success(
                request,
                f"Test-E-Mail wurde erfolgreich an {request.user.email} gesendet."
            )
        except Exception as e:
            messages.error(
                request,
                f"Fehler beim Senden der Test-E-Mail: {str(e)}"
            )

        from django.http import HttpResponseRedirect
        # SECURITY: Validate referer to prevent Open Redirect attacks
        return HttpResponseRedirect(get_safe_admin_redirect(request))

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        # Always edit the singleton instance
        if object_id is None:
            settings, _ = SiteSettings.objects.get_or_create(pk=1)
            from django.shortcuts import redirect
            return redirect(f"/admin/common/sitesettings/{settings.pk}/change/")
        return super().changeform_view(request, object_id, form_url, extra_context)

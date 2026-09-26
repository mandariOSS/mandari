# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin configuration for common app.

Includes SiteSettings admin for global configuration.
"""

import logging
from urllib.parse import urlparse

from django import forms
from django.contrib import admin, messages
from django.utils.http import url_has_allowed_host_and_scheme
from unfold.admin import ModelAdmin
from unfold.decorators import action

from .admin_mixins import SingletonAdminMixin
from .models import AISettings, ProblemReport, SiteSettings


def get_safe_admin_redirect(request):
    """
    Get a safe redirect URL from HTTP_REFERER for admin actions.

    SECURITY: Uses Django's url_has_allowed_host_and_scheme to prevent Open Redirect attacks.
    Only allows paths starting with /admin/ for additional security.
    """
    referer = request.META.get("HTTP_REFERER", "")
    if referer and url_has_allowed_host_and_scheme(
        referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        # Additional check: only allow admin paths
        parsed = urlparse(referer)
        if parsed.path.startswith("/admin/"):
            return referer
    return "/admin/"


class SiteSettingsAdminForm(forms.ModelForm):
    """
    Systemeinstellungen mit Geheimnissen, die nur geschrieben werden.

    SMTP-Passwort und Nebius-Schlüssel sind reine Formularfelder: Sie werden nie ins HTML
    ausgegeben, eingetragene Werte verschlüsselt gespeichert (Hauptschlüssel), und ein leer
    gelassenes Feld behält den gespeicherten Wert. Steht noch ein Klartextwert in der früheren
    Spalte, wird er beim Speichern verschlüsselt übernommen.
    """

    #: Formularfeld → Setter am Modell
    SECRET_FIELDS = {
        "email_host_password": "set_email_host_password",
        "nebius_api_key": "set_nebius_api_key",
    }

    email_host_password = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        required=False,
        label="SMTP Passwort",
        help_text="Wird verschlüsselt gespeichert. Leer lassen, um ein vorhandenes Passwort beizubehalten.",
    )

    nebius_api_key = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        required=False,
        label="Nebius API Key",
        help_text=(
            "API Key für Nebius TokenFactory, wird verschlüsselt gespeichert. "
            "Kann auch via NEBIUS_API_KEY Umgebungsvariable gesetzt werden."
        ),
    )

    class Meta:
        model = SiteSettings
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.has_email_host_password:
            self.fields["email_host_password"].help_text = "Passwort ist gesetzt. Leer lassen, um es beizubehalten."
        if self.instance and self.instance.pk and self.instance.has_nebius_api_key:
            self.fields["nebius_api_key"].help_text = "Key ist gesetzt. Leer lassen, um ihn beizubehalten."

    def _secret_to_store(self, field: str) -> str:
        """Neu eingetragener Wert, sonst ein Klartextwert einer älteren Version (Rückfall), sonst leer."""
        return self.cleaned_data.get(field) or getattr(self.instance, f"{field}_legacy", "") or ""

    def clean(self):
        cleaned_data = super().clean()
        if any(self._secret_to_store(field) for field in self.SECRET_FIELDS):
            from apps.common.encryption import get_master_key

            try:
                get_master_key()
            except ValueError:
                raise forms.ValidationError(
                    "Geheimnisse können nicht verschlüsselt werden: ENCRYPTION_MASTER_KEY fehlt oder ist ungültig."
                ) from None
        return cleaned_data

    def save(self, commit=True):
        obj = super().save(commit=False)
        for field, setter in self.SECRET_FIELDS.items():
            value = self._secret_to_store(field)
            if value:
                getattr(obj, setter)(value)
        if commit:
            obj.save()
        return obj


@admin.register(SiteSettings)
class SiteSettingsAdmin(SingletonAdminMixin, ModelAdmin):
    """
    Admin for global site settings.

    Provides a single-page configuration interface.
    """

    form = SiteSettingsAdminForm

    fieldsets = (
        (
            "E-Mail / SMTP Einstellungen",
            {
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
            },
        ),
        (
            "Allgemeine Einstellungen",
            {
                "fields": (
                    "site_name",
                    "site_description",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            "KI-Einstellungen",
            {
                "fields": ("nebius_api_key",),
                "description": (
                    "Globaler Nebius API Key für KI-Features (Zusammenfassungen, Dokument-Assistent). "
                    "Wird als Fallback verwendet, wenn keine organisationsspezifische Konfiguration vorhanden ist."
                ),
            },
        ),
        (
            "Wartungsmodus",
            {
                "fields": (
                    "maintenance_mode",
                    "maintenance_message",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    actions_detail = ["test_email"]

    @action(description="Test-E-Mail senden")
    def test_email(self, request, object_id):
        """Send a test email to verify SMTP settings."""
        from django.core.mail import EmailMessage

        from apps.common.mail_backends import build_backend, send_with

        settings = SiteSettings.get_settings()
        config = SiteSettings.get_email_config()

        try:
            # Backend mit den aktuellen Einstellungen aufbauen (Django ≥ 6.1, #80)
            connection = build_backend(
                config["EMAIL_BACKEND"],
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
            )
            send_with(connection, email)

            messages.success(request, f"Test-E-Mail wurde erfolgreich an {request.user.email} gesendet.")
        except Exception:
            # Details (Server-Antwort, Ausnahme) nur ins Protokoll
            logging.getLogger(__name__).exception("Test-E-Mail über die Systemeinstellungen fehlgeschlagen")
            messages.error(
                request,
                "Die Test-E-Mail konnte nicht gesendet werden. Bitte die SMTP-Einstellungen prüfen; "
                "Einzelheiten stehen im Anwendungsprotokoll.",
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


class AISettingsAdminForm(forms.ModelForm):
    """Custom form for AISettings with write-only API key field."""

    api_key = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        required=False,
        label="API Key",
        help_text="Wird verschlüsselt gespeichert (AES-256-GCM, Master-Key).",
    )

    class Meta:
        model = AISettings
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.api_key_encrypted:
            self.fields[
                "api_key"
            ].help_text = "Ein Key ist gesetzt. Für Rotation neuen Key eintragen, sonst leer lassen."

    def save(self, commit=True):
        obj = super().save(commit=False)
        api_key = self.cleaned_data.get("api_key", "").strip()
        if api_key:
            obj.set_api_key(api_key)
        if commit:
            obj.save()
        return obj


@admin.register(AISettings)
class AISettingsAdmin(SingletonAdminMixin, ModelAdmin):
    """
    Admin for global AI configuration (Work DMS editor).

    Single-page singleton configuration: provider, model, key, output cap
    and the default monthly token budget per organization.
    """

    form = AISettingsAdminForm

    fieldsets = (
        (
            "Anbieter",
            {
                "fields": ("enabled", "provider", "base_url", "model_name", "api_key"),
                "description": (
                    "Globale Standard-Konfiguration für den KI-Assistenten im Dokumenten-Editor. "
                    "Organisationen mit eigenem API Key (Organization → KI) überschreiben diese Einstellungen."
                ),
            },
        ),
        (
            "Limits",
            {
                "fields": ("max_output_tokens", "default_org_monthly_token_limit"),
                "description": (
                    "Das Monatslimit gilt für Organisationen ohne eigenes Limit. Pro Organisation überschreibbar "
                    "über Organization → 'Token-Limit pro Monat' (leer = Standard, 0 = KI deaktiviert)."
                ),
            },
        ),
    )

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        # Always edit the singleton instance
        if object_id is None:
            instance, _ = AISettings.objects.get_or_create(pk=1)
            from django.shortcuts import redirect

            return redirect(f"/admin/common/aisettings/{instance.pk}/change/")
        return super().changeform_view(request, object_id, form_url, extra_context)


@admin.register(ProblemReport)
class ProblemReportAdmin(ModelAdmin):
    """Fehlermeldungen als Tickets im Admin-Dashboard (Issue-Formular „Problem melden")."""

    list_display = ("reference", "status", "short_message", "error_id", "reporter", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("reference", "error_id", "message", "email", "url")
    readonly_fields = (
        "reference",
        "error_id",
        "url",
        "message",
        "browser_info",
        "user",
        "email",
        "ip_address",
        "created_at",
        "notified_at",
    )
    fieldsets = (
        ("Meldung", {"fields": ("reference", "status", "error_id", "url", "message", "browser_info")}),
        ("Kontakt", {"fields": ("user", "email", "ip_address", "created_at")}),
        ("Bearbeitung", {"fields": ("admin_note", "resolved_at", "notified_at")}),
    )
    actions = ("mark_resolved_and_notify",)

    @admin.display(description="Beschreibung")
    def short_message(self, obj):
        return obj.message[:80]

    @admin.display(description="Meldende Person")
    def reporter(self, obj):
        return obj.reporter_email or "anonym"

    @admin.action(description="Als gelöst markieren und Rückmeldung senden")
    def mark_resolved_and_notify(self, request, queryset):
        from django.utils import timezone

        from apps.common.email import send_email

        notified = 0
        for report in queryset:
            report.status = "resolved"
            report.resolved_at = timezone.now()
            recipient = report.reporter_email
            if recipient:
                body = (
                    f"Guten Tag,\n\n"
                    f"vielen Dank für deine Fehlermeldung {report.reference}"
                    f"{f' (Fehler-ID {report.error_id})' if report.error_id else ''}.\n"
                    f"Das Problem wurde behoben.\n\n"
                    + (f"Anmerkung unseres Teams: {report.admin_note}\n\n" if report.admin_note else "")
                    + "Mit freundlichen Grüßen\nDein mandari-Team"
                )
                if send_email(
                    subject=f"Rückmeldung zu deiner Fehlermeldung {report.reference}",
                    body=body,
                    to=[recipient],
                    fail_silently=True,
                ):
                    report.notified_at = timezone.now()
                    notified += 1
            report.save()
        self.message_user(
            request,
            f"{queryset.count()} Meldung(en) als gelöst markiert, {notified} Rückmeldung(en) versandt.",
        )

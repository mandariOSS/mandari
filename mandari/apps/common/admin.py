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
from .ki_anbieter import ANBIETER_VORLAGEN, EIGENER, erlaubte_hosts, pruefe_basis_url, vorlage_nutzbar
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

    Das SMTP-Passwort ist ein reines Formularfeld: Es wird nie ins HTML ausgegeben, ein
    eingetragener Wert verschlüsselt gespeichert (Hauptschlüssel), und ein leer gelassenes Feld
    behält den gespeicherten Wert. Steht noch ein Klartextwert in der früheren Spalte, wird er beim
    Speichern verschlüsselt übernommen. KI-Zugänge stehen in den KI-Einstellungen (Issue #950).
    """

    #: Formularfeld → Setter am Modell
    SECRET_FIELDS = {
        "email_host_password": "set_email_host_password",
    }

    email_host_password = forms.CharField(
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        required=False,
        label="SMTP Passwort",
        help_text="Wird verschlüsselt gespeichert. Leer lassen, um ein vorhandenes Passwort beizubehalten.",
    )

    class Meta:
        model = SiteSettings
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.has_email_host_password:
            self.fields["email_host_password"].help_text = "Passwort ist gesetzt. Leer lassen, um es beizubehalten."

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
            "Wartungsmodus",
            {
                "fields": (
                    "maintenance_mode",
                    "maintenance_message",
                ),
                "description": (
                    "Im Wartungsmodus antworten Bürgerportal, Work, Session und die APIs mit 503 und "
                    "der Wartungsnachricht. Erreichbar bleiben die Administration samt Anmeldung, "
                    "Gesundheitsprüfungen und Metriken; angemeldete Konten mit Mitarbeiterstatus sehen "
                    "die Anwendung weiter. Die Umstellung wirkt sofort nach dem Speichern."
                ),
                "classes": ("collapse",),
            },
        ),
    )

    actions_detail = ["test_email"]

    @action(description="Test-E-Mail senden")
    def test_email(self, request, object_id):
        """Send a test email to verify SMTP settings."""
        from apps.common import mail

        config = SiteSettings.get_email_config()

        try:
            # Sofort über den Weg der Plattform: Das Ergebnis zeigt der Admin an
            mail.send(
                kind="betrieb.testmail",
                sofort=True,
                subject="Mandari Test-E-Mail",
                body=(
                    "Dies ist eine Test-E-Mail von Mandari.\n\n"
                    f"SMTP-Server: {config['EMAIL_HOST']}:{config['EMAIL_PORT']}\n"
                    f"TLS: {config['EMAIL_USE_TLS']}, SSL: {config['EMAIL_USE_SSL']}\n\n"
                    "Wenn Sie diese E-Mail erhalten, funktioniert die Konfiguration."
                ),
                to=[request.user.email],
            )

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


def anbieter_auswahl_mit_freigabe(choices: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Auswahl der Anbieter; Vorlagen, deren Host nicht in KI_ERLAUBTE_HOSTS steht, sind gekennzeichnet."""
    return [
        (key, label if key not in ANBIETER_VORLAGEN or vorlage_nutzbar(key) else f"{label} (nicht freigegeben)")
        for key, label in choices
    ]


def pruefe_ki_endpunkt(
    form: forms.ModelForm, *, anbieter_feld: str, url_feld: str, anzeigename_feld: str, ort_feld: str
) -> None:
    """
    Gemeinsame Prüfung der Admin-Formulare (KI-Einstellungen, Organisation): Basis-URL bzw. URL der Vorlage
    muss einen Host aus KI_ERLAUBTE_HOSTS haben; beim eigenen Endpunkt sind URL, Anzeigename und
    Verarbeitungsort Pflicht. Eine eingetragene URL wird normalisiert gespeichert.
    """
    data = form.cleaned_data
    anbieter = data.get(anbieter_feld) or ""
    if anbieter not in ANBIETER_VORLAGEN:
        return
    url = (data.get(url_feld) or "").strip()
    if anbieter == EIGENER:
        for feld, text in (
            (url_feld, "Beim eigenen Endpunkt ist die Basis-URL Pflicht."),
            (anzeigename_feld, "Beim eigenen Endpunkt ist der Anzeigename Pflicht."),
            (ort_feld, "Beim eigenen Endpunkt ist der Verarbeitungsort Pflicht."),
        ):
            if not (data.get(feld) or "").strip():
                form.add_error(feld, text)
        if not url:
            return
    vorlage = ANBIETER_VORLAGEN[anbieter]
    try:
        geprueft = pruefe_basis_url(url or vorlage.basis_url)
    except forms.ValidationError as fehler:
        form.add_error(url_feld if url else anbieter_feld, fehler)
        return
    if url:
        data[url_feld] = geprueft


class AISettingsAdminForm(forms.ModelForm):
    """KI-Einstellungen mit Schlüssel, der nur geschrieben wird, und Prüfung gegen die Positivliste."""

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
        self.fields["provider"].choices = anbieter_auswahl_mit_freigabe(list(self.fields["provider"].choices))

    def clean(self):
        cleaned_data = super().clean()
        pruefe_ki_endpunkt(
            self,
            anbieter_feld="provider",
            url_feld="base_url",
            anzeigename_feld="anzeigename",
            ort_feld="verarbeitungsort",
        )
        return cleaned_data

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
    Eine KI-Konfiguration für Work und Bürgerportal (Issue #950).

    Anbieter (Vorlage oder eigener Endpunkt), Schlüssel, Modelle, Schalter je Bereich und Grenzen. Wirksam ist
    eine Adresse nur, wenn ihr Host in der Positivliste KI_ERLAUBTE_HOSTS steht.
    """

    form = AISettingsAdminForm

    fieldsets = (
        (
            "Anbieter",
            {
                "fields": ("provider", "base_url", "anzeigename", "verarbeitungsort", "api_key"),
                "description": (
                    "OpenAI-kompatibler Anbieter mit Verarbeitung in Europa. Ohne Anbieter bleibt die KI aus. "
                    "Freigegeben sind nur Hosts aus der Umgebungsvariable KI_ERLAUBTE_HOSTS (derzeit: {hosts}); "
                    "jede Adresse wird bei jedem Aufruf erneut geprüft."
                ),
            },
        ),
        (
            "Work",
            {
                "fields": ("enabled", "model_name"),
                "description": (
                    "Schreibhilfe und Co-Editor im Dokumenten-Editor. Organisationen mit eigenem Schlüssel "
                    "(Organization → KI) nutzen ihre eigene Konfiguration."
                ),
            },
        ),
        (
            "Bürgerportal",
            {
                "fields": ("insight_enabled", "insight_model", "fallback_model"),
                "description": "Zusammenfassungen, KI-Assistent und KI-Verortung im Bürgerportal.",
            },
        ),
        (
            "Limits",
            {
                "fields": ("max_output_tokens", "insight_max_output_tokens", "default_org_monthly_token_limit"),
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

    def get_fieldsets(self, request, obj=None):
        # Die freigegebenen Hosts stehen in der Beschreibung (Umgebung, nicht im Admin änderbar)
        hosts = ", ".join(erlaubte_hosts())
        return [
            (name, {**optionen, "description": optionen.get("description", "").replace("{hosts}", hosts)})
            for name, optionen in super().get_fieldsets(request, obj)
        ]


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

        from apps.common import mail

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
                if mail.send(
                    kind="plattform.fehlermeldung",
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

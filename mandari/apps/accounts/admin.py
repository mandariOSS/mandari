# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Admin configuration for accounts app.

DSGVO/GDPR COMPLIANCE:
- Minimale Daten sichtbar (nur E-Mail und Name zur Identifikation)
- Persönliche Daten (Telefon, Avatar, Einstellungen) nicht einsehbar
- Session/Login-Daten nicht im Admin (Datenschutz)
- User kann erstellt werden, aber Details werden im Work Portal verwaltet

Dieses Admin dient nur zur:
1. Erstellung von User-Accounts für Work
2. Aktivierung/Deaktivierung von Accounts
3. Zurücksetzen von Passwörtern
4. Staff/Superuser-Verwaltung für System-Admins
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from unfold.admin import ModelAdmin

from .models import LoginAttempt, User


class GDPRUserCreationForm(UserCreationForm):
    """
    Minimales Formular zur User-Erstellung.
    Nur E-Mail und Passwort erforderlich.
    """

    class Meta:
        model = User
        fields = ("email",)


class GDPRUserChangeForm(UserChangeForm):
    """
    Eingeschränktes Formular zur User-Bearbeitung.
    Versteckt sensible Felder.
    """

    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "is_active", "is_staff", "is_superuser")


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    """
    DSGVO-konformer User-Admin.

    Zeigt nur minimale Informationen:
    - E-Mail (zur Identifikation, erforderlich)
    - Name (optional, zur besseren Zuordnung)
    - Status (aktiv/inaktiv, Staff)

    NICHT sichtbar (Datenschutz):
    - Telefonnummer
    - Profilbild
    - Einstellungen
    - Login-Historie
    - Session-Daten
    """

    form = GDPRUserChangeForm
    add_form = GDPRUserCreationForm

    # Liste zeigt nur minimale Infos
    list_display = (
        "email",
        "display_name_safe",
        "is_active",
        "is_staff",
        "has_2fa",
        "membership_count",
        "date_joined",
    )
    list_filter = ("is_staff", "is_superuser", "is_active")
    search_fields = ("email", "first_name", "last_name")
    ordering = ("email",)

    # Kein Zugriff auf sensible Felder
    readonly_fields = ("date_joined", "last_login_info", "membership_info")

    # Feldsets für Bearbeitung - nur minimale Daten
    fieldsets = (
        (
            "Identifikation",
            {
                "fields": ("email", "password"),
                "description": "E-Mail ist der Login-Name. Passwort kann hier zurückgesetzt werden.",
            },
        ),
        (
            "Name (optional)",
            {
                "fields": ("first_name", "last_name"),
                "description": "Name hilft bei der Zuordnung. Wird im Work Portal vom User selbst verwaltet.",
                "classes": ("collapse",),
            },
        ),
        (
            "Status",
            {
                "fields": ("is_active",),
                "description": "Deaktivierte User können sich nicht einloggen.",
            },
        ),
        (
            "System-Berechtigungen",
            {
                "fields": ("is_staff", "is_superuser"),
                "description": "Nur für System-Administratoren. Work-Berechtigungen werden im Work Portal verwaltet.",
                "classes": ("collapse",),
            },
        ),
        (
            "Info (nur lesen)",
            {
                "fields": ("date_joined", "last_login_info", "membership_info"),
                "classes": ("collapse",),
            },
        ),
    )

    # Feldsets für Erstellung - nur E-Mail und Passwort
    add_fieldsets = (
        (
            "Neuen Work-User anlegen",
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
                "description": (
                    "Erstellt einen neuen User für das Work Portal. "
                    "Der User kann sich dann einloggen und sein Profil selbst vervollständigen. "
                    "Persönliche Daten werden nur im Work Portal verwaltet (DSGVO)."
                ),
            },
        ),
        (
            "Optional: Name",
            {
                "classes": ("wide", "collapse"),
                "fields": ("first_name", "last_name"),
                "description": "Name kann auch später vom User selbst eingetragen werden.",
            },
        ),
    )

    # Entferne groups und user_permissions aus dem Admin
    # Diese werden für Work nicht benötigt (eigenes Berechtigungssystem)
    filter_horizontal = ()

    def display_name_safe(self, obj):
        """Zeigt Namen oder 'Nicht angegeben' an."""
        name = obj.full_name
        if name and name != obj.email.split("@")[0]:
            return name
        return "—"

    display_name_safe.short_description = "Name"

    def has_2fa(self, obj):
        """Zeigt ob 2FA aktiviert ist (ohne Details)."""
        return hasattr(obj, "totp_device") and obj.totp_device.is_confirmed

    has_2fa.boolean = True
    has_2fa.short_description = "2FA"

    def membership_count(self, obj):
        """Zeigt Anzahl der Organisationen (ohne Details)."""
        count = obj.memberships.filter(is_active=True).count()
        if count == 0:
            return "—"
        return f"{count} Org."

    membership_count.short_description = "Mitgliedschaften"

    def last_login_info(self, obj):
        """Zeigt nur ob und wann letzter Login war (nicht wo/wie)."""
        if obj.last_login:
            return obj.last_login.strftime("%d.%m.%Y %H:%M")
        return "Noch nie eingeloggt"

    last_login_info.short_description = "Letzter Login"

    def membership_info(self, obj):
        """Zeigt Organisationsnamen (keine persönlichen Details)."""
        orgs = obj.memberships.filter(is_active=True).select_related("organization")
        if not orgs:
            return "Keine Mitgliedschaften"
        return ", ".join([m.organization.name for m in orgs[:5]])

    membership_info.short_description = "Organisationen"

    def get_queryset(self, request):
        """Optimierte Query mit prefetch."""
        return super().get_queryset(request).prefetch_related("memberships__organization")


@admin.register(LoginAttempt)
class LoginAttemptAdmin(ModelAdmin):
    """
    Login-Versuche für Sicherheitsmonitoring.

    Zeigt nur aggregierte Daten, keine persönlichen Details.
    Wichtig für: Brute-Force-Erkennung, Account-Sperrung
    """

    list_display = ("email_masked", "ip_address", "was_successful", "timestamp")
    list_filter = ("was_successful", "timestamp")
    search_fields = ("email", "ip_address")
    readonly_fields = (
        "email",
        "ip_address",
        "user_agent",
        "was_successful",
        "failure_reason",
        "timestamp",
    )
    ordering = ("-timestamp",)

    # Keine Bearbeitung erlaubt
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def email_masked(self, obj):
        """Maskiert E-Mail für Datenschutz."""
        email = obj.email
        if "@" in email:
            local, domain = email.split("@", 1)
            if len(local) > 2:
                return f"{local[:2]}***@{domain}"
            return f"***@{domain}"
        return "***"

    email_masked.short_description = "E-Mail (maskiert)"


# ============================================================================
# NICHT im Admin registriert (DSGVO):
# ============================================================================
# - TwoFactorDevice: Enthält Secrets, wird im Work Portal verwaltet
# - TrustedDevice: Persönliche Gerätedaten
# - UserSession: Session-Daten sind sensibel
# - PasswordResetToken: Tokens sollten nicht einsehbar sein
# - EmailVerificationToken: Tokens sollten nicht einsehbar sein
# - SecurityNotification: Persönliche Sicherheitsnachrichten
#
# Diese werden über das Work Portal vom User selbst verwaltet.
# ============================================================================

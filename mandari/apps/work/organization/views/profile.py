# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Profil und Konto-Sicherheit im Organisationskontext.
"""

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.accounts.services import PasswordService, SessionService, TwoFactorService
from apps.accounts.two_factor_policy import two_factor_required
from apps.common.mixins import WorkViewMixin
from apps.work.faction.models import CalendarFeedToken

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error


class ProfileView(WorkViewMixin, TemplateView):
    """User profile within organization context."""

    template_name = "work/profile/index.html"
    permission_required = "dashboard.view"
    guest_allowed = True  # Konto-Verwaltung auch für Gäste

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None

        user = self.request.user
        is_2fa = TwoFactorService().is_2fa_enabled(user)
        score = 1  # Basis: Konto vorhanden
        if is_2fa:
            score += 1
        if user.email_verified:
            score += 1
        context["security_score"] = score  # max 3
        context["security_max"] = 3
        context["is_2fa_enabled"] = is_2fa
        context["sessions_count"] = SessionService.get_user_sessions(user).count()

        # Persönlicher iCal-Feed (Issue #70): opakes Token, erneuerbar
        feed_token = CalendarFeedToken.for_user(user)
        base_url = getattr(django_settings, "SITE_URL", "").rstrip("/")
        context["calendar_feed_url"] = f"{base_url}/kalender/feed/{feed_token.token}.ics"
        context["calendar_feed_token"] = feed_token
        return context

    def post(self, request, *args, **kwargs):
        """Handle profile updates."""
        action = request.POST.get("action")
        try:
            if action == "update_profile":
                services.update_profile(
                    request.user,
                    first_name=request.POST.get("first_name", "").strip(),
                    last_name=request.POST.get("last_name", "").strip(),
                    phone=request.POST.get("phone", "").strip(),
                    avatar=request.FILES.get("avatar"),
                )
                messages.success(request, "Profil aktualisiert.")
            elif action == "remove_avatar":
                if services.remove_avatar(request.user):
                    messages.success(request, "Profilbild entfernt.")
            elif action == "regenerate_calendar_feed":
                services.regenerate_calendar_feed(request.user)
                messages.success(
                    request,
                    "Kalender-Feed-URL erneuert. Die bisherige URL ist ab sofort ungültig — "
                    "bitte den Feed im Kalender neu abonnieren.",
                )
        except ServiceError as exc:
            flash_error(request, exc)
        return redirect("work:profile", org_slug=self.organization.slug)


class SecurityView(WorkViewMixin, TemplateView):
    """Security settings within organization context."""

    template_name = "work/profile/security.html"
    permission_required = "dashboard.view"
    guest_allowed = True  # Konto-Sicherheit auch für Gäste

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None

        user = self.request.user
        context["is_2fa_enabled"] = TwoFactorService().is_2fa_enabled(user)
        context["two_factor_required"] = two_factor_required(user)

        sessions = SessionService.get_user_sessions(user)
        current_session_key = self.request.session.session_key
        for session in sessions:
            session.is_current = session.session_key == current_session_key
        context["sessions"] = sessions
        context["trusted_devices"] = selectors.trusted_devices(user)

        # Passwortregeln (für den UI-Hinweis)
        context["password_requirements"] = {
            "min_length": PasswordService.MIN_LENGTH,
            "require_uppercase": PasswordService.REQUIRE_UPPERCASE,
            "require_lowercase": PasswordService.REQUIRE_LOWERCASE,
            "require_digit": PasswordService.REQUIRE_DIGIT,
            "require_special": PasswordService.REQUIRE_SPECIAL,
        }
        return context

    def post(self, request, *args, **kwargs):
        """Handle security actions."""
        handler = {
            "change_password": self._change_password,
            "setup_2fa": self._setup_2fa,
            "confirm_2fa": self._confirm_2fa,
            "disable_2fa": self._disable_2fa,
            "regenerate_backup_codes": self._regenerate_backup_codes,
            "revoke_session": self._revoke_session,
            "revoke_all_sessions": self._revoke_all_sessions,
            "remove_trusted_device": self._remove_trusted_device,
        }.get(request.POST.get("action"))
        if handler is not None:
            return handler(request, request.user)
        return self._redirect()

    def _redirect(self):
        return redirect("work:security", org_slug=self.organization.slug)

    def _change_password(self, request, user):
        """Handle password change."""
        old_password = request.POST.get("old_password", "")
        new_password = request.POST.get("new_password", "")
        if new_password != request.POST.get("confirm_password", ""):
            messages.error(request, "Die Passwörter stimmen nicht überein.")
            return self._redirect()

        success, message = PasswordService.change_password(user, old_password, new_password)
        if success:
            update_session_auth_hash(request, user)  # nach Passwortwechsel eingeloggt bleiben
            messages.success(request, message)
        else:
            messages.error(request, message)
        return self._redirect()

    def _setup_2fa(self, request, user):
        """Start 2FA setup."""
        tfa_service = TwoFactorService()
        if tfa_service.is_2fa_enabled(user):
            messages.warning(request, "2FA ist bereits aktiviert.")
            return self._redirect()

        setup_data = tfa_service.setup_2fa(user)
        request.session["2fa_setup"] = {"secret": setup_data["secret"], "backup_codes": setup_data["backup_codes"]}

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "success": True,
                    "qr_code": setup_data["qr_code"],
                    "secret": setup_data["secret"],
                    "backup_codes": setup_data["backup_codes"],
                }
            )
        messages.info(request, "Scannen Sie den QR-Code mit Ihrer Authenticator-App.")
        return self._redirect()

    def _confirm_2fa(self, request, user):
        """Confirm 2FA setup with verification code."""
        if TwoFactorService().confirm_2fa(user, request.POST.get("code", "").strip()):
            request.session.pop("2fa_setup", None)
            messages.success(request, "2FA wurde erfolgreich aktiviert.")
        else:
            messages.error(request, "Ungültiger Code. Bitte versuchen Sie es erneut.")
        return self._redirect()

    def _disable_2fa(self, request, user):
        """Disable 2FA."""
        if two_factor_required(user):
            messages.error(
                request, "Für Ihr Konto ist ein zweiter Faktor vorgeschrieben – er kann nicht deaktiviert werden."
            )
            return self._redirect()
        if not user.check_password(request.POST.get("password", "")):
            messages.error(request, "Passwort ist nicht korrekt.")
            return self._redirect()
        if TwoFactorService().disable_2fa(user):
            messages.success(request, "2FA wurde deaktiviert.")
        else:
            messages.error(request, "Fehler beim Deaktivieren von 2FA.")
        return self._redirect()

    def _regenerate_backup_codes(self, request, user):
        """Regenerate backup codes."""
        if not user.check_password(request.POST.get("password", "")):
            messages.error(request, "Passwort ist nicht korrekt.")
            return self._redirect()

        codes = TwoFactorService().regenerate_backup_codes(user)
        if codes:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"success": True, "backup_codes": codes})
            messages.success(request, f"Neue Backup-Codes generiert: {', '.join(codes)}")
        else:
            messages.error(request, "Fehler beim Generieren der Backup-Codes.")
        return self._redirect()

    def _revoke_session(self, request, user):
        """Revoke a specific session."""
        session_key = request.POST.get("session_key", "")
        if session_key == request.session.session_key:
            messages.error(request, "Die aktuelle Sitzung kann hier nicht beendet werden.")
            return self._redirect()
        if SessionService.revoke_session(user, session_key):
            messages.success(request, "Sitzung wurde beendet.")
        else:
            messages.error(request, "Sitzung konnte nicht gefunden werden.")
        return self._redirect()

    def _revoke_all_sessions(self, request, user):
        """Revoke all other sessions."""
        count = SessionService.revoke_all_sessions(user, except_current=request.session.session_key)
        if count > 0:
            messages.success(request, f"{count} Sitzung(en) wurden beendet.")
        else:
            messages.info(request, "Keine anderen Sitzungen vorhanden.")
        return self._redirect()

    def _remove_trusted_device(self, request, user):
        """Remove a trusted device."""
        if services.remove_trusted_device(user, request.POST.get("device_id", "")):
            messages.success(request, "Gerät wurde entfernt.")
        else:
            messages.error(request, "Gerät nicht gefunden.")
        return self._redirect()

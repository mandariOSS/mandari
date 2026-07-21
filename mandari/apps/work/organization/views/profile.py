# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from apps.accounts.services import PasswordService, SessionService, TwoFactorService
from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


# =============================================================================
# USER PROFILE
# =============================================================================


class ProfileView(WorkViewMixin, TemplateView):
    """User profile within organization context."""

    template_name = "work/profile/index.html"
    permission_required = "dashboard.view"
    guest_allowed = True  # Konto-Verwaltung auch für Gäste

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None

        user = self.request.user
        tfa_service = TwoFactorService()

        # Security quick-status
        is_2fa = tfa_service.is_2fa_enabled(user)
        sessions_count = SessionService.get_user_sessions(user).count()
        score = 1  # base: has account
        if is_2fa:
            score += 1
        if user.email_verified:
            score += 1
        # max 3
        context["security_score"] = score
        context["security_max"] = 3
        context["is_2fa_enabled"] = is_2fa
        context["sessions_count"] = sessions_count

        # Persönlicher iCal-Feed (Issue #70): opakes Token, erneuerbar
        from django.conf import settings as django_settings

        from apps.work.faction.models import CalendarFeedToken

        feed_token = CalendarFeedToken.for_user(user)
        base_url = getattr(django_settings, "SITE_URL", "").rstrip("/")
        context["calendar_feed_url"] = f"{base_url}/kalender/feed/{feed_token.token}.ics"
        context["calendar_feed_token"] = feed_token

        return context

    def post(self, request, *args, **kwargs):
        """Handle profile updates."""
        user = request.user
        action = request.POST.get("action")

        if action == "update_profile":
            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()
            phone = request.POST.get("phone", "").strip()

            user.first_name = first_name
            user.last_name = last_name
            user.phone = phone

            # Handle avatar upload
            if "avatar" in request.FILES:
                avatar_file = request.FILES["avatar"]
                # Validate file type
                allowed_types = ["image/jpeg", "image/png", "image/webp"]
                if avatar_file.content_type in allowed_types and avatar_file.size <= 5 * 1024 * 1024:
                    # Delete old avatar if exists
                    if user.avatar:
                        user.avatar.delete(save=False)
                    user.avatar = avatar_file
                else:
                    messages.error(request, "Bild muss JPG, PNG oder WebP sein und max. 5 MB groß.")
                    return redirect("work:profile", org_slug=self.organization.slug)

            user.save()
            messages.success(request, "Profil aktualisiert.")

        elif action == "remove_avatar":
            if user.avatar:
                user.avatar.delete(save=False)
                user.avatar = None
                user.save()
                messages.success(request, "Profilbild entfernt.")

        elif action == "regenerate_calendar_feed":
            # Persönlichen iCal-Feed-Token erneuern (Issue #70) — die
            # bisherige Feed-URL wird sofort ungültig
            from apps.work.faction.models import CalendarFeedToken

            CalendarFeedToken.for_user(user).regenerate()
            messages.success(
                request,
                "Kalender-Feed-URL erneuert. Die bisherige URL ist ab sofort ungültig — "
                "bitte den Feed im Kalender neu abonnieren.",
            )

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
        tfa_service = TwoFactorService()

        # 2FA status
        context["is_2fa_enabled"] = tfa_service.is_2fa_enabled(user)

        # Get active sessions
        sessions = SessionService.get_user_sessions(user)
        current_session_key = self.request.session.session_key

        for session in sessions:
            session.is_current = session.session_key == current_session_key

        context["sessions"] = sessions

        # Get trusted devices
        from apps.accounts.models import TrustedDevice

        context["trusted_devices"] = TrustedDevice.objects.filter(user=user, expires_at__gt=timezone.now()).order_by(
            "-last_used_at"
        )

        # Password strength (for UI hint)
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
        action = request.POST.get("action")
        user = request.user

        if action == "change_password":
            return self._change_password(request, user)
        elif action == "setup_2fa":
            return self._setup_2fa(request, user)
        elif action == "confirm_2fa":
            return self._confirm_2fa(request, user)
        elif action == "disable_2fa":
            return self._disable_2fa(request, user)
        elif action == "regenerate_backup_codes":
            return self._regenerate_backup_codes(request, user)
        elif action == "revoke_session":
            return self._revoke_session(request, user)
        elif action == "revoke_all_sessions":
            return self._revoke_all_sessions(request, user)
        elif action == "remove_trusted_device":
            return self._remove_trusted_device(request, user)

        return redirect("work:security", org_slug=self.organization.slug)

    def _change_password(self, request, user):
        """Handle password change."""
        old_password = request.POST.get("old_password", "")
        new_password = request.POST.get("new_password", "")
        confirm_password = request.POST.get("confirm_password", "")

        if new_password != confirm_password:
            messages.error(request, "Die Passwörter stimmen nicht überein.")
            return redirect("work:security", org_slug=self.organization.slug)

        success, message = PasswordService.change_password(user, old_password, new_password)

        if success:
            # Keep user logged in after password change
            update_session_auth_hash(request, user)
            messages.success(request, message)
        else:
            messages.error(request, message)

        return redirect("work:security", org_slug=self.organization.slug)

    def _setup_2fa(self, request, user):
        """Start 2FA setup."""
        tfa_service = TwoFactorService()

        if tfa_service.is_2fa_enabled(user):
            messages.warning(request, "2FA ist bereits aktiviert.")
            return redirect("work:security", org_slug=self.organization.slug)

        # Generate setup data
        setup_data = tfa_service.setup_2fa(user)

        # Store in session for confirmation step
        request.session["2fa_setup"] = {
            "secret": setup_data["secret"],
            "backup_codes": setup_data["backup_codes"],
        }

        # Return JSON for HTMX/Alpine
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "success": True,
                    "qr_code": setup_data["qr_code"],
                    "secret": setup_data["secret"],
                    "backup_codes": setup_data["backup_codes"],
                }
            )

        # Store data for template
        messages.info(request, "Scannen Sie den QR-Code mit Ihrer Authenticator-App.")
        return redirect("work:security", org_slug=self.organization.slug)

    def _confirm_2fa(self, request, user):
        """Confirm 2FA setup with verification code."""
        code = request.POST.get("code", "").strip()
        tfa_service = TwoFactorService()

        if tfa_service.confirm_2fa(user, code):
            # Clear session data
            request.session.pop("2fa_setup", None)
            messages.success(request, "2FA wurde erfolgreich aktiviert.")
        else:
            messages.error(request, "Ungültiger Code. Bitte versuchen Sie es erneut.")

        return redirect("work:security", org_slug=self.organization.slug)

    def _disable_2fa(self, request, user):
        """Disable 2FA."""
        password = request.POST.get("password", "")

        if not user.check_password(password):
            messages.error(request, "Passwort ist nicht korrekt.")
            return redirect("work:security", org_slug=self.organization.slug)

        tfa_service = TwoFactorService()
        if tfa_service.disable_2fa(user):
            messages.success(request, "2FA wurde deaktiviert.")
        else:
            messages.error(request, "Fehler beim Deaktivieren von 2FA.")

        return redirect("work:security", org_slug=self.organization.slug)

    def _regenerate_backup_codes(self, request, user):
        """Regenerate backup codes."""
        password = request.POST.get("password", "")

        if not user.check_password(password):
            messages.error(request, "Passwort ist nicht korrekt.")
            return redirect("work:security", org_slug=self.organization.slug)

        tfa_service = TwoFactorService()
        codes = tfa_service.regenerate_backup_codes(user)

        if codes:
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"success": True, "backup_codes": codes})

            messages.success(request, f"Neue Backup-Codes generiert: {', '.join(codes)}")
        else:
            messages.error(request, "Fehler beim Generieren der Backup-Codes.")

        return redirect("work:security", org_slug=self.organization.slug)

    def _revoke_session(self, request, user):
        """Revoke a specific session."""
        session_key = request.POST.get("session_key", "")

        # Don't allow revoking current session via this method
        if session_key == request.session.session_key:
            messages.error(request, "Die aktuelle Sitzung kann hier nicht beendet werden.")
            return redirect("work:security", org_slug=self.organization.slug)

        if SessionService.revoke_session(user, session_key):
            messages.success(request, "Sitzung wurde beendet.")
        else:
            messages.error(request, "Sitzung konnte nicht gefunden werden.")

        return redirect("work:security", org_slug=self.organization.slug)

    def _revoke_all_sessions(self, request, user):
        """Revoke all other sessions."""
        current_key = request.session.session_key
        count = SessionService.revoke_all_sessions(user, except_current=current_key)

        if count > 0:
            messages.success(request, f"{count} Sitzung(en) wurden beendet.")
        else:
            messages.info(request, "Keine anderen Sitzungen vorhanden.")

        return redirect("work:security", org_slug=self.organization.slug)

    def _remove_trusted_device(self, request, user):
        """Remove a trusted device."""
        from apps.accounts.models import TrustedDevice

        device_id = request.POST.get("device_id", "")

        try:
            device = TrustedDevice.objects.get(id=device_id, user=user)
            device.delete()
            messages.success(request, "Gerät wurde entfernt.")
        except TrustedDevice.DoesNotExist:
            messages.error(request, "Gerät nicht gefunden.")

        return redirect("work:security", org_slug=self.organization.slug)

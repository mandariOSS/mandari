# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Authentication views for Mandari.

Provides views for:
- Login / Logout
- Password reset flow
"""

import contextlib
import time
from datetime import timedelta
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.views import (
    PasswordResetCompleteView as DjangoPasswordResetCompleteView,
)
from django.contrib.auth.views import (
    PasswordResetConfirmView as DjangoPasswordResetConfirmView,
)
from django.contrib.auth.views import (
    PasswordResetDoneView as DjangoPasswordResetDoneView,
)
from django.contrib.auth.views import (
    PasswordResetView as DjangoPasswordResetView,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from . import webauthn_service
from .forms import LoginForm, PasswordResetForm, RegistrationForm, SetPasswordForm
from .models import LoginAttempt
from .services import SessionService, TwoFactorService
from .two_factor_policy import (
    POLICY_CACHE_SESSION_KEY,
    security_key_required,
    two_factor_reasons,
    two_factor_required,
)

# Zweiter Anmeldeschritt: Passwort ist geprüft, angemeldet wird erst nach gültigem Code
PENDING_2FA_SESSION_KEY = "auth_2fa_pending"
PENDING_2FA_MAX_AGE_SECONDS = 300
MAX_2FA_FAILURES = 5
# Pflicht-Einrichtung nach dem Passwort: etwas mehr Zeit für App-Installation und Scan
PENDING_ENROLL_MAX_AGE_SECONDS = 900
ENROLL_SETUP_SESSION_KEY = "auth_2fa_enroll"


def complete_login(request, user, remember: bool) -> None:
    """Anmeldung abschließen: Session-Rotation, Laufzeit, Geräteliste."""
    auth_login(request, user)
    # Ohne „Angemeldet bleiben" endet die Session mit dem Browser, sonst nach 30 Tagen
    request.session.set_expiry(60 * 60 * 24 * 30 if remember else 0)
    if request.session.session_key:
        SessionService.create_session(user, request, request.session.session_key)


class LoginView(View):
    """
    Custom login view with rate limiting and audit logging.
    """

    template_name = "accounts/login.html"

    def get(self, request):
        # If already authenticated, redirect
        if request.user.is_authenticated:
            return redirect(self.get_success_url(request))

        form = LoginForm()
        next_url = request.GET.get("next", "")

        # Check for pending invitation
        invitation = self._get_pending_invitation(request)

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "next": next_url,
                "invitation": invitation,
            },
        )

    def post(self, request):
        form = LoginForm(request.POST, request=request)
        next_url = request.POST.get("next", "")

        # Rate limiting check
        ip_address = self.get_client_ip(request)
        email = request.POST.get("email", "")

        # Check for pending invitation
        invitation = self._get_pending_invitation(request)

        if self.is_rate_limited(ip_address, email):
            messages.error(request, "Zu viele fehlgeschlagene Anmeldeversuche. Bitte warte 15 Minuten.")
            return render(
                request,
                self.template_name,
                {
                    "form": form,
                    "next": next_url,
                    "invitation": invitation,
                },
            )

        if form.is_valid():
            user = form.get_user()

            remember = bool(form.cleaned_data.get("remember_me"))

            if TwoFactorService().is_2fa_enabled(user):
                # Noch NICHT anmelden: erst nach gültigem Code (LoginTwoFactorView)
                request.session[PENDING_2FA_SESSION_KEY] = {
                    "user_id": str(user.pk),
                    "remember": remember,
                    "next": next_url or "",
                    "started": int(time.time()),
                }
                request.session.cycle_key()
                return redirect("accounts:login_2fa")

            if two_factor_required(user):
                # 2FA-Pflicht ohne eingerichteten Faktor: erst einrichten, dann anmelden
                request.session[PENDING_2FA_SESSION_KEY] = {
                    "user_id": str(user.pk),
                    "remember": remember,
                    "next": next_url or "",
                    "started": int(time.time()),
                    "enroll": True,
                }
                request.session.cycle_key()
                return redirect("accounts:two_factor_enroll")

            self.log_attempt(request, email, success=True)
            complete_login(request, user, remember)

            messages.success(request, "Erfolgreich angemeldet.")

            return redirect(self.get_success_url(request, next_url))
        # Log failed attempt
        self.log_attempt(request, email, success=False)

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "next": next_url,
                "invitation": invitation,
            },
        )

    def get_success_url(self, request, next_url=None):
        """Determine where to redirect after login."""
        # Validate next_url against open redirect attacks
        if next_url:
            from django.utils.http import url_has_allowed_host_and_scheme

            if url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                return next_url

        # Default: redirect to work portal if user has memberships
        if hasattr(request.user, "memberships"):
            active_memberships = request.user.memberships.filter(is_active=True)
            if active_memberships.exists():
                first_org = active_memberships.first().organization
                return f"/work/{first_org.slug}/"

        # Fallback to home
        return "/"

    def is_safe_url(self, url, request):
        """Check if URL is safe for redirect."""
        from django.utils.http import url_has_allowed_host_and_scheme

        return url_has_allowed_host_and_scheme(
            url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )

    def get_client_ip(self, request):
        """Get client IP address."""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "")

    def is_rate_limited(self, ip_address, email):
        """Check if login attempts are rate limited."""
        from datetime import timedelta

        from django.utils import timezone

        # Allow 5 attempts per 15 minutes
        threshold = timezone.now() - timedelta(minutes=15)
        recent_failures = LoginAttempt.objects.filter(
            ip_address=ip_address,
            was_successful=False,
            timestamp__gte=threshold,
        ).count()

        return recent_failures >= 5

    def log_attempt(self, request, email, success):
        """Log login attempt for security monitoring."""
        # Login darf nicht scheitern, wenn das Protokollieren fehlschlägt
        with contextlib.suppress(Exception):
            LoginAttempt.objects.create(
                email=email,
                ip_address=self.get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
                was_successful=success,
                failure_reason="" if success else "invalid_credentials",
            )

    def _get_pending_invitation(self, request):
        """Get pending invitation from session if any."""
        token = request.session.get("pending_invitation_token")
        if not token:
            return None

        from django.utils import timezone

        from apps.tenants.models import UserInvitation

        try:
            return UserInvitation.objects.get(token=token, accepted_at__isnull=True, expires_at__gt=timezone.now())
        except UserInvitation.DoesNotExist:
            # Clear invalid token
            request.session.pop("pending_invitation_token", None)
            return None


class LoginTwoFactorView(View):
    """Zweiter Anmeldeschritt: TOTP-Code aus der Authenticator-App oder Backup-Code."""

    template_name = "accounts/login_2fa.html"

    def _pending(self, request):
        data = request.session.get(PENDING_2FA_SESSION_KEY)
        if not isinstance(data, dict) or data.get("enroll"):
            return None, None
        if int(time.time()) - int(data.get("started", 0)) > PENDING_2FA_MAX_AGE_SECONDS:
            request.session.pop(PENDING_2FA_SESSION_KEY, None)
            return None, None
        from .models import User

        user = User.objects.filter(pk=data.get("user_id"), is_active=True).first()
        if user is None:
            request.session.pop(PENDING_2FA_SESSION_KEY, None)
            return None, None
        return data, user

    def _expired(self, request):
        messages.error(request, "Die Anmeldung ist abgelaufen. Bitte melde dich erneut an.")
        return redirect("accounts:login")

    def _recent_failures(self, user) -> int:
        return LoginAttempt.objects.filter(
            email=user.email,
            was_successful=False,
            failure_reason="invalid_2fa",
            timestamp__gte=timezone.now() - timedelta(minutes=15),
        ).count()

    def _log(self, request, user, success: bool) -> None:
        # Anmeldung darf nicht scheitern, wenn das Protokollieren fehlschlägt
        with contextlib.suppress(Exception):
            LoginAttempt.objects.create(
                email=user.email,
                ip_address=LoginView().get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
                was_successful=success,
                failure_reason="" if success else "invalid_2fa",
            )

    def get(self, request):
        _data, user = self._pending(request)
        if user is None:
            return self._expired(request)
        return render(
            request, self.template_name, {"error": None, "has_security_key": webauthn_service.has_credentials(user)}
        )

    def post(self, request):
        data, user = self._pending(request)
        if user is None:
            return self._expired(request)

        if self._recent_failures(user) >= MAX_2FA_FAILURES:
            request.session.pop(PENDING_2FA_SESSION_KEY, None)
            messages.error(
                request, "Zu viele fehlgeschlagene Versuche. Bitte warte 15 Minuten und melde dich erneut an."
            )
            return redirect("accounts:login")

        code = request.POST.get("code", "")
        has_security_key = webauthn_service.has_credentials(user)
        if security_key_required(user) and has_security_key and code.replace(" ", "").isdigit():
            # Pflicht zum Sicherheitsschlüssel: App-Codes gelten nicht, Backup-Codes bleiben der Rückfall
            return render(
                request,
                self.template_name,
                {
                    "error": "Für dieses Konto ist die Anmeldung mit Sicherheitsschlüssel vorgeschrieben. "
                    "Ohne Schlüssel hilft ein Backup-Code.",
                    "has_security_key": has_security_key,
                },
            )

        if TwoFactorService().verify_2fa(user, code):
            request.session.pop(PENDING_2FA_SESSION_KEY, None)
            self._log(request, user, success=True)
            complete_login(request, user, bool(data.get("remember")))
            messages.success(request, "Erfolgreich angemeldet.")
            return redirect(LoginView().get_success_url(request, data.get("next") or None))

        self._log(request, user, success=False)
        return render(
            request,
            self.template_name,
            {"error": "Der Code ist ungültig oder abgelaufen.", "has_security_key": has_security_key},
        )


class TwoFactorEnrollView(View):
    """
    Pflicht-Einrichtung des zweiten Faktors (Authenticator-App).

    Zwei Wege führen hierher: direkt nach geprüftem Passwort – angemeldet wird
    erst nach der Einrichtung – oder über die Middleware für bereits angemeldete
    Konten, die die Pflicht noch nicht erfüllen. Freiwillige Einrichtung ist
    ebenfalls möglich.
    """

    template_name = "accounts/two_factor_enroll.html"

    def _subject(self, request):
        """Konto und ausstehende Anmeldung (``None``, wenn bereits angemeldet)."""
        if request.user.is_authenticated:
            return request.user, None
        data = request.session.get(PENDING_2FA_SESSION_KEY)
        if not isinstance(data, dict) or not data.get("enroll"):
            return None, None
        if int(time.time()) - int(data.get("started", 0)) > PENDING_ENROLL_MAX_AGE_SECONDS:
            request.session.pop(PENDING_2FA_SESSION_KEY, None)
            return None, None
        from .models import User

        user = User.objects.filter(pk=data.get("user_id"), is_active=True).first()
        if user is None:
            request.session.pop(PENDING_2FA_SESSION_KEY, None)
            return None, None
        return user, data

    def _setup(self, request, user) -> dict:
        """Secret und Backup-Codes dieser Einrichtung (bleiben beim Neuladen erhalten)."""
        setup = request.session.get(ENROLL_SETUP_SESSION_KEY)
        if isinstance(setup, dict) and setup.get("user_id") == str(user.pk):
            return setup
        data = TwoFactorService().setup_2fa(user)
        setup = {
            "user_id": str(user.pk),
            "secret": data["secret"],
            "backup_codes": data["backup_codes"],
            "confirmed": False,
        }
        request.session[ENROLL_SETUP_SESSION_KEY] = setup
        return setup

    def _next(self, request, pending) -> str:
        if pending is not None:
            return pending.get("next") or ""
        return request.POST.get("next") or request.GET.get("next") or ""

    def _expired(self, request):
        messages.error(request, "Die Anmeldung ist abgelaufen. Bitte melde dich erneut an.")
        return redirect("accounts:login")

    def _render_scan(self, request, user, pending, setup, error=None, status=200):
        service = TwoFactorService()
        context = {
            "step": "scan",
            "qr_code": service.generate_qr_code(service.get_totp_uri(user, setup["secret"])),
            "secret": setup["secret"],
            "reasons": two_factor_reasons(user),
            "pending": pending is not None,
            "next": self._next(request, pending),
            "error": error,
        }
        return render(request, self.template_name, context, status=status)

    def _render_codes(self, request, pending, setup):
        context = {
            "step": "codes",
            "backup_codes": setup.get("backup_codes", []),
            "pending": pending is not None,
            "next": self._next(request, pending),
        }
        return render(request, self.template_name, context)

    def get(self, request):
        user, pending = self._subject(request)
        if user is None:
            return self._expired(request)
        if TwoFactorService().is_2fa_enabled(user):
            setup = request.session.get(ENROLL_SETUP_SESSION_KEY)
            if isinstance(setup, dict) and setup.get("user_id") == str(user.pk) and setup.get("confirmed"):
                return self._render_codes(request, pending, setup)
            if pending is not None:
                # Inzwischen anderweitig eingerichtet: regulärer Code-Schritt
                pending.pop("enroll", None)
                request.session[PENDING_2FA_SESSION_KEY] = pending
                return redirect("accounts:login_2fa")
            return redirect(LoginView().get_success_url(request, self._next(request, pending) or None))
        return self._render_scan(request, user, pending, self._setup(request, user))

    def post(self, request):
        user, pending = self._subject(request)
        if user is None:
            return self._expired(request)
        service = TwoFactorService()

        if request.POST.get("action") == "finish":
            if not service.is_2fa_enabled(user):
                return redirect("accounts:two_factor_enroll")
            next_url = self._next(request, pending)
            request.session.pop(ENROLL_SETUP_SESSION_KEY, None)
            request.session.pop(POLICY_CACHE_SESSION_KEY, None)
            if pending is not None:
                request.session.pop(PENDING_2FA_SESSION_KEY, None)
                LoginTwoFactorView()._log(request, user, success=True)
                complete_login(request, user, bool(pending.get("remember")))
                messages.success(request, "Zweiter Faktor eingerichtet – du bist angemeldet.")
            else:
                messages.success(request, "Zweiter Faktor eingerichtet.")
            return redirect(LoginView().get_success_url(request, next_url or None))

        setup = self._setup(request, user)
        if LoginTwoFactorView()._recent_failures(user) >= MAX_2FA_FAILURES:
            if pending is not None:
                request.session.pop(PENDING_2FA_SESSION_KEY, None)
                messages.error(
                    request, "Zu viele fehlgeschlagene Versuche. Bitte warte 15 Minuten und melde dich erneut an."
                )
                return redirect("accounts:login")
            return self._render_scan(
                request,
                user,
                pending,
                setup,
                error="Zu viele fehlgeschlagene Versuche. Bitte warte 15 Minuten.",
                status=429,
            )

        if service.confirm_2fa(user, request.POST.get("code", "")):
            setup["confirmed"] = True
            request.session[ENROLL_SETUP_SESSION_KEY] = setup
            request.session.pop(POLICY_CACHE_SESSION_KEY, None)
            return self._render_codes(request, pending, setup)

        LoginTwoFactorView()._log(request, user, success=False)
        return self._render_scan(
            request,
            user,
            pending,
            setup,
            error="Der Code ist ungültig. Prüfe die Uhrzeit deines Geräts und versuche es erneut.",
        )


def admin_login_redirect(request):
    """Admin-Anmeldung immer über die eigene Anmeldung (inkl. zweitem Faktor)."""
    next_url = request.GET.get("next") or "/admin/"
    return redirect(f"{reverse('accounts:login')}?{urlencode({'next': next_url})}")


class LogoutView(View):
    """
    Custom logout view with confirmation.
    """

    template_name = "accounts/logout.html"

    def get(self, request):
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        return render(request, self.template_name)

    def post(self, request):
        # Clean up UserSession before logout destroys session_key
        session_key = request.session.session_key
        if session_key and request.user.is_authenticated:
            from .models import UserSession

            UserSession.objects.filter(session_key=session_key).delete()

        auth_logout(request)
        return redirect("accounts:logged_out")


class LoggedOutView(TemplateView):
    """View shown after successful logout."""

    template_name = "accounts/logged_out.html"


# =============================================================================
# Password Reset Views (using Django's built-in views with custom templates)
# =============================================================================


class PasswordResetView(DjangoPasswordResetView):
    """Request password reset."""

    template_name = "accounts/password_reset.html"
    # Text- und HTML-Fassung; der Versand läuft über PasswordResetForm.send_mail
    # (Basis-Layout + Inliner, Issue #175)
    email_template_name = "accounts/emails/password_reset.txt"
    html_email_template_name = "accounts/emails/password_reset.html"
    subject_template_name = "accounts/emails/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")
    form_class = PasswordResetForm

    def form_valid(self, form):
        # Always show success message (don't reveal if email exists)
        return super().form_valid(form)


class PasswordResetDoneView(DjangoPasswordResetDoneView):
    """Password reset email sent confirmation."""

    template_name = "accounts/password_reset_done.html"


class PasswordResetConfirmView(DjangoPasswordResetConfirmView):
    """Set new password after reset."""

    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")
    form_class = SetPasswordForm


class PasswordResetCompleteView(DjangoPasswordResetCompleteView):
    """Password reset complete confirmation."""

    template_name = "accounts/password_reset_complete.html"


# =============================================================================
# Registration (for invited users)
# =============================================================================


class RegisterView(View):
    """
    Registration view for invited users.

    Only allows registration if there's a pending invitation token in session.
    """

    template_name = "accounts/register.html"

    def get(self, request):
        # Check for pending invitation
        invitation_token = request.session.get("pending_invitation_token")
        if not invitation_token:
            messages.error(request, "Registrierung ist nur mit einer Einladung möglich.")
            return redirect("accounts:login")

        # Get invitation
        invitation = self.get_invitation(invitation_token)
        if not invitation:
            messages.error(request, "Einladung nicht gefunden oder abgelaufen.")
            request.session.pop("pending_invitation_token", None)
            return redirect("accounts:login")

        # Check if email already has an account
        from .models import User

        if User.objects.filter(email=invitation.email).exists():
            messages.info(request, "Ein Konto mit dieser E-Mail existiert bereits. Bitte melden Sie sich an.")
            return redirect("accounts:login")

        form = RegistrationForm(email=invitation.email)

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "invitation": invitation,
            },
        )

    def post(self, request):
        # Check for pending invitation
        invitation_token = request.session.get("pending_invitation_token")
        if not invitation_token:
            messages.error(request, "Registrierung ist nur mit einer Einladung möglich.")
            return redirect("accounts:login")

        # Get invitation
        invitation = self.get_invitation(invitation_token)
        if not invitation:
            messages.error(request, "Einladung nicht gefunden oder abgelaufen.")
            request.session.pop("pending_invitation_token", None)
            return redirect("accounts:login")

        form = RegistrationForm(request.POST, email=invitation.email)

        if form.is_valid():
            # Konto anlegen – der Einladungslink kam per E-Mail, die Adresse ist damit bestätigt
            user = form.save()
            user.email_verified = True
            user.save(update_fields=["email_verified"])

            # Log the user in
            auth_login(request, user)

            # Track session
            if request.session.session_key:
                SessionService.create_session(user, request, request.session.session_key)

            # Redirect to accept invitation
            messages.success(request, f"Willkommen, {user.first_name}! Ihr Konto wurde erstellt.")
            return redirect("work:accept_invitation", token=invitation_token)

        return render(
            request,
            self.template_name,
            {
                "form": form,
                "invitation": invitation,
            },
        )

    def get_invitation(self, token):
        """Get and validate the invitation."""
        from django.utils import timezone

        from apps.tenants.models import UserInvitation

        try:
            return UserInvitation.objects.get(token=token, accepted_at__isnull=True, expires_at__gt=timezone.now())
        except UserInvitation.DoesNotExist:
            return None


SELF_REGISTER_MAILS_PER_IP_PER_HOUR = 10
SELF_REGISTER_MAILS_PER_ADDRESS_PER_HOUR = 3


def _registration_mail_allowed(request, email: str) -> bool:
    """Bestätigungsmails je IP und Adresse drosseln – die Registrierung darf kein Werkzeug für Mail-Fluten sein."""
    import hashlib

    from django.core.cache import cache

    from .two_factor_policy import client_ip

    address_key = hashlib.sha256(email.lower().encode()).hexdigest()
    limits = {
        f"self-register:ip:{client_ip(request)}": SELF_REGISTER_MAILS_PER_IP_PER_HOUR,
        f"self-register:address:{address_key}": SELF_REGISTER_MAILS_PER_ADDRESS_PER_HOUR,
    }
    if any(cache.get(key, 0) >= limit for key, limit in limits.items()):
        return False
    for key in limits:
        cache.add(key, 0, 60 * 60)
        cache.incr(key)
    return True


def registration_state_response(request, org, membership):
    """Antwort für eine bestehende Mitgliedschaft: aktiv, Anfrage offen oder deaktiviert."""
    if membership.is_active:
        messages.info(request, f"Du bist bereits Mitglied bei {org.name}.")
        if request.user.is_authenticated:
            return redirect("work:dashboard", org_slug=org.slug)
        dashboard = reverse("work:dashboard", kwargs={"org_slug": org.slug})
        return redirect(f"{reverse('accounts:login')}?next={quote(dashboard)}")
    if membership.registration_requested_at:
        return render(
            request,
            "accounts/registration_pending.html",
            {"org": org, "state": "pending", "email": membership.user.email},
        )
    messages.error(request, f"Dein Zugang zu {org.name} ist derzeit deaktiviert. Bitte wende dich an die Organisation.")
    return redirect("accounts:login")


class SelfRegisterView(View):
    """
    Selbstregistrierung für Organisationen mit aktivierter Registrierung.

    URL: /accounts/register/<org_slug>/
    Ablauf: Formular → Bestätigungslink per E-Mail → erst nach dem Klick entsteht die Mitgliedschaft
    (sofort aktiv oder als offene Anfrage). Die Registrierung meldet nicht an; bereits angemeldete
    Konten mit bestätigter Adresse treten direkt bei.
    """

    template_name = "accounts/self_register.html"

    def dispatch(self, request, *args, **kwargs):
        from apps.tenants.models import Membership, Organization

        self.org = get_object_or_404(Organization, slug=kwargs["org_slug"], is_active=True)
        if not self.org.registration_enabled:
            messages.error(request, "Selbstregistrierung ist für diese Organisation nicht aktiviert.")
            return redirect("accounts:login")

        if request.user.is_authenticated:
            membership = Membership.objects.filter(user=request.user, organization=self.org).first()
            if membership is not None:
                return registration_state_response(request, self.org, membership)

        return super().dispatch(request, *args, **kwargs)

    def get(self, request, **kwargs):
        from .forms import SelfRegistrationForm

        form = SelfRegistrationForm(org=self.org, user=request.user if request.user.is_authenticated else None)
        return render(request, self.template_name, {"form": form, "org": self.org})

    def post(self, request, **kwargs):
        from apps.work.organization import services as organization_services

        from .forms import SelfRegistrationForm
        from .models import User

        form = SelfRegistrationForm(
            request.POST,
            org=self.org,
            user=request.user if request.user.is_authenticated else None,
        )
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "org": self.org})

        # Angemeldet liefert das Formular immer die Adresse des Kontos
        email = form.cleaned_data["email"]
        if not self.org.is_email_allowed_for_registration(email):
            domains = self.org.registration_email_domains
            form.add_error("email", f"Nur E-Mail-Adressen mit folgenden Domains sind erlaubt: {', '.join(domains)}")
            return render(request, self.template_name, {"form": form, "org": self.org})

        # Ein bestehendes Konto wird nie ohne Anmeldung übernommen (sonst Kontoübernahme per E-Mail-Adresse)
        if not request.user.is_authenticated and User.objects.filter(email__iexact=email).exists():
            messages.info(
                request,
                "Für diese E-Mail-Adresse besteht bereits ein Konto. Bitte melde dich an, um beizutreten.",
            )
            return redirect(f"{reverse('accounts:login')}?next={quote(request.path)}")

        if request.user.is_authenticated and request.user.email_verified:
            membership = organization_services.join_by_self_registration(self.org, request.user)
            if membership.is_active:
                messages.success(request, f"Willkommen bei {self.org.name}!")
                return redirect("work:dashboard", org_slug=self.org.slug)
            return registration_state_response(request, self.org, membership)

        if not _registration_mail_allowed(request, email):
            form.add_error(None, "Gerade wurden zu viele Registrierungen angefordert. Bitte versuche es später erneut.")
            return render(request, self.template_name, {"form": form, "org": self.org})

        if request.user.is_authenticated:
            user = request.user
        else:
            user = User.objects.create_user(
                email=email,
                password=form.cleaned_data["password1"],
                first_name=form.cleaned_data["first_name"],
                last_name=form.cleaned_data["last_name"],
            )
        mail_sent = organization_services.start_self_registration(self.org, user)
        return render(
            request,
            "accounts/registration_pending.html",
            {"org": self.org, "state": "confirm", "email": user.email, "mail_failed": not mail_sent},
        )


class SelfRegisterConfirmView(View):
    """
    Bestätigungslink der Selbstregistrierung.

    GET zeigt nur die Bestätigung – Link-Scanner in Mailprogrammen rufen Links vorab auf. Erst POST
    legt die Mitgliedschaft an. Der Link meldet nicht an; die Anmeldung läuft wie immer über Passwort
    und gegebenenfalls zweiten Faktor.
    """

    template_name = "accounts/self_register_confirm.html"

    def dispatch(self, request, *args, **kwargs):
        from apps.tenants.models import Organization
        from apps.work.organization import selectors as organization_selectors

        self.org = get_object_or_404(Organization, slug=kwargs["org_slug"], is_active=True)
        self.token = organization_selectors.find_registration_token(kwargs["token"])
        if self.token is None:
            return render(request, self.template_name, {"org": self.org, "invalid": True}, status=404)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, **kwargs):
        return render(request, self.template_name, {"org": self.org, "email": self.token.email})

    def post(self, request, **kwargs):
        from apps.work.organization import services as organization_services

        try:
            membership = organization_services.confirm_self_registration(self.org, self.token)
        except organization_services.ServiceError as exc:
            return render(request, self.template_name, {"org": self.org, "error": str(exc)}, status=400)

        if not membership.is_active:
            return registration_state_response(request, self.org, membership)
        messages.success(request, f"Deine E-Mail-Adresse ist bestätigt – willkommen bei {self.org.name}!")
        dashboard = reverse("work:dashboard", kwargs={"org_slug": self.org.slug})
        if request.user.is_authenticated and request.user.pk == membership.user_id:
            return redirect(dashboard)
        return redirect(f"{reverse('accounts:login')}?next={quote(dashboard)}")

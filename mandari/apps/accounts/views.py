# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Authentication views for Mandari.

Provides views for:
- Login / Logout
- Password reset flow
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.views import (
    PasswordResetView as DjangoPasswordResetView,
    PasswordResetDoneView as DjangoPasswordResetDoneView,
    PasswordResetConfirmView as DjangoPasswordResetConfirmView,
    PasswordResetCompleteView as DjangoPasswordResetCompleteView,
)
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import TemplateView

from .forms import LoginForm, PasswordResetForm, SetPasswordForm, RegistrationForm
from .models import LoginAttempt


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

        return render(request, self.template_name, {
            "form": form,
            "next": next_url,
            "invitation": invitation,
        })

    def post(self, request):
        form = LoginForm(request.POST, request=request)
        next_url = request.POST.get("next", "")

        # Rate limiting check
        ip_address = self.get_client_ip(request)
        email = request.POST.get("email", "")

        # Check for pending invitation
        invitation = self._get_pending_invitation(request)

        if self.is_rate_limited(ip_address, email):
            messages.error(
                request,
                "Zu viele fehlgeschlagene Anmeldeversuche. Bitte warte 15 Minuten."
            )
            return render(request, self.template_name, {
                "form": form,
                "next": next_url,
                "invitation": invitation,
            })

        if form.is_valid():
            user = form.get_user()

            # Log successful attempt
            self.log_attempt(request, email, success=True)

            # Login the user
            auth_login(request, user)

            # Handle "remember me"
            if not form.cleaned_data.get("remember_me"):
                # Session expires when browser closes
                request.session.set_expiry(0)
            else:
                # Session expires in 30 days
                request.session.set_expiry(60 * 60 * 24 * 30)

            messages.success(request, "Erfolgreich angemeldet.")

            return redirect(self.get_success_url(request, next_url))
        else:
            # Log failed attempt
            self.log_attempt(request, email, success=False)

        return render(request, self.template_name, {
            "form": form,
            "next": next_url,
            "invitation": invitation,
        })

    def get_success_url(self, request, next_url=None):
        """Determine where to redirect after login."""
        # Check for safe next URL
        if next_url and self.is_safe_url(next_url, request):
            return next_url

        # Default: redirect to work portal if user has memberships
        if hasattr(request.user, 'memberships'):
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
        from django.utils import timezone
        from datetime import timedelta

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
        try:
            LoginAttempt.objects.create(
                email=email,
                ip_address=self.get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
                was_successful=success,
                failure_reason="" if success else "invalid_credentials",
            )
        except Exception:
            pass  # Don't fail login if logging fails

    def _get_pending_invitation(self, request):
        """Get pending invitation from session if any."""
        token = request.session.get("pending_invitation_token")
        if not token:
            return None

        from apps.tenants.models import UserInvitation
        from django.utils import timezone

        try:
            invitation = UserInvitation.objects.get(
                token=token,
                accepted_at__isnull=True,
                expires_at__gt=timezone.now()
            )
            return invitation
        except UserInvitation.DoesNotExist:
            # Clear invalid token
            request.session.pop("pending_invitation_token", None)
            return None


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
    email_template_name = "accounts/emails/password_reset.html"
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

        return render(request, self.template_name, {
            "form": form,
            "invitation": invitation,
        })

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
            # Create user
            user = form.save()

            # Log the user in
            auth_login(request, user)

            # Redirect to accept invitation
            messages.success(
                request,
                f"Willkommen, {user.first_name}! Ihr Konto wurde erstellt."
            )
            return redirect("work:accept_invitation", token=invitation_token)

        return render(request, self.template_name, {
            "form": form,
            "invitation": invitation,
        })

    def get_invitation(self, token):
        """Get and validate the invitation."""
        from apps.tenants.models import UserInvitation
        from django.utils import timezone

        try:
            invitation = UserInvitation.objects.get(
                token=token,
                accepted_at__isnull=True,
                expires_at__gt=timezone.now()
            )
            return invitation
        except UserInvitation.DoesNotExist:
            return None

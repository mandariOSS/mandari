# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.email import send_email
from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


class MemberInviteView(WorkViewMixin, TemplateView):
    """Invite a new member."""

    template_name = "work/organization/invite.html"
    permission_required = "members.invite"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"

        from apps.tenants.models import Role

        context["available_roles"] = Role.objects.filter(organization=self.organization).order_by("name")

        return context

    def post(self, request, *args, **kwargs):
        """Handle invitation creation."""
        from apps.accounts.models import User
        from apps.tenants.models import Membership, Role, UserInvitation

        email = request.POST.get("email", "").strip().lower()
        role_ids = request.POST.getlist("roles")
        message_text = request.POST.get("message", "").strip()

        # Validate email
        if not email:
            messages.error(request, "Bitte geben Sie eine E-Mail-Adresse ein.")
            return redirect("work:member_invite", org_slug=self.organization.slug)

        # Check if user already exists
        existing_user = User.objects.filter(email=email).first()

        if existing_user:
            # Check if already a member
            existing_membership = Membership.objects.filter(user=existing_user, organization=self.organization).first()

            if existing_membership:
                if existing_membership.is_active:
                    messages.warning(request, f"{email} ist bereits Mitglied dieser Organisation.")
                else:
                    # Reactivate membership
                    existing_membership.is_active = True
                    existing_membership.save()
                    messages.success(request, f"{email} wurde reaktiviert.")
                return redirect("work:members", org_slug=self.organization.slug)

        # Check for existing pending invitation
        existing_invitation = UserInvitation.objects.filter(
            organization=self.organization,
            email=email,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).first()

        if existing_invitation:
            messages.warning(request, f"Eine Einladung für {email} ist bereits ausstehend.")
            return redirect("work:members", org_slug=self.organization.slug)

        # Get selected roles
        roles = Role.objects.filter(id__in=role_ids, organization=self.organization) if role_ids else None

        # Create invitation
        try:
            invitation = UserInvitation.create_for_organization(
                organization=self.organization,
                email=email,
                invited_by=request.user,
                roles=roles,
                message=message_text,
                valid_days=7,
            )

            # Send invitation email
            self._send_invitation_email(invitation)

            messages.success(request, f"Einladung an {email} wurde versendet.")

        except Exception as e:
            messages.error(request, f"Fehler beim Erstellen der Einladung: {str(e)}")

        return redirect("work:members", org_slug=self.organization.slug)

    def _send_invitation_email(self, invitation):
        """Send the invitation email."""
        from django.conf import settings as django_settings

        # Build acceptance URL using SITE_URL (not request host)
        base_url = getattr(django_settings, "SITE_URL", "https://volt.mandari.de").rstrip("/")
        accept_path = reverse("work:accept_invitation", kwargs={"token": invitation.token})
        accept_url = f"{base_url}{accept_path}"

        subject = f"Einladung zu {self.organization.name}"

        # Simple HTML email
        html_message = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>Einladung zu {self.organization.name}</h2>
            <p>Hallo,</p>
            <p>Sie wurden von <strong>{invitation.invited_by.get_full_name() or invitation.invited_by.email}</strong>
               eingeladen, der Organisation <strong>{self.organization.name}</strong> auf Mandari Work beizutreten.</p>

            {f"<p><em>Nachricht: {invitation.message}</em></p>" if invitation.message else ""}

            <p>
                <a href="{accept_url}"
                   style="display: inline-block; padding: 12px 24px; background-color: #4f46e5;
                          color: white; text-decoration: none; border-radius: 6px;">
                    Einladung annehmen
                </a>
            </p>

            <p style="color: #666; font-size: 14px;">
                Diese Einladung ist gültig bis zum {invitation.expires_at.strftime("%d.%m.%Y um %H:%M Uhr")}.
            </p>

            <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
            <p style="color: #999; font-size: 12px;">
                Falls Sie diese Einladung nicht erwartet haben, können Sie diese E-Mail ignorieren.
            </p>
        </body>
        </html>
        """

        plain_message = f"""
Einladung zu {self.organization.name}

Hallo,

Sie wurden von {invitation.invited_by.get_full_name() or invitation.invited_by.email} eingeladen,
der Organisation {self.organization.name} auf Mandari Work beizutreten.

{f"Nachricht: {invitation.message}" if invitation.message else ""}

Klicken Sie auf folgenden Link, um die Einladung anzunehmen:
{accept_url}

Diese Einladung ist gültig bis zum {invitation.expires_at.strftime("%d.%m.%Y um %H:%M Uhr")}.

Falls Sie diese Einladung nicht erwartet haben, können Sie diese E-Mail ignorieren.
        """

        success = send_email(
            subject=subject,
            body=plain_message,
            to=[invitation.email],
            html_body=html_message,
            fail_silently=True,  # Don't fail - the invitation is still created
        )

        if not success:
            logger.error(f"Failed to send invitation email to {invitation.email}")


class GuestInviteView(WorkViewMixin, TemplateView):
    """
    Gast einladen.

    Erzeugt sofort einen User-Account (falls nötig) und eine
    Membership(is_guest=True, keine Rollen). Der Gast erhält eine
    Passwort-Setz-Mail über den bestehenden Reset-Mechanismus.
    Optional können direkt Dokumente freigegeben werden
    (MotionShare, scope=user) — oder später am Dokument selbst.
    """

    template_name = "work/organization/guest_invite.html"
    permission_required = "guests.invite"

    GUEST_SHARE_LEVELS = [("view", "Lesen"), ("comment", "Kommentieren"), ("edit", "Bearbeiten")]

    def _shareable_documents(self):
        """Dokumente, die der Einladende freigeben darf (eigene + org-sichtbare)."""
        from django.db.models import Q

        from apps.work.motions.models import Motion

        return (
            Motion.objects.filter(organization=self.organization)
            .filter(Q(visibility="organization") | Q(author=self.membership))
            .exclude(status="deleted")
            .order_by("-updated_at")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["guest_count"] = self.organization.get_active_guest_count()
        context["guest_limit"] = self.organization.guest_limit
        context["guest_limit_reached"] = not self.organization.has_free_guest_slot()
        context["share_levels"] = self.GUEST_SHARE_LEVELS
        context["shareable_documents"] = self._shareable_documents()[:200]
        return context

    def post(self, request, *args, **kwargs):
        from apps.accounts.models import User
        from apps.tenants.models import Membership
        from apps.work.motions.models import MotionShare

        email = request.POST.get("email", "").strip().lower()
        note = request.POST.get("message", "").strip()
        share_level = request.POST.get("share_level", "view")
        if share_level not in dict(self.GUEST_SHARE_LEVELS):
            share_level = "view"
        document_ids = request.POST.getlist("documents")

        if not email or "@" not in email:
            messages.error(request, "Bitte geben Sie eine gültige E-Mail-Adresse ein.")
            return redirect("work:guest_invite", org_slug=self.organization.slug)

        # Gast-Limit prüfen (Standard 25, per Addon erweiterbar)
        if not self.organization.has_free_guest_slot():
            messages.error(
                request,
                f"Gast-Limit erreicht ({self.organization.guest_limit}). Erweiterung als Addon im Kundenportal.",
            )
            return redirect("work:members", org_slug=self.organization.slug)

        user = User.objects.filter(email=email).first()
        user_created = False
        if user is None:
            # Neuer Account ohne Passwort — Passwort-Setz-Mail folgt
            user = User.objects.create_user(email=email, password=None)
            user_created = True
        else:
            existing = Membership.objects.filter(user=user, organization=self.organization).first()
            if existing:
                if existing.is_active:
                    messages.warning(request, f"{email} ist bereits Mitglied dieser Organisation.")
                else:
                    messages.warning(
                        request,
                        f"{email} hat bereits eine deaktivierte Mitgliedschaft. "
                        "Reaktivieren Sie diese in der Mitgliederliste.",
                    )
                return redirect("work:members", org_slug=self.organization.slug)

        Membership.objects.create(
            user=user,
            organization=self.organization,
            is_guest=True,
            invited_by=request.user,
        )

        # Ausgewählte Dokumente sofort freigeben
        shared_count = 0
        if document_ids:
            motions = self._shareable_documents().filter(id__in=document_ids)
            for motion in motions:
                MotionShare.objects.get_or_create(
                    motion=motion,
                    scope="user",
                    user=user,
                    defaults={"level": share_level, "created_by": request.user, "message": note},
                )
                shared_count += 1

        self._send_guest_invitation_email(user, note, user_created)

        success_text = f"Gastzugang für {email} wurde eingerichtet."
        if shared_count:
            success_text += f" {shared_count} Dokument(e) freigegeben."
        messages.success(request, success_text)
        logger.info(
            f"[Guests] Gastzugang {email} in '{self.organization.slug}' angelegt "
            f"(von {request.user.email}, {shared_count} Freigaben)"
        )
        return redirect("work:members", org_slug=self.organization.slug)

    def _send_guest_invitation_email(self, user, note: str, user_created: bool):
        """Gast-Mail: Passwort-Setz-Link (bestehender Reset-Mechanismus) bzw. Direktlink."""
        from django.conf import settings as django_settings
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        base_url = getattr(django_settings, "SITE_URL", "https://mandari.de").rstrip("/")

        if user_created or not user.has_usable_password():
            uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            target_url = base_url + reverse(
                "accounts:password_reset_confirm", kwargs={"uidb64": uidb64, "token": token}
            )
            action_hint = "Über den folgenden Link legen Sie Ihr Passwort fest und aktivieren Ihren Zugang:"
        else:
            target_url = base_url + reverse("work:guest_documents", kwargs={"org_slug": self.organization.slug})
            action_hint = "Ihre freigegebenen Dokumente finden Sie hier:"

        inviter = self.request.user.get_full_name() or self.request.user.email
        subject = f"Gastzugang für {self.organization.name}"
        plain_message = (
            f"Hallo,\n\n"
            f"{inviter} hat Ihnen einen Gastzugang zur Organisation "
            f"{self.organization.name} auf Mandari Work eingerichtet.\n\n"
            f"Als Gast sehen Sie ausschließlich die Dokumente, die für Sie freigegeben wurden.\n\n"
            f"{f'Nachricht: {note}' + chr(10) + chr(10) if note else ''}"
            f"{action_hint}\n{target_url}\n\n"
            f"Falls Sie diese E-Mail nicht erwartet haben, können Sie sie ignorieren.\n"
        )

        success = send_email(
            subject=subject,
            body=plain_message,
            to=[user.email],
            fail_silently=True,
        )
        if not success:
            logger.error(f"Failed to send guest invitation email to {user.email}")


class InvitationResendView(WorkViewMixin, View):
    """Resend an invitation."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import UserInvitation

        invitation_id = kwargs.get("invitation_id")
        invitation = get_object_or_404(
            UserInvitation,
            id=invitation_id,
            organization=self.organization,
            accepted_at__isnull=True,
        )

        # Extend expiration
        from datetime import timedelta

        invitation.expires_at = timezone.now() + timedelta(days=7)
        invitation.save()

        # Resend email
        invite_view = MemberInviteView()
        invite_view.request = request
        invite_view.organization = self.organization
        invite_view._send_invitation_email(invitation)

        messages.success(request, f"Einladung an {invitation.email} wurde erneut versendet.")
        return redirect("work:members", org_slug=self.organization.slug)


class InvitationCancelView(WorkViewMixin, View):
    """Cancel a pending invitation."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import UserInvitation

        invitation_id = kwargs.get("invitation_id")
        invitation = get_object_or_404(
            UserInvitation,
            id=invitation_id,
            organization=self.organization,
            accepted_at__isnull=True,
        )

        email = invitation.email
        invitation.delete()

        messages.success(request, f"Einladung für {email} wurde zurückgezogen.")
        return redirect("work:members", org_slug=self.organization.slug)


class AcceptInvitationView(TemplateView):
    """Accept an invitation (public view - no login required initially)."""

    template_name = "work/organization/accept_invitation.html"

    def get(self, request, *args, **kwargs):
        from apps.tenants.models import UserInvitation

        token = kwargs.get("token")

        try:
            invitation = (
                UserInvitation.objects.select_related("organization", "invited_by")
                .prefetch_related("roles")
                .get(token=token)
            )
        except UserInvitation.DoesNotExist:
            messages.error(request, "Einladung nicht gefunden oder bereits verwendet.")
            return redirect("accounts:login")

        if not invitation.is_valid:
            if invitation.accepted_at:
                messages.info(request, "Diese Einladung wurde bereits angenommen.")
            else:
                messages.error(request, "Diese Einladung ist abgelaufen.")
            return redirect("accounts:login")

        # If user is logged in, show acceptance page
        # If not, redirect to login or register depending on whether account exists
        if request.user.is_authenticated:
            return super().get(request, *args, **kwargs)
        else:
            # Store token in session
            request.session["pending_invitation_token"] = token

            # Check if user with this email already exists
            from apps.accounts.models import User

            user_exists = User.objects.filter(email=invitation.email).exists()

            if user_exists:
                # User exists → redirect to login
                messages.info(request, "Bitte melden Sie sich an, um die Einladung anzunehmen.")
                return redirect("accounts:login")
            else:
                # No account yet → redirect directly to registration
                messages.info(
                    request,
                    f"Willkommen! Erstellen Sie Ihr Konto, um {invitation.organization.name} beizutreten.",
                )
                return redirect("accounts:register")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from apps.tenants.models import UserInvitation

        token = self.kwargs.get("token")
        invitation = (
            UserInvitation.objects.select_related("organization", "invited_by")
            .prefetch_related("roles")
            .get(token=token)
        )

        context["invitation"] = invitation
        context["organization"] = invitation.organization
        return context

    def post(self, request, *args, **kwargs):
        """Accept the invitation and create membership."""
        from apps.tenants.models import Membership, UserInvitation

        if not request.user.is_authenticated:
            return redirect("accounts:login")

        token = kwargs.get("token")

        try:
            invitation = (
                UserInvitation.objects.select_related("organization").prefetch_related("roles").get(token=token)
            )
        except UserInvitation.DoesNotExist:
            messages.error(request, "Einladung nicht gefunden.")
            return redirect("accounts:login")

        if not invitation.is_valid:
            messages.error(request, "Diese Einladung ist nicht mehr gültig.")
            return redirect("accounts:login")

        # Check if already a member
        existing = Membership.objects.filter(user=request.user, organization=invitation.organization).first()

        if existing:
            if existing.is_active:
                messages.info(request, "Sie sind bereits Mitglied dieser Organisation.")
            else:
                existing.is_active = True
                existing.save()
                messages.success(request, f"Willkommen zurück bei {invitation.organization.name}!")
        else:
            # Create membership
            membership = Membership.objects.create(
                user=request.user,
                organization=invitation.organization,
                invited_by=invitation.invited_by,
                invitation_accepted_at=timezone.now(),
            )

            # Add roles from invitation
            if invitation.roles.exists():
                membership.roles.set(invitation.roles.all())

            # If no owner, set this user as owner
            if not invitation.organization.owner:
                invitation.organization.owner = request.user
                invitation.organization.save()

            messages.success(request, f"Willkommen bei {invitation.organization.name}!")

        # Mark invitation as accepted
        invitation.accepted_at = timezone.now()
        invitation.accepted_by = request.user
        invitation.save()

        # Clear session token
        request.session.pop("pending_invitation_token", None)

        return redirect("work:dashboard", org_slug=invitation.organization.slug)

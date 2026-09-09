# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einladungen (Mitglieder und Gäste) sowie die öffentliche Annahme per Token.
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error

logger = logging.getLogger(__name__)


class MemberInviteView(WorkViewMixin, TemplateView):
    """Invite a new member."""

    template_name = "work/organization/invite.html"
    permission_required = "members.invite"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["available_roles"] = selectors.roles_for_organization(self.organization)
        return context

    def post(self, request, *args, **kwargs):
        """Handle invitation creation."""
        email = request.POST.get("email", "").strip().lower()
        if not email:
            messages.error(request, "Bitte geben Sie eine E-Mail-Adresse ein.")
            return redirect("work:member_invite", org_slug=self.organization.slug)

        try:
            message = services.invite_member(
                self.organization,
                request.user,
                email,
                request.POST.getlist("roles"),
                request.POST.get("message", "").strip(),
            )
        except ServiceError as exc:
            flash_error(request, exc)
        else:
            messages.success(request, message)
        return redirect("work:members", org_slug=self.organization.slug)


class GuestInviteView(WorkViewMixin, TemplateView):
    """
    Gast einladen.

    Erzeugt sofort einen User-Account (falls nötig) und eine
    Membership(is_guest=True, keine Rollen). Der Gast erhält eine
    Passwort-Setz-Mail über den bestehenden Reset-Mechanismus.
    Optional können direkt Dokumente (MotionShare, scope=user) und/oder
    ganze Ordner (FolderGuestShare, rekursiv inkl. Unterordner und
    enthaltener Dokumente) freigegeben werden — oder später.
    """

    template_name = "work/organization/guest_invite.html"
    permission_required = "guests.invite"

    GUEST_SHARE_LEVELS = services.GUEST_SHARE_LEVELS

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["guest_count"] = self.organization.get_active_guest_count()
        context["guest_limit"] = self.organization.guest_limit
        context["guest_limit_reached"] = not self.organization.has_free_guest_slot()
        context["share_levels"] = self.GUEST_SHARE_LEVELS
        context["shareable_documents"] = selectors.shareable_documents(self.organization, self.membership)[:200]
        context["shareable_folders"] = selectors.shareable_folders(self.organization, self.membership)
        return context

    def post(self, request, *args, **kwargs):
        email = request.POST.get("email", "").strip().lower()
        if not email or "@" not in email:
            messages.error(request, "Bitte geben Sie eine gültige E-Mail-Adresse ein.")
            return redirect("work:guest_invite", org_slug=self.organization.slug)

        try:
            result = services.invite_guest(
                self.organization,
                self.membership,
                email=email,
                note=request.POST.get("message", "").strip(),
                share_level=request.POST.get("share_level", "view"),
                document_ids=request.POST.getlist("documents"),
                folder_ids=request.POST.getlist("folders"),
            )
        except ServiceError as exc:
            flash_error(request, exc)
        else:
            messages.success(request, result.message)
        return redirect("work:members", org_slug=self.organization.slug)


class GuestAccessResendView(WorkViewMixin, View):
    """
    Zugang eines Gastes erneut senden (Issue #75): Der Passwort-Setz-Link läuft
    nach PASSWORD_RESET_TIMEOUT ab; hier wird ein frischer Link bzw. der
    Direktlink zu den freigegebenen Dokumenten verschickt.
    """

    permission_required = "guests.invite"

    def post(self, request, *args, **kwargs):
        member = selectors.get_member_or_404(self.organization, kwargs.get("member_id"), is_guest=True, is_active=True)
        services.resend_guest_access(
            self.organization, member, request.user, note=request.POST.get("message", "").strip()[:500]
        )
        messages.success(request, f"Zugangs-E-Mail an {member.user.email} wurde erneut versendet.")
        return redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)


class InvitationResendView(WorkViewMixin, View):
    """Resend an invitation."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        invitation = selectors.get_open_invitation_or_404(self.organization, kwargs.get("invitation_id"))
        services.resend_invitation(self.organization, invitation)
        messages.success(request, f"Einladung an {invitation.email} wurde erneut versendet.")
        return redirect("work:members", org_slug=self.organization.slug)


class InvitationCancelView(WorkViewMixin, View):
    """Cancel a pending invitation."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        invitation = selectors.get_open_invitation_or_404(self.organization, kwargs.get("invitation_id"))
        email = services.cancel_invitation(invitation)
        messages.success(request, f"Einladung für {email} wurde zurückgezogen.")
        return redirect("work:members", org_slug=self.organization.slug)


class AcceptInvitationView(TemplateView):
    """Accept an invitation (public view - no login required initially)."""

    template_name = "work/organization/accept_invitation.html"

    def get(self, request, *args, **kwargs):
        token = kwargs.get("token")
        invitation = selectors.find_invitation_by_token(token)
        if invitation is None:
            messages.error(request, "Einladung nicht gefunden oder bereits verwendet.")
            return redirect("accounts:login")

        if not invitation.is_valid:
            if invitation.accepted_at:
                messages.info(request, "Diese Einladung wurde bereits angenommen.")
            else:
                messages.error(request, "Diese Einladung ist abgelaufen.")
            return redirect("accounts:login")

        # Eingeloggt: Annahmeseite anzeigen; sonst je nach Konto zu Login oder Registrierung
        if request.user.is_authenticated:
            return super().get(request, *args, **kwargs)
        request.session["pending_invitation_token"] = token

        if selectors.find_user_by_email(invitation.email) is not None:
            messages.info(request, "Bitte melden Sie sich an, um die Einladung anzunehmen.")
            return redirect("accounts:login")
        messages.info(
            request,
            f"Willkommen! Erstellen Sie Ihr Konto, um {invitation.organization.name} beizutreten.",
        )
        return redirect("accounts:register")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        invitation = selectors.find_invitation_by_token(self.kwargs.get("token"))
        context["invitation"] = invitation
        context["organization"] = invitation.organization if invitation else None
        return context

    def post(self, request, *args, **kwargs):
        """Accept the invitation and create membership."""
        if not request.user.is_authenticated:
            return redirect("accounts:login")

        invitation = selectors.find_invitation_by_token(kwargs.get("token"))
        if invitation is None:
            messages.error(request, "Einladung nicht gefunden.")
            return redirect("accounts:login")
        if not invitation.is_valid:
            messages.error(request, "Diese Einladung ist nicht mehr gültig.")
            return redirect("accounts:login")

        message = services.accept_invitation(invitation, request.user)
        if message.startswith("Sie sind bereits"):
            messages.info(request, message)
        else:
            messages.success(request, message)

        request.session.pop("pending_invitation_token", None)
        return redirect("work:dashboard", org_slug=invitation.organization.slug)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.services import PasswordService, SessionService, TwoFactorService
from apps.common.email import send_email
from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


class OrganizationSettingsView(WorkViewMixin, TemplateView):
    """Organization settings page."""

    template_name = "work/organization/settings.html"
    permission_required = "organization.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "general"

        # Check if user can manage faction settings
        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

        # Get document settings counts
        from apps.work.motions.models import MotionTemplate, MotionType, OrganizationLetterhead

        context["type_count"] = MotionType.objects.filter(organization=self.organization).count()
        context["template_count"] = MotionTemplate.objects.filter(organization=self.organization).count()
        context["letterhead_count"] = OrganizationLetterhead.objects.filter(organization=self.organization).count()

        return context


class OrganizationFactionSettingsView(WorkViewMixin, TemplateView):
    """Faction meeting settings tab in organization settings."""

    template_name = "work/organization/faction_settings.html"
    permission_required = "faction.manage"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "faction"

        # Get current faction settings
        settings = self.organization.settings or {}
        context["faction_settings"] = settings.get("faction", {})

        # Default values for display
        defaults = {
            "auto_create_approval_item": True,
            "link_previous_meeting": True,
            "protocol_revision_safe": True,
            "auto_lock_protocol_on_complete": True,
            "require_protocol_approval": True,
            "first_agenda_title_with_previous": "Genehmigung der Tagesordnung und des Protokolls der Sitzung vom {datum_letzte_sitzung}",
            "first_agenda_title_no_previous": "Genehmigung der Tagesordnung",
            "first_agenda_description": "",
        }

        for key, default in defaults.items():
            if key not in context["faction_settings"]:
                context["faction_settings"][key] = default

        # Available placeholders for reference
        context["placeholders"] = [
            ("{datum_letzte_sitzung}", "Datum der letzten Sitzung (z.B. 15.01.2026)"),
            ("{titel_letzte_sitzung}", "Titel der letzten Sitzung"),
            ("{nr_letzte_sitzung}", "Nummer der letzten Sitzung"),
            ("{datum}", "Datum der aktuellen Sitzung"),
            ("{titel}", "Titel der aktuellen Sitzung"),
            ("{nr}", "Nummer der aktuellen Sitzung"),
        ]

        return context

    def post(self, request, *args, **kwargs):
        from django.contrib import messages

        # Get current settings
        settings = self.organization.settings or {}
        faction_settings = settings.get("faction", {})

        # Update workflow settings
        faction_settings["auto_create_approval_item"] = request.POST.get("auto_create_approval_item") == "on"
        faction_settings["link_previous_meeting"] = request.POST.get("link_previous_meeting") == "on"
        faction_settings["protocol_revision_safe"] = request.POST.get("protocol_revision_safe") == "on"
        faction_settings["auto_lock_protocol_on_complete"] = request.POST.get("auto_lock_protocol_on_complete") == "on"
        faction_settings["require_protocol_approval"] = request.POST.get("require_protocol_approval") == "on"

        # Update title templates
        faction_settings["first_agenda_title_with_previous"] = request.POST.get(
            "first_agenda_title_with_previous", ""
        ).strip()
        faction_settings["first_agenda_title_no_previous"] = request.POST.get(
            "first_agenda_title_no_previous", ""
        ).strip()
        faction_settings["first_agenda_description"] = request.POST.get("first_agenda_description", "").strip()

        # Save back to organization
        settings["faction"] = faction_settings
        self.organization.settings = settings
        self.organization.save()

        messages.success(request, "Einstellungen gespeichert.")
        return redirect("work:organization_faction_settings", org_slug=self.organization.slug)


class OrganizationDocumentsView(WorkViewMixin, TemplateView):
    """Document settings tab for organization (Anträge & Vorgänge)."""

    template_name = "work/organization/documents.html"
    permission_required = "organization.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"

        # Check if user can manage faction settings
        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

        # Get document settings counts
        from apps.work.motions.models import MotionTemplate, MotionType, OrganizationLetterhead

        context["motion_types"] = MotionType.objects.filter(organization=self.organization).order_by(
            "sort_order", "name"
        )
        context["templates"] = (
            MotionTemplate.objects.filter(organization=self.organization).select_related("motion_type").order_by("name")
        )
        context["letterheads"] = OrganizationLetterhead.objects.filter(organization=self.organization).order_by("name")

        context["type_count"] = context["motion_types"].count()
        context["template_count"] = context["templates"].count()
        context["letterhead_count"] = context["letterheads"].count()

        return context


# =============================================================================
# MEMBER MANAGEMENT
# =============================================================================


class MemberListView(WorkViewMixin, TemplateView):
    """List of organization members."""

    template_name = "work/organization/members.html"
    permission_required = "members.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"

        # Check if user can manage faction settings
        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

        from apps.tenants.models import Membership, UserInvitation

        # Get all active members
        members = (
            Membership.objects.filter(organization=self.organization, is_active=True)
            .select_related("user")
            .prefetch_related("roles")
            .order_by("user__first_name", "user__last_name")
        )

        # Get inactive members
        inactive_members = (
            Membership.objects.filter(organization=self.organization, is_active=False)
            .select_related("user")
            .prefetch_related("roles")
        )

        # Get pending invitations
        pending_invitations = UserInvitation.objects.filter(
            organization=self.organization, accepted_at__isnull=True, expires_at__gt=timezone.now()
        ).order_by("-created_at")

        context["members"] = members
        context["inactive_members"] = inactive_members
        context["pending_invitations"] = pending_invitations
        context["is_owner"] = self.organization.owner == self.request.user

        return context


class MemberDetailView(WorkViewMixin, TemplateView):
    """View and edit a member's details."""

    template_name = "work/organization/member_detail.html"
    permission_required = "members.view"

    def _find_matching_persons(self, user, body):
        """Findet OParl-Personen anhand des Benutzernamens."""
        from django.db.models import Q

        from insight_core.models import OParlPerson

        first = user.first_name.strip() if user.first_name else ""
        last = user.last_name.strip() if user.last_name else ""

        if not first and not last:
            return []

        query = Q(body=body)
        if first and last:
            query &= (
                Q(given_name__iexact=first, family_name__iexact=last)
                | Q(name__icontains=f"{first} {last}")
                | Q(name__icontains=f"{last}, {first}")
            )
        elif last:
            query &= Q(family_name__iexact=last) | Q(name__icontains=last)

        return list(OParlPerson.objects.filter(query)[:5])

    def _get_suggested_committees(self, oparl_person, body, today):
        """Holt aktive Gremienmitgliedschaften einer OParl-Person."""
        from django.db.models import Q

        from insight_core.models import OParlMembership

        memberships = (
            OParlMembership.objects.filter(person=oparl_person, organization__body=body)
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .select_related("organization")
        )

        return [m.organization for m in memberships if m.organization.is_active]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"

        from django.db.models import Q

        from apps.tenants.models import Membership, Role

        member_id = kwargs.get("member_id")
        member = get_object_or_404(Membership, id=member_id, organization=self.organization)

        context["member"] = member
        context["available_roles"] = Role.objects.filter(organization=self.organization).order_by("name")
        context["is_owner"] = self.organization.owner == member.user
        context["is_self"] = member.user == self.request.user
        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_edit"] = checker.has_permission("members.edit") or checker.is_admin()

        # Committee assignment (OParl committees from linked body)
        body = self.organization.body
        context["has_body"] = body is not None

        if body:
            from insight_core.models import OParlOrganization

            today = timezone.now().date()

            # Gremien nach Status aufteilen
            all_committees = OParlOrganization.objects.filter(body=body)

            context["active_committees"] = all_committees.filter(
                Q(end_date__isnull=True) | Q(end_date__gte=today)
            ).order_by("name")

            context["inactive_committees"] = all_committees.filter(end_date__lt=today).order_by("name")

            context["member_committees"] = list(member.oparl_committees.values_list("id", flat=True))

            # OParl-Person Vorschläge
            context["oparl_person"] = member.oparl_person
            context["suggested_committee_ids"] = []

            if member.oparl_person:
                # Aktive Mitgliedschaften der verknüpften Person
                suggested = self._get_suggested_committees(member.oparl_person, body, today)
                context["suggested_committee_ids"] = [c.id for c in suggested]
            else:
                # Namens-Matching für Vorschläge
                context["potential_oparl_persons"] = self._find_matching_persons(member.user, body)
        else:
            context["active_committees"] = []
            context["inactive_committees"] = []
            context["member_committees"] = []
            context["oparl_person"] = None
            context["suggested_committee_ids"] = []
            context["potential_oparl_persons"] = []

        return context

    def post(self, request, *args, **kwargs):
        """Handle member updates."""
        from apps.tenants.models import Membership, Role

        member_id = kwargs.get("member_id")
        member = get_object_or_404(Membership, id=member_id, organization=self.organization)

        # Check permission
        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        if not checker.has_permission("members.edit") and not checker.is_admin():
            messages.error(request, "Keine Berechtigung zum Bearbeiten von Mitgliedern.")
            return redirect("work:members", org_slug=self.organization.slug)

        action = request.POST.get("action")

        if action == "update_committees":
            # Update member's OParl committee assignments
            committee_ids = request.POST.getlist("committees")
            body = self.organization.body
            if body:
                from insight_core.models import OParlOrganization

                committees = OParlOrganization.objects.filter(id__in=committee_ids, body=body)
                member.oparl_committees.set(committees)
                messages.success(
                    request,
                    f"Gremien für {member.user.get_full_name() or member.user.email} aktualisiert.",
                )
            else:
                messages.error(request, "Keine Kommune verknüpft. Gremien können nicht zugewiesen werden.")

        elif action == "update_roles":
            # Update member roles
            role_ids = request.POST.getlist("roles")
            roles = Role.objects.filter(id__in=role_ids, organization=self.organization)
            member.roles.set(roles)
            messages.success(
                request,
                f"Rollen für {member.user.get_full_name() or member.user.email} aktualisiert.",
            )

        elif action == "deactivate":
            # Deactivate member (soft delete)
            if member.user == self.organization.owner:
                messages.error(request, "Der Eigentümer kann nicht deaktiviert werden.")
            elif member.user == request.user:
                messages.error(request, "Sie können sich nicht selbst deaktivieren.")
            else:
                member.is_active = False
                member.save()
                messages.success(
                    request,
                    f"{member.user.get_full_name() or member.user.email} wurde deaktiviert.",
                )
                return redirect("work:members", org_slug=self.organization.slug)

        elif action == "reactivate":
            member.is_active = True
            member.save()
            messages.success(request, f"{member.user.get_full_name() or member.user.email} wurde reaktiviert.")

        elif action == "remove":
            # Completely remove member
            if member.user == self.organization.owner:
                messages.error(request, "Der Eigentümer kann nicht entfernt werden.")
            elif member.user == request.user:
                messages.error(request, "Sie können sich nicht selbst entfernen.")
            else:
                name = member.user.get_full_name() or member.user.email
                member.delete()
                messages.success(request, f"{name} wurde aus der Organisation entfernt.")
                return redirect("work:members", org_slug=self.organization.slug)

        elif action == "transfer_ownership":
            # Transfer ownership to this member
            if self.organization.owner != request.user:
                messages.error(request, "Nur der aktuelle Eigentümer kann die Eigentümerschaft übertragen.")
            else:
                self.organization.owner = member.user
                self.organization.save()
                messages.success(
                    request,
                    f"Eigentümerschaft wurde auf {member.user.get_full_name() or member.user.email} übertragen.",
                )

        elif action == "link_oparl_person":
            person_id = request.POST.get("oparl_person_id")
            body = self.organization.body
            if body and person_id:
                from insight_core.models import OParlPerson

                oparl_person = get_object_or_404(OParlPerson, id=person_id, body=body)
                member.oparl_person = oparl_person
                member.save()
                messages.success(request, f"RIS-Person '{oparl_person.display_name}' verknüpft.")
            else:
                messages.error(request, "Keine Kommune verknüpft oder ungültige Person.")

        elif action == "unlink_oparl_person":
            member.oparl_person = None
            member.save()
            messages.success(request, "RIS-Verknüpfung entfernt.")

        elif action == "apply_suggestions":
            body = self.organization.body
            if body and member.oparl_person:
                today = timezone.now().date()
                suggested = self._get_suggested_committees(member.oparl_person, body, today)
                member.oparl_committees.set(suggested)
                messages.success(request, f"{len(suggested)} Gremien übernommen.")
            else:
                messages.error(request, "Keine RIS-Person verknüpft oder keine Kommune zugeordnet.")

        elif action == "update_sworn_in":
            # Update sworn-in status (Vereidigung)
            is_sworn_in = request.POST.get("is_sworn_in") == "1"
            member.is_sworn_in = is_sworn_in
            member.save()
            if is_sworn_in:
                messages.success(
                    request,
                    f"{member.user.get_full_name() or member.user.email} wurde als vereidigt markiert.",
                )
            else:
                messages.success(
                    request,
                    f"Vereidigungsstatus für {member.user.get_full_name() or member.user.email} wurde entfernt.",
                )

        return redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)


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


class RoleListView(WorkViewMixin, TemplateView):
    """List and manage roles."""

    template_name = "work/organization/roles.html"
    permission_required = "organization.manage_roles"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"

        # Check if user can manage faction settings
        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

        # Get all roles for this organization
        from apps.tenants.models import Role

        roles = (
            Role.objects.filter(organization=self.organization)
            .prefetch_related("permissions")
            .order_by("priority", "name")
        )

        context["roles"] = roles
        return context


# =============================================================================
# USER PROFILE
# =============================================================================


class ProfileView(WorkViewMixin, TemplateView):
    """User profile within organization context."""

    template_name = "work/profile/index.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        return context

    def post(self, request, *args, **kwargs):
        """Handle profile updates."""
        user = request.user
        action = request.POST.get("action")

        if action == "update_profile":
            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()

            user.first_name = first_name
            user.last_name = last_name
            user.save()

            messages.success(request, "Profil aktualisiert.")

        return redirect("work:profile", org_slug=self.organization.slug)


class SecurityView(WorkViewMixin, TemplateView):
    """Security settings within organization context."""

    template_name = "work/profile/security.html"
    permission_required = "dashboard.view"

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


class SecurityAPIView(WorkViewMixin, View):
    """API endpoints for security settings."""

    permission_required = "dashboard.view"

    def post(self, request, *args, **kwargs):
        """Handle API requests."""
        action = kwargs.get("action")
        user = request.user

        if action == "setup_2fa":
            tfa_service = TwoFactorService()
            setup_data = tfa_service.setup_2fa(user)
            request.session["2fa_setup"] = {
                "secret": setup_data["secret"],
                "backup_codes": setup_data["backup_codes"],
            }
            return JsonResponse(
                {
                    "success": True,
                    "qr_code": setup_data["qr_code"],
                    "secret": setup_data["secret"],
                    "backup_codes": setup_data["backup_codes"],
                }
            )

        elif action == "verify_2fa":
            data = json.loads(request.body) if request.content_type == "application/json" else request.POST
            code = data.get("code", "").strip()

            tfa_service = TwoFactorService()
            if tfa_service.confirm_2fa(user, code):
                request.session.pop("2fa_setup", None)
                return JsonResponse({"success": True})
            else:
                return JsonResponse({"success": False, "error": "Ungültiger Code"})

        elif action == "check_password":
            data = json.loads(request.body) if request.content_type == "application/json" else request.POST
            password = data.get("password", "")
            result = PasswordService.check_strength(password)
            return JsonResponse(result)

        return JsonResponse({"error": "Unknown action"}, status=400)


# =============================================================================
# COUNCIL PARTY MANAGEMENT
# =============================================================================


class CouncilPartyListView(WorkViewMixin, TemplateView):
    """List and manage council parties for coalition settings."""

    template_name = "work/organization/parties.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "parties"

        from apps.tenants.models import CouncilParty

        parties = CouncilParty.objects.filter(organization=self.organization).order_by("coalition_order", "name")

        context["parties"] = parties
        context["coalition_parties"] = parties.filter(is_coalition_member=True)
        context["other_parties"] = parties.filter(is_coalition_member=False)

        # Organization settings
        context["coalition_name"] = self.organization.coalition_name
        context["administration_email"] = self.organization.administration_email

        return context

    def post(self, request, *args, **kwargs):
        """Handle organization settings update."""
        action = request.POST.get("action")

        if action == "update_org_settings":
            self.organization.coalition_name = request.POST.get("coalition_name", "").strip()
            self.organization.administration_email = request.POST.get("administration_email", "").strip()
            self.organization.save()
            messages.success(request, "Einstellungen gespeichert.")

        return redirect("work:council_parties", org_slug=self.organization.slug)


class CouncilPartyCreateView(WorkViewMixin, TemplateView):
    """Create a new council party."""

    template_name = "work/organization/party_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["is_edit"] = False
        context["party"] = None
        return context

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import CouncilParty

        name = request.POST.get("name", "").strip()
        short_name = request.POST.get("short_name", "").strip()

        if not name or not short_name:
            messages.error(request, "Name und Kurzname sind erforderlich.")
            return redirect("work:council_party_create", org_slug=self.organization.slug)

        # Check for duplicate short name
        if CouncilParty.objects.filter(organization=self.organization, short_name=short_name).exists():
            messages.error(request, f"Eine Fraktion mit dem Kurzname '{short_name}' existiert bereits.")
            return redirect("work:council_party_create", org_slug=self.organization.slug)

        CouncilParty.objects.create(
            organization=self.organization,
            name=name,
            short_name=short_name,
            email=request.POST.get("email", "").strip(),
            contact_name=request.POST.get("contact_name", "").strip(),
            contact_phone=request.POST.get("contact_phone", "").strip(),
            color=request.POST.get("color", "#6b7280").strip(),
            is_coalition_member=request.POST.get("is_coalition_member") == "on",
            coalition_order=int(request.POST.get("coalition_order", 0) or 0),
        )

        messages.success(request, f"Fraktion '{name}' wurde erstellt.")
        return redirect("work:council_parties", org_slug=self.organization.slug)


class CouncilPartyEditView(WorkViewMixin, TemplateView):
    """Edit an existing council party."""

    template_name = "work/organization/party_form.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["is_edit"] = True

        from apps.tenants.models import CouncilParty

        party_id = kwargs.get("party_id")
        party = get_object_or_404(CouncilParty, id=party_id, organization=self.organization)
        context["party"] = party
        return context

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import CouncilParty

        party_id = kwargs.get("party_id")
        party = get_object_or_404(CouncilParty, id=party_id, organization=self.organization)

        action = request.POST.get("action")

        if action == "delete":
            name = party.name
            party.delete()
            messages.success(request, f"Fraktion '{name}' wurde gelöscht.")
            return redirect("work:council_parties", org_slug=self.organization.slug)

        # Update party
        name = request.POST.get("name", "").strip()
        short_name = request.POST.get("short_name", "").strip()

        if not name or not short_name:
            messages.error(request, "Name und Kurzname sind erforderlich.")
            return redirect("work:council_party_edit", org_slug=self.organization.slug, party_id=party_id)

        # Check for duplicate short name (excluding current party)
        if (
            CouncilParty.objects.filter(organization=self.organization, short_name=short_name)
            .exclude(id=party_id)
            .exists()
        ):
            messages.error(request, f"Eine andere Fraktion mit dem Kurzname '{short_name}' existiert bereits.")
            return redirect("work:council_party_edit", org_slug=self.organization.slug, party_id=party_id)

        party.name = name
        party.short_name = short_name
        party.email = request.POST.get("email", "").strip()
        party.contact_name = request.POST.get("contact_name", "").strip()
        party.contact_phone = request.POST.get("contact_phone", "").strip()
        party.color = request.POST.get("color", "#6b7280").strip()
        party.is_coalition_member = request.POST.get("is_coalition_member") == "on"
        party.coalition_order = int(request.POST.get("coalition_order", 0) or 0)
        party.is_active = request.POST.get("is_active") != "off"
        party.save()

        messages.success(request, f"Fraktion '{name}' wurde aktualisiert.")
        return redirect("work:council_parties", org_slug=self.organization.slug)

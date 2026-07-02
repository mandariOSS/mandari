# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.services import PasswordService, SessionService, TwoFactorService
from apps.common.email import send_email
from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


# =============================================================================
# TEAM DIRECTORY
# =============================================================================


class TeamDirectoryView(WorkViewMixin, TemplateView):
    """Internal team directory — visible to all active members."""

    template_name = "work/team/directory.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "team"

        from django.db.models import Q

        from apps.tenants.models import Membership

        from .models import MemberAbsence

        members = (
            Membership.objects.filter(organization=self.organization, is_active=True)
            .select_related("user")
            .prefetch_related("roles", "oparl_committees")
            .order_by("user__last_name", "user__first_name")
        )

        # Search filter
        search_query = self.request.GET.get("q", "").strip()
        if search_query:
            members = members.filter(
                Q(user__first_name__icontains=search_query)
                | Q(user__last_name__icontains=search_query)
                | Q(user__email__icontains=search_query)
            )

        # Current absences for all members
        today = timezone.now().date()
        current_absences = MemberAbsence.objects.filter(
            organization=self.organization,
            is_active=True,
            start_date__lte=today,
            end_date__gte=today,
        ).select_related("deputy__user")
        absence_map = {a.membership_id: a for a in current_absences}

        # Annotate members with absence info
        member_list = []
        for member in members:
            member.current_absence = absence_map.get(member.id)
            member_list.append(member)

        context["members"] = member_list
        context["member_count"] = len(member_list) if search_query else members.count()
        context["search_query"] = search_query
        context["is_owner_user"] = self.organization.owner

        return context


class TeamMemberProfileView(WorkViewMixin, TemplateView):
    """Read-only member profile in team directory."""

    template_name = "work/team/profile.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "team"

        from apps.tenants.models import Membership

        from .models import MemberAbsence

        member_id = kwargs.get("member_id")
        member = get_object_or_404(
            Membership.objects.select_related("user").prefetch_related("roles", "oparl_committees"),
            id=member_id,
            organization=self.organization,
            is_active=True,
        )

        context["member"] = member
        context["is_self"] = member.user == self.request.user
        context["is_owner"] = self.organization.owner == member.user
        context["roles"] = member.roles.all()
        context["committees"] = member.oparl_committees.all()
        context["joined_at"] = member.joined_at

        # Current absence
        today = timezone.now().date()
        context["current_absence"] = (
            MemberAbsence.objects.filter(
                membership=member,
                organization=self.organization,
                is_active=True,
                start_date__lte=today,
                end_date__gte=today,
            )
            .select_related("deputy__user")
            .first()
        )

        # Visibility / contact settings from User.settings
        user_settings = member.user.settings or {}
        profile = user_settings.get("profile", {})
        context["bio"] = profile.get("bio", "")
        context["show_email"] = profile.get("show_email", True)
        context["show_phone"] = profile.get("show_phone", False)
        context["preferred_contact"] = profile.get("preferred_contact", "email")
        context["contact_signal"] = profile.get("contact_signal", "")

        return context


class OrganizationSettingsView(WorkViewMixin, TemplateView):
    """Organization settings page."""

    template_name = "work/organization/settings.html"
    permission_required = "organization.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "general"

        # Check permissions
        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")
        context["can_edit"] = checker.has_permission("organization.edit")

        return context

    def post(self, request, *args, **kwargs):
        """Handle organization settings updates."""
        import re

        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        if not checker.has_permission("organization.edit"):
            messages.error(request, "Keine Berechtigung zum Bearbeiten.")
            return redirect("work:organization", org_slug=self.organization.slug)

        action = request.POST.get("action")

        if action == "update_general":
            name = request.POST.get("name", "").strip()
            if not name:
                messages.error(request, "Der Name darf nicht leer sein.")
                return redirect("work:organization", org_slug=self.organization.slug)

            self.organization.name = name
            self.organization.description = request.POST.get("description", "").strip()

            # Primary color
            color = request.POST.get("primary_color", "").strip()
            if color and re.match(r"^#[0-9a-fA-F]{6}$", color):
                self.organization.primary_color = color

            # Logo upload
            if "logo" in request.FILES:
                logo_file = request.FILES["logo"]
                allowed_types = ["image/jpeg", "image/png", "image/webp"]
                if logo_file.content_type in allowed_types and logo_file.size <= 5 * 1024 * 1024:
                    if self.organization.logo:
                        self.organization.logo.delete(save=False)
                    self.organization.logo = logo_file
                else:
                    messages.error(request, "Logo muss JPG, PNG oder WebP sein und max. 5 MB gross.")
                    return redirect("work:organization", org_slug=self.organization.slug)

            # Logo remove
            if request.POST.get("remove_logo") == "1":
                if self.organization.logo:
                    self.organization.logo.delete(save=False)
                    self.organization.logo = None

            self.organization.save()
            messages.success(request, "Organisationseinstellungen gespeichert.")

        elif action == "update_contact":
            from django.core.exceptions import ValidationError
            from django.core.validators import EmailValidator, URLValidator

            contact_email = request.POST.get("contact_email", "").strip()
            website = request.POST.get("website", "").strip()

            if contact_email:
                try:
                    EmailValidator()(contact_email)
                except ValidationError:
                    messages.error(request, "Ungueltige E-Mail-Adresse.")
                    return redirect("work:organization", org_slug=self.organization.slug)

            if website:
                try:
                    URLValidator()(website)
                except ValidationError:
                    messages.error(request, "Ungueltige Website-URL.")
                    return redirect("work:organization", org_slug=self.organization.slug)

            self.organization.contact_email = contact_email
            self.organization.contact_phone = request.POST.get("contact_phone", "").strip()
            self.organization.website = website
            self.organization.address = request.POST.get("address", "").strip()
            self.organization.save()
            messages.success(request, "Kontaktdaten gespeichert.")

        return redirect("work:organization", org_slug=self.organization.slug)


class OrganizationFactionSettingsView(WorkViewMixin, TemplateView):
    """Faction meeting settings tab in organization settings."""

    template_name = "work/organization/faction_settings.html"
    permission_required = "faction.manage"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "faction"
        context["can_manage_faction"] = True  # This view requires faction.manage

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
        context["active_tab"] = "documents"

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
        context["active_tab"] = "members"

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

        # Pending self-registrations (inactive members without invitation)
        pending_registrations = (
            Membership.objects.filter(
                organization=self.organization,
                is_active=False,
                invitation_accepted_at__isnull=True,
            )
            .select_related("user")
            .order_by("-joined_at")
        )

        context["members"] = members
        context["inactive_members"] = inactive_members
        context["pending_invitations"] = pending_invitations
        context["pending_registrations"] = pending_registrations
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
        context["active_tab"] = "roles"

        # Check if user can manage faction settings
        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

        # Get all roles for this organization with member count
        from django.db.models import Count

        from apps.tenants.models import Role

        roles = (
            Role.objects.filter(organization=self.organization)
            .prefetch_related("permissions")
            .annotate(member_count=Count("memberships"))
            .order_by("-priority", "name")
        )

        context["roles"] = roles
        return context


class RoleCreateView(WorkViewMixin, TemplateView):
    """Create a new role."""

    template_name = "work/organization/role_form.html"
    permission_required = "organization.manage_roles"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["is_edit"] = False
        context["role"] = None
        context["role_permissions"] = set()

        from apps.common.permissions import get_permissions_by_category

        context["permission_categories"] = get_permissions_by_category()
        return context

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Permission, Role

        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "Der Name ist erforderlich.")
            return redirect("work:role_create", org_slug=self.organization.slug)

        # Check unique name per org
        if Role.objects.filter(organization=self.organization, name=name).exists():
            messages.error(request, f"Eine Rolle mit dem Namen '{name}' existiert bereits.")
            return redirect("work:role_create", org_slug=self.organization.slug)

        import re

        color = request.POST.get("color", "#6b7280").strip()
        if not re.match(r"^#[0-9a-fA-F]{6}$", color):
            color = "#6b7280"

        role = Role.objects.create(
            organization=self.organization,
            name=name,
            description=request.POST.get("description", "").strip(),
            color=color,
            priority=min(max(int(request.POST.get("priority", 50) or 50), 0), 100),
            is_admin=request.POST.get("is_admin") == "on",
            require_2fa=request.POST.get("require_2fa") == "on",
            is_system_role=False,
        )

        # Set permissions
        perm_codenames = request.POST.getlist("permissions")
        if perm_codenames and not role.is_admin:
            perms = Permission.objects.filter(codename__in=perm_codenames)
            role.permissions.set(perms)

        messages.success(request, f"Rolle '{name}' wurde erstellt.")
        return redirect("work:roles", org_slug=self.organization.slug)


class RoleEditView(WorkViewMixin, TemplateView):
    """Edit an existing role."""

    template_name = "work/organization/role_form.html"
    permission_required = "organization.manage_roles"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["is_edit"] = True

        from apps.tenants.models import Role

        role = get_object_or_404(Role, id=kwargs["role_id"], organization=self.organization)
        context["role"] = role
        context["role_permissions"] = set(role.permissions.values_list("codename", flat=True))

        from apps.common.permissions import get_permissions_by_category

        context["permission_categories"] = get_permissions_by_category()
        return context

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Permission, Role

        role = get_object_or_404(Role, id=kwargs["role_id"], organization=self.organization)

        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "Der Name ist erforderlich.")
            return redirect("work:role_edit", org_slug=self.organization.slug, role_id=role.id)

        # Check unique name per org (excluding self)
        if Role.objects.filter(organization=self.organization, name=name).exclude(id=role.id).exists():
            messages.error(request, f"Eine Rolle mit dem Namen '{name}' existiert bereits.")
            return redirect("work:role_edit", org_slug=self.organization.slug, role_id=role.id)

        import re

        color = request.POST.get("color", role.color).strip()
        if not re.match(r"^#[0-9a-fA-F]{6}$", color):
            color = role.color

        role.name = name
        role.description = request.POST.get("description", "").strip()
        role.color = color
        role.require_2fa = request.POST.get("require_2fa") == "on"

        # System roles: is_admin and priority can't be changed
        if not role.is_system_role:
            role.is_admin = request.POST.get("is_admin") == "on"
            role.priority = min(max(int(request.POST.get("priority", role.priority) or role.priority), 0), 100)

        role.save()

        # Update permissions
        perm_codenames = request.POST.getlist("permissions")
        if role.is_admin:
            role.permissions.clear()
        else:
            perms = Permission.objects.filter(codename__in=perm_codenames)
            role.permissions.set(perms)

        messages.success(request, f"Rolle '{name}' wurde aktualisiert.")
        return redirect("work:roles", org_slug=self.organization.slug)


class RoleDeleteView(WorkViewMixin, View):
    """Delete a role (POST only)."""

    permission_required = "organization.manage_roles"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Role

        role = get_object_or_404(Role, id=kwargs["role_id"], organization=self.organization)

        if role.is_system_role:
            messages.error(request, "Systemrollen können nicht gelöscht werden.")
            return redirect("work:roles", org_slug=self.organization.slug)

        member_count = role.memberships.count()
        if member_count > 0:
            messages.error(
                request,
                f"Die Rolle '{role.name}' ist noch {member_count} Mitglied(ern) zugewiesen. "
                "Entfernen Sie zuerst die Zuweisungen.",
            )
            return redirect("work:roles", org_slug=self.organization.slug)

        role_name = role.name
        role.delete()
        messages.success(request, f"Rolle '{role_name}' wurde gelöscht.")
        return redirect("work:roles", org_slug=self.organization.slug)


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


# =============================================================================
# COUNCIL PARTY MANAGEMENT
# =============================================================================


class CouncilPartyListView(WorkViewMixin, TemplateView):
    """Ratsfraktionen, Koalition und Verwaltungskontakte verwalten."""

    template_name = "work/organization/parties.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "parties"

        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

        from apps.tenants.models import AdministrationContact, CouncilParty

        parties = CouncilParty.objects.filter(organization=self.organization).order_by("coalition_order", "name")
        context["parties"] = parties
        context["coalition_parties"] = parties.filter(is_coalition_member=True)
        context["other_parties"] = parties.filter(is_coalition_member=False)

        # Verwaltungskontakte
        context["admin_contacts"] = AdministrationContact.objects.filter(organization=self.organization)

        # Koalitionsname
        context["coalition_name"] = self.organization.coalition_name

        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")

        if action == "update_coalition":
            self.organization.coalition_name = request.POST.get("coalition_name", "").strip()
            self.organization.save(update_fields=["coalition_name"])
            messages.success(request, "Koalitionsname gespeichert.")

        elif action == "add_admin_contact":
            self._handle_add_admin_contact(request)

        elif action == "delete_admin_contact":
            self._handle_delete_admin_contact(request)

        elif action == "add_party":
            self._handle_add_party(request)

        elif action == "update_party":
            self._handle_update_party(request)

        elif action == "delete_party":
            self._handle_delete_party(request)

        return redirect("work:council_parties", org_slug=self.organization.slug)

    def _handle_add_admin_contact(self, request):
        from apps.tenants.models import AdministrationContact

        label = request.POST.get("contact_label", "").strip()
        email = request.POST.get("contact_email", "").strip()
        if label and email:
            AdministrationContact.objects.create(
                organization=self.organization,
                label=label,
                email=email,
            )
            messages.success(request, f"Kontakt '{label}' hinzugefügt.")
        else:
            messages.error(request, "Bezeichnung und E-Mail sind erforderlich.")

    def _handle_delete_admin_contact(self, request):
        from apps.tenants.models import AdministrationContact

        contact_id = request.POST.get("contact_id")
        deleted, _ = AdministrationContact.objects.filter(
            id=contact_id,
            organization=self.organization,
        ).delete()
        if deleted:
            messages.success(request, "Kontakt entfernt.")

    def _handle_add_party(self, request):
        from apps.tenants.models import CouncilParty

        name = request.POST.get("name", "").strip()
        short_name = request.POST.get("short_name", "").strip()
        if not name or not short_name:
            messages.error(request, "Name und Kurzname sind erforderlich.")
            return

        if CouncilParty.objects.filter(organization=self.organization, short_name=short_name).exists():
            messages.error(request, f"Kurzname '{short_name}' existiert bereits.")
            return

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
        messages.success(request, f"Fraktion '{name}' hinzugefügt.")

    def _handle_update_party(self, request):
        from apps.tenants.models import CouncilParty

        party_id = request.POST.get("party_id")
        party = CouncilParty.objects.filter(id=party_id, organization=self.organization).first()
        if not party:
            return

        name = request.POST.get("name", "").strip()
        short_name = request.POST.get("short_name", "").strip()
        if not name or not short_name:
            messages.error(request, "Name und Kurzname sind erforderlich.")
            return

        if (
            CouncilParty.objects.filter(
                organization=self.organization,
                short_name=short_name,
            )
            .exclude(id=party_id)
            .exists()
        ):
            messages.error(request, f"Kurzname '{short_name}' existiert bereits.")
            return

        party.name = name
        party.short_name = short_name
        party.email = request.POST.get("email", "").strip()
        party.contact_name = request.POST.get("contact_name", "").strip()
        party.contact_phone = request.POST.get("contact_phone", "").strip()
        party.color = request.POST.get("color", "#6b7280").strip()
        party.is_coalition_member = request.POST.get("is_coalition_member") == "on"
        party.coalition_order = int(request.POST.get("coalition_order", 0) or 0)
        party.is_active = request.POST.get("is_active") == "on"
        party.save()
        messages.success(request, f"Fraktion '{name}' aktualisiert.")

    def _handle_delete_party(self, request):
        from apps.tenants.models import CouncilParty

        party_id = request.POST.get("party_id")
        party = CouncilParty.objects.filter(id=party_id, organization=self.organization).first()
        if party:
            name = party.name
            party.delete()
            messages.success(request, f"Fraktion '{name}' gelöscht.")


# =============================================================================
# PROFILE: NOTIFICATIONS TAB
# =============================================================================


class ProfileNotificationsView(WorkViewMixin, TemplateView):
    """Notification preferences within profile tabs."""

    template_name = "work/profile/notifications.html"
    permission_required = "dashboard.view"

    # Notification type categories for grouping
    NOTIFICATION_CATEGORIES = {
        "meetings": ["meeting_reminder", "meeting_updated", "meeting_cancelled"],
        "tasks": ["task_assigned", "task_due_soon", "task_completed", "task_comment"],
        "motions": ["motion_shared", "motion_comment", "motion_status"],
        "faction": ["faction_reminder", "faction_updated"],
        "organization": ["member_joined", "role_changed"],
        "support": [
            "support_created",
            "support_reply",
            "support_status",
            "support_resolved",
            "support_escalated",
        ],
        "system": ["change_request_new", "change_request_decided", "absence_deputy", "system", "announcement"],
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "notifications"

        from apps.work.notifications.models import NotificationPreference, NotificationType

        prefs, _ = NotificationPreference.objects.get_or_create(membership=self.membership)
        context["preferences"] = prefs

        # Build categorized notification types
        type_lookup = dict(NotificationType.choices)
        categorized = []
        for category_name, type_values in self.NOTIFICATION_CATEGORIES.items():
            items = []
            for val in type_values:
                if val in type_lookup:
                    items.append(
                        {
                            "value": val,
                            "label": type_lookup[val],
                            "in_app_enabled": prefs.is_type_enabled(val, "in_app"),
                            "email_enabled": prefs.is_type_enabled(val, "email"),
                            "category": category_name,
                        }
                    )
            if items:
                categorized.extend(items)

        context["notification_types"] = categorized

        return context

    def post(self, request, *args, **kwargs):
        """Update notification preferences."""
        from apps.work.notifications.models import NotificationPreference, NotificationType

        prefs, _ = NotificationPreference.objects.get_or_create(membership=self.membership)

        prefs.email_enabled = request.POST.get("email_enabled") == "on"
        prefs.email_digest = request.POST.get("email_digest", "instant")

        prefs.quiet_hours_enabled = request.POST.get("quiet_hours_enabled") == "on"
        if prefs.quiet_hours_enabled:
            start = request.POST.get("quiet_hours_start")
            end = request.POST.get("quiet_hours_end")
            if start:
                prefs.quiet_hours_start = start
            if end:
                prefs.quiet_hours_end = end

        type_settings = {}
        for ntype, _ in NotificationType.choices:
            type_settings[ntype] = {
                "in_app": request.POST.get(f"type_{ntype}_in_app") == "on",
                "email": request.POST.get(f"type_{ntype}_email") == "on",
            }
        prefs.type_settings = type_settings
        prefs.save()

        messages.success(request, "Benachrichtigungseinstellungen gespeichert.")
        return redirect("work:profile_notifications", org_slug=self.organization.slug)


# =============================================================================
# PROFILE: ABSENCE TAB
# =============================================================================


class ProfileAbsenceView(WorkViewMixin, TemplateView):
    """Absence management within profile tabs."""

    template_name = "work/profile/absence.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "absence"

        from apps.tenants.models import Membership

        from .models import MemberAbsence

        today = timezone.now().date()

        # My absences
        my_absences = MemberAbsence.objects.filter(
            membership=self.membership,
            organization=self.organization,
        )

        current = my_absences.filter(
            is_active=True,
            start_date__lte=today,
            end_date__gte=today,
        ).first()

        active_absences = (
            my_absences.filter(is_active=True, end_date__gte=today)
            .select_related("deputy__user")
            .order_by("start_date")
        )

        past_absences = (
            my_absences.filter(end_date__lt=today).select_related("deputy__user").order_by("-start_date")[:10]
        )

        # Where I'm deputy
        deputy_for = (
            MemberAbsence.objects.filter(
                deputy=self.membership,
                organization=self.organization,
                is_active=True,
                end_date__gte=today,
            )
            .select_related("membership__user")
            .order_by("start_date")
        )

        # Available deputies (other active members)
        available_deputies = (
            Membership.objects.filter(
                organization=self.organization,
                is_active=True,
            )
            .exclude(id=self.membership.id)
            .select_related("user")
            .order_by("user__first_name", "user__last_name")
        )

        context["current_absence"] = current
        context["active_absences"] = active_absences
        context["past_absences"] = past_absences
        context["deputy_for"] = deputy_for
        context["available_deputies"] = available_deputies

        return context

    def post(self, request, *args, **kwargs):
        """Handle absence actions."""
        action = request.POST.get("action")

        if action == "create_absence":
            return self._create_absence(request)
        elif action == "cancel_absence":
            return self._cancel_absence(request)

        return redirect("work:profile_absence", org_slug=self.organization.slug)

    def _create_absence(self, request):
        from datetime import datetime

        from apps.tenants.models import Membership

        from .models import MemberAbsence

        start_date = request.POST.get("start_date")
        end_date = request.POST.get("end_date")
        reason = request.POST.get("reason", "").strip()
        deputy_id = request.POST.get("deputy_id")
        auto_decline = request.POST.get("auto_decline_meetings") == "on"
        notify_dep = request.POST.get("notify_deputy") == "on"

        if not start_date or not end_date:
            messages.error(request, "Von- und Bis-Datum sind erforderlich.")
            return redirect("work:profile_absence", org_slug=self.organization.slug)

        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Ungültiges Datumsformat.")
            return redirect("work:profile_absence", org_slug=self.organization.slug)

        if end < start:
            messages.error(request, "Das Enddatum muss nach dem Startdatum liegen.")
            return redirect("work:profile_absence", org_slug=self.organization.slug)

        deputy = None
        if deputy_id:
            deputy = Membership.objects.filter(
                id=deputy_id,
                organization=self.organization,
                is_active=True,
            ).first()

        MemberAbsence.objects.create(
            organization=self.organization,
            membership=self.membership,
            start_date=start,
            end_date=end,
            reason=reason,
            deputy=deputy,
            auto_decline_meetings=auto_decline,
            notify_deputy=notify_dep,
        )

        # Auto-decline faction meetings in the absence period
        if auto_decline:
            self._auto_decline_meetings(start, end)

        # Notify deputy
        if deputy and notify_dep:
            from apps.work.notifications.models import NotificationType
            from apps.work.notifications.services import NotificationHub

            user_name = request.user.get_full_name() or request.user.email
            NotificationHub.send(
                recipient=deputy,
                notification_type=NotificationType.ABSENCE_DEPUTY,
                title="Stellvertretung zugewiesen",
                message=f"{user_name} hat Sie als Stellvertreter eingetragen ({start.strftime('%d.%m.')} – {end.strftime('%d.%m.%Y')}).",
                link=f"/work/{self.organization.slug}/profile/absence/",
                actor=self.membership,
            )

        messages.success(request, "Abwesenheit eingetragen.")
        return redirect("work:profile_absence", org_slug=self.organization.slug)

    def _auto_decline_meetings(self, start_date, end_date):
        """Set FactionAttendance to excused for meetings in the absence period."""
        try:
            from apps.work.faction.models import FactionAttendance, FactionMeeting

            meetings = FactionMeeting.objects.filter(
                organization=self.organization,
                date__range=[start_date, end_date],
                status__in=["draft", "invited", "scheduled"],
            )

            for meeting in meetings:
                FactionAttendance.objects.update_or_create(
                    meeting=meeting,
                    membership=self.membership,
                    defaults={"status": "excused"},
                )
        except Exception as e:
            logger.error(f"Failed to auto-decline meetings: {e}")

    def _cancel_absence(self, request):
        from .models import MemberAbsence

        absence_id = request.POST.get("absence_id")
        absence = get_object_or_404(
            MemberAbsence,
            id=absence_id,
            membership=self.membership,
            organization=self.organization,
        )
        absence.is_active = False
        absence.save()
        messages.success(request, "Abwesenheit storniert.")
        return redirect("work:profile_absence", org_slug=self.organization.slug)


# =============================================================================
# PROFILE: CHANGE REQUESTS TAB
# =============================================================================


class ProfileChangeRequestsView(WorkViewMixin, TemplateView):
    """Change requests within profile tabs."""

    template_name = "work/profile/change_requests.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "requests"

        from apps.common.permissions import PermissionChecker
        from apps.tenants.models import Permission, Role

        from .models import MemberChangeRequest

        # Permission check for review capability
        checker = PermissionChecker(self.membership)
        can_review = (
            checker.has_permission("members.edit")
            or checker.has_permission("organization.manage_roles")
            or checker.is_admin()
        )
        context["can_review"] = can_review

        # My requests
        context["my_requests"] = (
            MemberChangeRequest.objects.filter(
                requester=self.membership,
                organization=self.organization,
            )
            .select_related("decided_by__user")
            .order_by("-created_at")[:20]
        )

        # Pending requests (for reviewers)
        if can_review:
            context["pending_requests"] = (
                MemberChangeRequest.objects.filter(
                    organization=self.organization,
                    status="pending",
                )
                .exclude(requester=self.membership)
                .select_related("requester__user")
                .order_by("created_at")
            )
        else:
            context["pending_requests"] = []

        # Available roles
        context["available_roles"] = Role.objects.filter(organization=self.organization).order_by("name")
        context["current_role_ids"] = list(self.membership.roles.values_list("id", flat=True))

        # Available committees
        body = self.organization.body
        if body:
            from django.db.models import Q

            from insight_core.models import OParlOrganization

            today = timezone.now().date()
            context["available_committees"] = (
                OParlOrganization.objects.filter(
                    body=body,
                )
                .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
                .order_by("name")
            )
            context["current_committee_ids"] = list(self.membership.oparl_committees.values_list("id", flat=True))
        else:
            context["available_committees"] = []
            context["current_committee_ids"] = []

        # Available permissions
        context["available_permissions"] = Permission.objects.all().order_by("category", "codename")

        return context

    def post(self, request, *args, **kwargs):
        """Handle change request actions."""
        action = request.POST.get("action")

        if action == "submit_request":
            return self._submit_request(request)
        elif action == "withdraw_request":
            return self._withdraw_request(request)
        elif action == "approve_request":
            return self._approve_request(request)
        elif action == "reject_request":
            return self._reject_request(request)

        return redirect("work:profile_requests", org_slug=self.organization.slug)

    def _submit_request(self, request):
        from .models import MemberChangeRequest

        request_type = request.POST.get("request_type")
        reason = request.POST.get("reason", "").strip()

        if not request_type or not reason:
            messages.error(request, "Antragstyp und Begründung sind erforderlich.")
            return redirect("work:profile_requests", org_slug=self.organization.slug)

        # Build request_data based on type
        request_data = {}
        if request_type == "role_change":
            request_data["requested_roles"] = request.POST.getlist("requested_roles")
        elif request_type == "committee_change":
            request_data["requested_committees"] = request.POST.getlist("requested_committees")
        elif request_type == "permission_request":
            request_data["requested_permissions"] = request.POST.getlist("requested_permissions")
        else:
            messages.error(request, "Ungültiger Antragstyp.")
            return redirect("work:profile_requests", org_slug=self.organization.slug)

        change_request = MemberChangeRequest.objects.create(
            organization=self.organization,
            requester=self.membership,
            request_type=request_type,
            request_data=request_data,
            reason=reason,
        )

        # Notify admins
        from apps.work.notifications.models import NotificationType
        from apps.work.notifications.services import NotificationHub

        admins = (
            self.organization.memberships.filter(
                is_active=True,
                roles__is_admin=True,
            )
            .distinct()
            .exclude(id=self.membership.id)
        )

        user_name = request.user.get_full_name() or request.user.email
        NotificationHub.send_bulk(
            recipients=list(admins),
            notification_type=NotificationType.CHANGE_REQUEST_NEW,
            title="Neuer Änderungsantrag",
            message=f"{user_name} hat einen {change_request.get_request_type_display()} eingereicht.",
            link=f"/work/{self.organization.slug}/profile/requests/",
            actor=self.membership,
        )

        messages.success(request, "Antrag eingereicht.")
        return redirect("work:profile_requests", org_slug=self.organization.slug)

    def _withdraw_request(self, request):
        from .models import MemberChangeRequest

        request_id = request.POST.get("request_id")
        change_request = get_object_or_404(
            MemberChangeRequest,
            id=request_id,
            requester=self.membership,
            organization=self.organization,
            status="pending",
        )
        change_request.status = "withdrawn"
        change_request.save()
        messages.success(request, "Antrag zurückgezogen.")
        return redirect("work:profile_requests", org_slug=self.organization.slug)

    def _approve_request(self, request):
        from apps.common.permissions import PermissionChecker

        from .models import MemberChangeRequest

        checker = PermissionChecker(self.membership)
        if not (
            checker.has_permission("members.edit")
            or checker.has_permission("organization.manage_roles")
            or checker.is_admin()
        ):
            messages.error(request, "Keine Berechtigung.")
            return redirect("work:profile_requests", org_slug=self.organization.slug)

        request_id = request.POST.get("request_id")
        change_request = get_object_or_404(
            MemberChangeRequest,
            id=request_id,
            organization=self.organization,
            status="pending",
        )

        # Apply the change
        self._apply_change(change_request)

        change_request.status = "approved"
        change_request.decided_by = self.membership
        change_request.decided_at = timezone.now()
        change_request.save()

        # Notify requester
        from apps.work.notifications.models import NotificationType
        from apps.work.notifications.services import NotificationHub

        decider_name = request.user.get_full_name() or request.user.email
        NotificationHub.send(
            recipient=change_request.requester,
            notification_type=NotificationType.CHANGE_REQUEST_DECIDED,
            title="Antrag genehmigt",
            message=f"Ihr {change_request.get_request_type_display()} wurde von {decider_name} genehmigt.",
            link=f"/work/{self.organization.slug}/profile/requests/",
            actor=self.membership,
        )

        messages.success(request, "Antrag genehmigt und Änderung angewendet.")
        return redirect("work:profile_requests", org_slug=self.organization.slug)

    def _apply_change(self, change_request):
        """Apply the actual change when a request is approved."""
        requester = change_request.requester
        data = change_request.request_data

        if change_request.request_type == "role_change":
            role_ids = data.get("requested_roles", [])
            if role_ids:
                from apps.tenants.models import Role

                roles = Role.objects.filter(id__in=role_ids, organization=self.organization)
                requester.roles.set(roles)

        elif change_request.request_type == "committee_change":
            committee_ids = data.get("requested_committees", [])
            body = self.organization.body
            if body:
                from insight_core.models import OParlOrganization

                committees = OParlOrganization.objects.filter(id__in=committee_ids, body=body)
                requester.oparl_committees.set(committees)

        elif change_request.request_type == "permission_request":
            perm_codes = data.get("requested_permissions", [])
            if perm_codes:
                from apps.tenants.models import Permission

                perms = Permission.objects.filter(codename__in=perm_codes)
                for perm in perms:
                    requester.individual_permissions.add(perm)

    def _reject_request(self, request):
        from apps.common.permissions import PermissionChecker

        from .models import MemberChangeRequest

        checker = PermissionChecker(self.membership)
        if not (
            checker.has_permission("members.edit")
            or checker.has_permission("organization.manage_roles")
            or checker.is_admin()
        ):
            messages.error(request, "Keine Berechtigung.")
            return redirect("work:profile_requests", org_slug=self.organization.slug)

        request_id = request.POST.get("request_id")
        comment = request.POST.get("decision_comment", "").strip()

        change_request = get_object_or_404(
            MemberChangeRequest,
            id=request_id,
            organization=self.organization,
            status="pending",
        )

        change_request.status = "rejected"
        change_request.decided_by = self.membership
        change_request.decided_at = timezone.now()
        change_request.decision_comment = comment
        change_request.save()

        # Notify requester
        from apps.work.notifications.models import NotificationType
        from apps.work.notifications.services import NotificationHub

        decider_name = request.user.get_full_name() or request.user.email
        msg = f"Ihr {change_request.get_request_type_display()} wurde von {decider_name} abgelehnt."
        if comment:
            msg += f" Kommentar: {comment}"

        NotificationHub.send(
            recipient=change_request.requester,
            notification_type=NotificationType.CHANGE_REQUEST_DECIDED,
            title="Antrag abgelehnt",
            message=msg,
            link=f"/work/{self.organization.slug}/profile/requests/",
            actor=self.membership,
        )

        messages.success(request, "Antrag abgelehnt.")
        return redirect("work:profile_requests", org_slug=self.organization.slug)


# =============================================================================
# COUNCIL PARTY MANAGEMENT
# =============================================================================


# =============================================================================
# PROFILE: DATA & PRIVACY (DSGVO)
# =============================================================================


class ProfileDataPrivacyView(WorkViewMixin, TemplateView):
    """DSGVO data export, activity log, and account deletion."""

    template_name = "work/profile/data_privacy.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "data"

        user = self.request.user

        # Security events (last logins, password changes)
        from apps.accounts.models import UserSession

        context["recent_sessions"] = UserSession.objects.filter(user=user).order_by("-created_at")[:10]

        context["is_owner"] = self.organization.owner == user

        # Export history
        from .models import DataExport

        context["exports"] = DataExport.objects.filter(
            membership=self.membership,
            organization=self.organization,
        ).order_by("-created_at")[:10]

        context["has_active_export"] = DataExport.objects.filter(
            membership=self.membership,
            organization=self.organization,
            status__in=["pending", "processing"],
        ).exists()

        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")

        if action == "export_data":
            return self._export_data(request)
        elif action == "request_deletion":
            return self._request_deletion(request)

        return redirect("work:profile_data", org_slug=self.organization.slug)

    def _export_data(self, request):
        """Start async DSGVO data export."""
        from apps.work.background_tasks import generate_dsgvo_export_task

        from .models import DataExport

        # Prevent duplicate exports
        if DataExport.objects.filter(
            membership=self.membership,
            organization=self.organization,
            status__in=["pending", "processing"],
        ).exists():
            messages.info(request, "Es läuft bereits ein Export. Bitte warten Sie, bis dieser abgeschlossen ist.")
            return redirect("work:profile_data", org_slug=self.organization.slug)

        export_format = request.POST.get("format", "json")
        if export_format not in ("json", "pdf"):
            export_format = "json"

        export = DataExport.objects.create(
            organization=self.organization,
            membership=self.membership,
            export_format=export_format,
        )

        generate_dsgvo_export_task.enqueue(str(export.id))

        messages.success(request, "Ihr Datenexport wird erstellt. Sie können die Datei in Kürze herunterladen.")
        return redirect("work:profile_data", org_slug=self.organization.slug)

    def _request_deletion(self, request):
        """Handle account deletion request."""
        user = request.user

        if self.organization.owner == user:
            messages.error(
                request,
                "Als Eigentümer müssen Sie zuerst die Eigentümerschaft übertragen, bevor Sie Ihr Konto löschen können.",
            )
            return redirect("work:profile_data", org_slug=self.organization.slug)

        password = request.POST.get("password", "")
        if not user.check_password(password):
            messages.error(request, "Falsches Passwort.")
            return redirect("work:profile_data", org_slug=self.organization.slug)

        # Deactivate membership (soft delete)
        self.membership.is_active = False
        self.membership.save()

        messages.success(
            request,
            "Ihre Mitgliedschaft wurde deaktiviert. Kontaktieren Sie den Support für eine vollständige Kontolöschung.",
        )
        return redirect("work:dashboard", org_slug=self.organization.slug)


class DataExportStatusView(WorkViewMixin, View):
    """JSON API for polling export status."""

    permission_required = "dashboard.view"

    def get(self, request, *args, **kwargs):
        from .models import DataExport

        export = get_object_or_404(
            DataExport,
            id=kwargs["export_id"],
            membership=self.membership,
            organization=self.organization,
        )

        download_url = (
            reverse(
                "work:export_download",
                kwargs={"org_slug": self.organization.slug, "export_id": export.id},
            )
            if export.is_ready
            else None
        )

        return JsonResponse(
            {
                "id": str(export.id),
                "status": export.status,
                "format": export.export_format,
                "file_size": export.file_size,
                "file_size_human": export.file_size_human,
                "is_ready": export.is_ready,
                "is_in_progress": export.is_in_progress,
                "download_url": download_url,
                "error_message": export.error_message,
                "created_at": export.created_at.isoformat() if export.created_at else None,
                "completed_at": export.completed_at.isoformat() if export.completed_at else None,
            }
        )


class DataExportDownloadView(WorkViewMixin, View):
    """Serve export file for download."""

    permission_required = "dashboard.view"

    def get(self, request, *args, **kwargs):
        from .models import DataExport

        export = get_object_or_404(
            DataExport,
            id=kwargs["export_id"],
            membership=self.membership,
            organization=self.organization,
            status="completed",
        )

        file_path = export.get_absolute_path()
        if not file_path or not file_path.exists():
            raise Http404("Exportdatei nicht gefunden.")

        content_type = "application/pdf" if export.export_format == "pdf" else "application/json; charset=utf-8"
        filename = f"mandari-datenexport-{export.created_at.strftime('%Y%m%d')}.{export.export_format}"

        response = HttpResponse(file_path.read_bytes(), content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class DataExportDeleteView(WorkViewMixin, View):
    """Delete an export and its file."""

    permission_required = "dashboard.view"

    def post(self, request, *args, **kwargs):
        from .models import DataExport

        export = get_object_or_404(
            DataExport,
            id=kwargs["export_id"],
            membership=self.membership,
            organization=self.organization,
        )

        export.delete_file()
        export.delete()

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": True})

        messages.success(request, "Export wurde gelöscht.")
        return redirect("work:profile_data", org_slug=self.organization.slug)


# =============================================================================
# PROFILE: ACTIVITY OVERVIEW
# =============================================================================


class ProfileActivityView(WorkViewMixin, TemplateView):
    """Activity overview with statistics and timeline."""

    template_name = "work/profile/activity.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "activity"

        membership = self.membership
        org = self.organization

        # --- Statistics ---
        from django.db.models import Q

        from apps.work.faction.models import FactionAttendance, FactionMeeting
        from apps.work.motions.models import Motion, MotionComment
        from apps.work.tasks.models import Task

        # Tasks
        my_tasks = Task.objects.filter(organization=org).filter(Q(created_by=membership) | Q(assigned_to=membership))
        context["tasks_total"] = my_tasks.count()
        context["tasks_completed"] = my_tasks.filter(is_completed=True).count()
        context["tasks_open"] = my_tasks.filter(is_completed=False).count()

        # Motions
        context["motions_authored"] = Motion.objects.filter(organization=org, author=membership).count()

        # Motion comments
        context["motion_comments"] = MotionComment.objects.filter(motion__organization=org, author=membership).count()

        # Faction meetings
        attendance_qs = FactionAttendance.objects.filter(membership=membership, meeting__organization=org)
        context["meetings_present"] = attendance_qs.filter(status="present").count()
        context["meetings_excused"] = attendance_qs.filter(status="excused").count()
        context["meetings_absent"] = attendance_qs.filter(status="absent").count()
        context["meetings_total"] = FactionMeeting.objects.filter(organization=org, status="completed").count()

        # Meeting preparations
        from apps.work.meetings.models import AgendaItemNote, MeetingPreparation

        context["preparations"] = MeetingPreparation.objects.filter(organization=org, membership=membership).count()

        context["agenda_notes"] = AgendaItemNote.objects.filter(organization=org, author=membership).count()

        # --- Timeline (last 20 activities) ---
        timeline = []

        # Recent tasks (created or completed)
        recent_tasks = my_tasks.order_by("-updated_at")[:5]
        for t in recent_tasks:
            timeline.append(
                {
                    "date": t.updated_at,
                    "icon": "check-square",
                    "color": "green" if t.is_completed else "blue",
                    "title": f"Aufgabe: {t.title}",
                    "detail": "Erledigt" if t.is_completed else f"Status: {t.get_status_display()}",
                }
            )

        # Recent motions
        recent_motions = Motion.objects.filter(organization=org, author=membership).order_by("-updated_at")[:5]
        for m in recent_motions:
            timeline.append(
                {
                    "date": m.updated_at,
                    "icon": "file-text",
                    "color": "indigo",
                    "title": f"Antrag: {m.title}",
                    "detail": m.get_status_display(),
                }
            )

        # Recent attendance
        recent_attendance = (
            FactionAttendance.objects.filter(membership=membership, meeting__organization=org)
            .select_related("meeting")
            .order_by("-meeting__start")[:5]
        )
        for a in recent_attendance:
            timeline.append(
                {
                    "date": a.meeting.start if a.meeting.start else a.meeting.created_at,
                    "icon": "users",
                    "color": "purple",
                    "title": f"Sitzung: {a.meeting.title}",
                    "detail": a.get_status_display(),
                }
            )

        # Recent meeting preparations
        recent_preps = MeetingPreparation.objects.filter(organization=org, membership=membership).order_by(
            "-updated_at"
        )[:5]
        for p in recent_preps:
            timeline.append(
                {
                    "date": p.updated_at,
                    "icon": "clipboard-check",
                    "color": "amber",
                    "title": "Sitzungsvorbereitung",
                    "detail": f"Aktualisiert am {p.updated_at.strftime('%d.%m.%Y')}",
                }
            )

        # Sort by date descending, take top 20
        timeline.sort(key=lambda x: x["date"] if x["date"] else timezone.now(), reverse=True)
        context["timeline"] = timeline[:20]

        return context


# =============================================================================
# PROFILE: VISIBILITY & CONTACT
# =============================================================================


class ProfileVisibilityView(WorkViewMixin, TemplateView):
    """Visibility settings, bio, and contact preferences."""

    template_name = "work/profile/visibility.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "visibility"

        # Load profile settings from User.settings JSON
        user_settings = self.request.user.settings or {}
        profile = user_settings.get("profile", {})

        context["bio"] = profile.get("bio", "")
        context["show_email"] = profile.get("show_email", True)
        context["show_phone"] = profile.get("show_phone", False)
        context["preferred_contact"] = profile.get("preferred_contact", "email")
        context["contact_signal"] = profile.get("contact_signal", "")
        context["oparl_person"] = self.membership.oparl_person

        return context

    def post(self, request, *args, **kwargs):
        user = request.user
        settings = user.settings or {}
        profile = settings.get("profile", {})

        profile["bio"] = request.POST.get("bio", "").strip()[:500]
        profile["show_email"] = request.POST.get("show_email") == "on"
        profile["show_phone"] = request.POST.get("show_phone") == "on"
        profile["preferred_contact"] = request.POST.get("preferred_contact", "email")
        profile["contact_signal"] = request.POST.get("contact_signal", "").strip()[:100]

        settings["profile"] = profile
        user.settings = settings
        user.save(update_fields=["settings"])

        messages.success(request, "Sichtbarkeitseinstellungen gespeichert.")
        return redirect("work:profile_visibility", org_slug=self.organization.slug)


class RegistrationSettingsView(WorkViewMixin, TemplateView):
    """Selbstregistrierungs-Einstellungen für die Organisation."""

    template_name = "work/organization/registration_settings.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "registration"

        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

        # Domains als Text (eine pro Zeile)
        domains = self.organization.registration_email_domains or []
        context["email_domains_text"] = "\n".join(domains)

        # Rollen für Dropdown
        context["roles"] = self.organization.roles.order_by("priority", "name")

        return context

    def post(self, request, *args, **kwargs):
        org = self.organization

        org.registration_enabled = request.POST.get("registration_enabled") == "1"
        org.registration_auto_approve = request.POST.get("registration_auto_approve") == "1"

        # Domains parsen (eine pro Zeile, bereinigt)
        domains_text = request.POST.get("registration_email_domains", "")
        domains = [d.strip().lower().lstrip("@") for d in domains_text.splitlines() if d.strip()]
        org.registration_email_domains = domains

        # Standardrolle
        role_id = request.POST.get("registration_default_role", "")
        if role_id:
            from apps.tenants.models import Role

            role = Role.objects.filter(id=role_id, organization=org).first()
            org.registration_default_role = role
        else:
            org.registration_default_role = None

        org.save(
            update_fields=[
                "registration_enabled",
                "registration_email_domains",
                "registration_auto_approve",
                "registration_default_role",
            ]
        )

        messages.success(request, "Registrierungseinstellungen gespeichert.")
        return redirect("work:organization_registration", org_slug=org.slug)


class MemberApproveView(WorkViewMixin, View):
    """Ausstehende Selbstregistrierung freischalten."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Membership

        membership = get_object_or_404(
            Membership,
            id=kwargs["membership_id"],
            organization=self.organization,
            is_active=False,
        )
        membership.is_active = True
        membership.save(update_fields=["is_active"])

        messages.success(request, f"{membership.user.get_display_name()} wurde freigeschaltet.")
        return redirect("work:members", org_slug=self.organization.slug)


class MemberRejectView(WorkViewMixin, View):
    """Ausstehende Selbstregistrierung ablehnen."""

    permission_required = "members.invite"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Membership

        membership = get_object_or_404(
            Membership,
            id=kwargs["membership_id"],
            organization=self.organization,
            is_active=False,
        )
        name = membership.user.get_display_name()
        membership.delete()

        messages.success(request, f"Registrierungsanfrage von {name} wurde abgelehnt.")
        return redirect("work:members", org_slug=self.organization.slug)

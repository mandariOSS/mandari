# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView

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

        from ..models import MemberAbsence

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

        from ..models import MemberAbsence

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

        # Heimat-Kommune + Partei (Pflicht-Zuordnungen, prominent angezeigt)
        context["home_body"] = self.organization.body
        context["primary_party"] = self.organization.party_group

        # Weitere Kommunen (read-only: Änderungen nur über Support/Admin)
        home_body_id = self.organization.body_id
        context["linked_bodies"] = self.organization.get_all_bodies().exclude(pk=home_body_id).order_by("name")

        # Parteien (durch Org-Admins editierbar)
        from apps.tenants.models import PartyGroup

        context["org_parties"] = self.organization.get_all_parties().order_by("name")
        context["available_parties"] = PartyGroup.objects.filter(is_active=True).order_by("name")
        context["org_party_ids"] = [str(p.id) for p in context["org_parties"]]

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

        elif action == "update_parties":
            from apps.tenants.models import PartyGroup

            party_ids = request.POST.getlist("parties")
            parties = list(PartyGroup.objects.filter(id__in=party_ids, is_active=True))

            # Optional: neue Partei anlegen
            new_party_name = request.POST.get("new_party", "").strip()
            if new_party_name:
                existing = PartyGroup.objects.filter(name__iexact=new_party_name).first()
                if existing:
                    if existing not in parties:
                        parties.append(existing)
                else:
                    parties.append(PartyGroup.objects.create(name=new_party_name))

            # Primäre Parteigruppe (FK) bleibt immer verknüpft
            if self.organization.party_group and self.organization.party_group not in parties:
                parties.append(self.organization.party_group)

            self.organization.parties.set(parties)
            messages.success(request, "Parteizugehörigkeit gespeichert.")

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
        # Veröffentlichungs-Opt-in darf nur mit protocols.publish geändert werden
        context["can_publish_protocols"] = self.membership.has_permission("protocols.publish")

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

        # Öffentliche Protokolle (Opt-in): nur mit protocols.publish änderbar
        if self.membership.has_permission("protocols.publish"):
            self.organization.publish_protocols = request.POST.get("publish_protocols") == "on"

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

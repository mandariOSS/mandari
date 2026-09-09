# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mitgliederliste und Mitglieder-Detail (Rollen, Gremien, Berechtigungen, RIS-Verknüpfung).
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..services import ServiceError, display_name
from ._helpers import flash_error


class MemberListView(WorkViewMixin, TemplateView):
    """List of organization members."""

    template_name = "work/organization/members.html"
    permission_required = "members.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "members"

        checker = selectors.permission_checker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")
        context["can_invite_guests"] = checker.has_permission("guests.invite")

        guests = selectors.guests_with_share_counts(self.organization)
        context["guests"] = guests
        context["guest_count"] = len(guests)
        context["guest_limit"] = self.organization.guest_limit

        context["members"] = selectors.active_members(self.organization)
        context["inactive_members"] = selectors.inactive_members(self.organization)
        context["pending_invitations"] = selectors.pending_invitations(self.organization)
        context["pending_registrations"] = selectors.pending_registrations(self.organization)
        context["is_owner"] = self.organization.owner == self.request.user
        return context


class MemberDetailView(WorkViewMixin, TemplateView):
    """View and edit a member's details."""

    template_name = "work/organization/member_detail.html"
    permission_required = "members.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"

        from apps.common.permissions import get_permissions_by_category

        member = selectors.get_member_or_404(self.organization, kwargs.get("member_id"))
        checker = selectors.permission_checker(self.membership)

        context["member"] = member
        context["available_roles"] = selectors.roles_for_organization(self.organization)
        context["is_owner"] = self.organization.owner == member.user
        context["is_self"] = member.user == self.request.user
        context["can_edit"] = checker.has_permission("members.edit") or checker.is_admin()
        context["can_invite_guests"] = checker.has_permission("guests.invite") or checker.is_admin()
        if member.is_guest:
            # „Was sieht dieser Gast?“ (Issue #77): alle wirksamen Freigaben mit Entzugs-Möglichkeit
            context["guest_has_password"] = member.user.has_usable_password()
            context["guest_document_shares"] = selectors.guest_document_shares(self.organization, member.user)
            context["guest_folder_shares"] = selectors.guest_folder_shares(self.organization, member.user)
            context["can_manage_guest_shares"] = (
                checker.has_permission("guests.manage") or checker.has_permission("motions.share") or checker.is_admin()
            )

        # Effektive Berechtigungen (Matrix mit Herkunft)
        context["permission_categories"] = get_permissions_by_category()
        context.update(selectors.permission_matrix(member))

        # Gremienzuordnung (OParl-Gremien aller verknüpften Kommunen) + RIS-Personen-Vorschläge
        context.update(selectors.committee_context(self.organization, member))

        # Fachgebiete (Themenkatalog)
        context["org_topics"] = selectors.org_topics(self.organization)
        context["member_expertise_ids"] = selectors.expertise_ids(member)
        return context

    def post(self, request, *args, **kwargs):
        """Handle member updates."""
        member = selectors.get_member_or_404(self.organization, kwargs.get("member_id"))
        checker = selectors.permission_checker(self.membership)
        if not checker.has_permission("members.edit") and not checker.is_admin():
            messages.error(request, "Keine Berechtigung zum Bearbeiten von Mitgliedern.")
            return redirect("work:members", org_slug=self.organization.slug)

        handler = {
            "update_committees": self._update_committees,
            "update_expertise": self._update_expertise,
            "update_roles": self._update_roles,
            "update_permissions": self._update_permissions,
            "deactivate": self._deactivate,
            "reactivate": self._reactivate,
            "remove": self._remove,
            "transfer_ownership": self._transfer_ownership,
            "link_oparl_person": self._link_oparl_person,
            "unlink_oparl_person": self._unlink_oparl_person,
            "apply_suggestions": self._apply_suggestions,
            "update_sworn_in": self._update_sworn_in,
        }.get(request.POST.get("action"))

        response = None
        if handler is not None:
            try:
                response = handler(request, member)
            except ServiceError as exc:
                flash_error(request, exc)
        return response or redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)

    def _update_committees(self, request, member):
        services.update_member_committees(self.organization, member, request.POST.getlist("committees"))
        messages.success(request, f"Gremien für {display_name(member.user)} aktualisiert.")

    def _update_expertise(self, request, member):
        services.update_member_expertise(self.organization, member, request.POST.getlist("expertise_topics"))
        messages.success(request, f"Fachgebiete für {display_name(member.user)} aktualisiert.")

    def _update_roles(self, request, member):
        services.update_member_roles(self.organization, member, self.membership, request.POST.getlist("roles"))
        messages.success(request, f"Rollen für {display_name(member.user)} aktualisiert.")

    def _update_permissions(self, request, member):
        services.update_member_permissions(
            member,
            self.membership,
            request.POST.getlist("individual_permissions"),
            request.POST.getlist("denied_permissions"),
        )
        messages.success(request, f"Individuelle Berechtigungen für {display_name(member.user)} aktualisiert.")

    def _deactivate(self, request, member):
        services.deactivate_member(self.organization, member, request.user)
        messages.success(request, f"{display_name(member.user)} wurde deaktiviert.")
        return redirect("work:members", org_slug=self.organization.slug)

    def _reactivate(self, request, member):
        services.reactivate_member(self.organization, member)
        messages.success(request, f"{display_name(member.user)} wurde reaktiviert.")

    def _remove(self, request, member):
        name = services.remove_member(self.organization, member, request.user)
        messages.success(request, f"{name} wurde aus der Organisation entfernt.")
        return redirect("work:members", org_slug=self.organization.slug)

    def _transfer_ownership(self, request, member):
        services.transfer_ownership(self.organization, member, request.user)
        messages.success(request, f"Eigentümerschaft wurde auf {display_name(member.user)} übertragen.")

    def _link_oparl_person(self, request, member):
        person_name = services.link_oparl_person(self.organization, member, request.POST.get("oparl_person_id"))
        messages.success(request, f"RIS-Person '{person_name}' verknüpft.")

    def _unlink_oparl_person(self, request, member):
        services.unlink_oparl_person(member)
        messages.success(request, "RIS-Verknüpfung entfernt.")

    def _apply_suggestions(self, request, member):
        count = services.apply_committee_suggestions(self.organization, member)
        messages.success(request, f"{count} Gremien übernommen.")

    def _update_sworn_in(self, request, member):
        is_sworn_in = request.POST.get("is_sworn_in") == "1"
        services.update_sworn_in(member, self.membership, is_sworn_in)
        if is_sworn_in:
            messages.success(request, f"{display_name(member.user)} wurde als vereidigt markiert.")
        else:
            messages.success(request, f"Vereidigungsstatus für {display_name(member.user)} wurde entfernt.")

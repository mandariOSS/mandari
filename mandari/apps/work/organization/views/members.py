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

        # Get all active members (ohne Gäste — die haben eine eigene Sektion)
        members = (
            Membership.objects.filter(organization=self.organization, is_active=True, is_guest=False)
            .select_related("user")
            .prefetch_related("roles")
            .order_by("user__first_name", "user__last_name")
        )

        # Gäste: eigener Abschnitt mit Zähler gegen das Gast-Limit
        guests = (
            Membership.objects.filter(organization=self.organization, is_active=True, is_guest=True)
            .select_related("user")
            .order_by("user__first_name", "user__last_name")
        )
        context["guests"] = guests
        context["guest_count"] = guests.count()
        context["guest_limit"] = self.organization.guest_limit
        context["can_invite_guests"] = checker.has_permission("guests.invite")

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

    def _find_matching_persons(self, user, bodies):
        """Findet OParl-Personen anhand des Benutzernamens."""
        from django.db.models import Q

        from insight_core.models import OParlPerson

        first = user.first_name.strip() if user.first_name else ""
        last = user.last_name.strip() if user.last_name else ""

        if not first and not last:
            return []

        query = Q(body__in=bodies)
        if first and last:
            query &= (
                Q(given_name__iexact=first, family_name__iexact=last)
                | Q(name__icontains=f"{first} {last}")
                | Q(name__icontains=f"{last}, {first}")
            )
        elif last:
            query &= Q(family_name__iexact=last) | Q(name__icontains=last)

        return list(OParlPerson.objects.filter(query)[:5])

    def _get_suggested_committees(self, oparl_person, bodies, today):
        """Holt aktive Gremienmitgliedschaften einer OParl-Person."""
        from django.db.models import Q

        from insight_core.models import OParlMembership

        memberships = (
            OParlMembership.objects.filter(person=oparl_person, organization__body__in=bodies)
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

        # === Effektive Berechtigungen (Matrix mit Herkunft) ===
        # Drei Zustände je Berechtigung: aus Rollen (read-only, mit Herkunft),
        # individuell hinzugefügt, explizit verweigert (schlägt Rollen).
        from apps.common.permissions import PERMISSIONS, get_permissions_by_category

        context["permission_categories"] = get_permissions_by_category()

        role_permission_sources = {}  # codename -> Liste der Rollennamen
        for role in member.roles.all():
            if role.is_admin:
                for code in PERMISSIONS:
                    role_permission_sources.setdefault(code, []).append(f"{role.name} (Administrator)")
            else:
                for perm in role.permissions.all():
                    role_permission_sources.setdefault(perm.codename, []).append(role.name)

        individual_codes = set(member.individual_permissions.values_list("codename", flat=True))
        denied_codes = set(member.denied_permissions.values_list("codename", flat=True))

        context["role_permission_sources"] = role_permission_sources
        context["individual_permission_codes"] = individual_codes
        context["denied_permission_codes"] = denied_codes
        context["effective_permission_codes"] = (set(role_permission_sources) | individual_codes) - denied_codes

        # Committee assignment (OParl committees from all linked bodies)
        bodies = self.organization.get_all_bodies()
        has_body = bodies.exists()
        context["has_body"] = has_body

        if has_body:
            from insight_core.models import OParlOrganization

            today = timezone.now().date()

            # Gremien nach Status aufteilen
            all_committees = OParlOrganization.objects.filter(body__in=bodies)

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
                suggested = self._get_suggested_committees(member.oparl_person, bodies, today)
                context["suggested_committee_ids"] = [c.id for c in suggested]
            else:
                # Namens-Matching für Vorschläge
                context["potential_oparl_persons"] = self._find_matching_persons(member.user, bodies)
        else:
            context["active_committees"] = []
            context["inactive_committees"] = []
            context["member_committees"] = []
            context["oparl_person"] = None
            context["suggested_committee_ids"] = []
            context["potential_oparl_persons"] = []

        # Fachgebiete (Themenkatalog)
        from apps.tenants.models import Topic

        context["org_topics"] = Topic.objects.filter(organization=self.organization)
        context["member_expertise_ids"] = set(member.expertise_topics.values_list("id", flat=True))

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
            bodies = self.organization.get_all_bodies()
            if bodies.exists():
                from insight_core.models import OParlOrganization

                committees = OParlOrganization.objects.filter(id__in=committee_ids, body__in=bodies)
                member.oparl_committees.set(committees)
                messages.success(
                    request,
                    f"Gremien für {member.user.get_full_name() or member.user.email} aktualisiert.",
                )
            else:
                messages.error(request, "Keine Kommune verknüpft. Gremien können nicht zugewiesen werden.")

        elif action == "update_expertise":
            # Fachgebiete des Mitglieds setzen (Themenkatalog der Organisation)
            from apps.tenants.models import Topic

            topic_ids = request.POST.getlist("expertise_topics")
            topics = Topic.objects.filter(id__in=topic_ids, organization=self.organization)
            member.expertise_topics.set(topics)
            messages.success(
                request,
                f"Fachgebiete für {member.user.get_full_name() or member.user.email} aktualisiert.",
            )

        elif action == "update_roles":
            # Update member roles
            if member.is_guest:
                messages.error(request, "Gast-Zugänge können keine Rollen erhalten.")
                return redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)
            role_ids = request.POST.getlist("roles")
            roles = Role.objects.filter(id__in=role_ids, organization=self.organization)
            # Rechte-Eskalation verhindern: Nicht-Admins dürfen weder ihre
            # eigenen Rollen ändern noch die Administrator-Rolle vergeben oder
            # entziehen. Admins bleiben uneingeschränkt.
            if not checker.is_admin():
                if member.user == request.user:
                    messages.error(request, "Eigene Rollen können nur Administratoren ändern.")
                    return redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)
                if member.roles.filter(is_admin=True).exists() or roles.filter(is_admin=True).exists():
                    messages.error(
                        request, "Nur Administratoren können die Administrator-Rolle vergeben oder entziehen."
                    )
                    return redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)
            member.roles.set(roles)
            messages.success(
                request,
                f"Rollen für {member.user.get_full_name() or member.user.email} aktualisiert.",
            )

        elif action == "update_permissions":
            # Individuelle/verweigerte Berechtigungen (Matrix im Mitglieder-Detail)
            if member.is_guest:
                messages.error(request, "Gast-Zugänge haben keine Berechtigungen.")
                return redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)

            # Das direkte Setzen einzelner/verweigerter Rechte ist eine
            # administrative Operation (kann bis organization.admin gewähren)
            # und bleibt Administratoren vorbehalten — sonst könnte ein
            # members.edit-Mitglied sich selbst oder andere hochstufen.
            if not checker.is_admin():
                messages.error(request, "Individuelle Berechtigungen können nur Administratoren ändern.")
                return redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)

            from apps.tenants.models import Permission

            individual_codes = request.POST.getlist("individual_permissions")
            denied_codes = request.POST.getlist("denied_permissions")
            member.individual_permissions.set(Permission.objects.filter(codename__in=individual_codes))
            member.denied_permissions.set(Permission.objects.filter(codename__in=denied_codes))
            messages.success(
                request,
                f"Individuelle Berechtigungen für {member.user.get_full_name() or member.user.email} aktualisiert.",
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
            # Gast-Limit gilt auch bei Reaktivierung (sonst wäre es per
            # Deaktivieren/Reaktivieren umgehbar)
            if member.is_guest and not member.is_active and not self.organization.has_free_guest_slot():
                messages.error(
                    request,
                    f"Gast-Limit erreicht ({self.organization.guest_limit}). Erweiterung als Addon im Kundenportal.",
                )
                return redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)
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
            bodies = self.organization.get_all_bodies()
            if bodies.exists() and person_id:
                from insight_core.models import OParlPerson

                oparl_person = get_object_or_404(OParlPerson, id=person_id, body__in=bodies)
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
            bodies = self.organization.get_all_bodies()
            if bodies.exists() and member.oparl_person:
                today = timezone.now().date()
                suggested = self._get_suggested_committees(member.oparl_person, bodies, today)
                member.oparl_committees.set(suggested)
                messages.success(request, f"{len(suggested)} Gremien übernommen.")
            else:
                messages.error(request, "Keine RIS-Person verknüpft oder keine Kommune zugeordnet.")

        elif action == "update_sworn_in":
            # Update sworn-in status (Vereidigung)
            # Selbst-Vereidigung verhindern: der Vereidigungsstatus schaltet
            # den Zugriff auf nicht-öffentliche Inhalte frei (can_access_non_public)
            # und darf von Nicht-Admins nicht am eigenen Konto gesetzt werden.
            if member.user == request.user and not checker.is_admin():
                messages.error(request, "Den eigenen Vereidigungsstatus können nur Administratoren setzen.")
                return redirect("work:member_detail", org_slug=self.organization.slug, member_id=member.id)
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

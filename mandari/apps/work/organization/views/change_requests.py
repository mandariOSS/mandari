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

        from ..models import MemberChangeRequest

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
        bodies = self.organization.get_all_bodies()
        if bodies.exists():
            from django.db.models import Q

            from insight_core.models import OParlOrganization

            today = timezone.now().date()
            context["available_committees"] = (
                OParlOrganization.objects.filter(
                    body__in=bodies,
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
        from ..models import MemberChangeRequest

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
        from ..models import MemberChangeRequest

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

        from ..models import MemberChangeRequest

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

        # Rechte-Eskalation über den Antragsweg verhindern (gleiche Invariante
        # wie im Mitglieder-Detail): Nicht-Admins dürfen weder eigene Anträge
        # genehmigen noch Anträge, die Administrator-Rollen oder direkte
        # Berechtigungen gewähren.
        if not checker.is_admin() and not self._approver_may_apply(change_request):
            messages.error(
                request,
                "Diese Genehmigung ist Administratoren vorbehalten "
                "(eigener Antrag oder Vergabe administrativer Rechte).",
            )
            return redirect("work:profile_requests", org_slug=self.organization.slug)

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

    def _approver_may_apply(self, change_request) -> bool:
        """Darf ein Nicht-Admin diesen Antrag genehmigen?

        Nein bei eigenem Antrag, bei Vergabe direkter Berechtigungen und bei
        role_change, der eine Administrator-Rolle enthält.
        """
        if change_request.requester == self.membership:
            return False
        if change_request.request_type == "permission_request":
            return False
        if change_request.request_type == "role_change":
            from apps.tenants.models import Role

            role_ids = change_request.request_data.get("requested_roles", [])
            if Role.objects.filter(id__in=role_ids, organization=self.organization, is_admin=True).exists():
                return False
        return True

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
            bodies = self.organization.get_all_bodies()
            if bodies.exists():
                from insight_core.models import OParlOrganization

                committees = OParlOrganization.objects.filter(id__in=committee_ids, body__in=bodies)
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

        from ..models import MemberChangeRequest

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

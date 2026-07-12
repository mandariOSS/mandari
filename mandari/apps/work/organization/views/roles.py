# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


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


class RoleResetView(WorkViewMixin, View):
    """
    Setzt eine Standard-Rolle auf ihre Definition aus setup_roles zurück.

    Nur für Rollen, deren Name einer Standard-Rolle entspricht (POST only).
    """

    permission_required = "organization.manage_roles"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Role

        role = get_object_or_404(Role, id=kwargs["role_id"], organization=self.organization)

        if role.reset_to_default():
            messages.success(request, f"Rolle '{role.name}' wurde auf den Standard zurückgesetzt.")
        else:
            messages.error(request, f"Für '{role.name}' existiert keine Standard-Definition.")
        return redirect("work:roles", org_slug=self.organization.slug)


class RoleRestoreDefaultsView(WorkViewMixin, View):
    """
    Legt fehlende Standard-Rollen an (POST only).

    Bestehende Rollen — auch angepasste — bleiben unverändert.
    """

    permission_required = "organization.manage_roles"

    def post(self, request, *args, **kwargs):
        from apps.tenants.models import Permission, Role

        # Sicherstellen, dass der Berechtigungskatalog aktuell ist
        Permission.sync_permissions()
        created = Role.restore_missing_default_roles(self.organization)
        if created:
            names = ", ".join(r.name for r in created)
            messages.success(request, f"{len(created)} Standard-Rolle(n) angelegt: {names}.")
        else:
            messages.info(request, "Alle Standard-Rollen sind bereits vorhanden.")
        return redirect("work:roles", org_slug=self.organization.slug)

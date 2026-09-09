# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollenverwaltung der Organisation.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.common.permissions import get_permissions_by_category

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error


def _role_input(request, color_default: str = "#6b7280", priority_default: str = "50") -> services.RoleInput:
    """Formularfelder einer Rolle einlesen."""
    return services.RoleInput(
        name=request.POST.get("name", "").strip(),
        description=request.POST.get("description", "").strip(),
        color=request.POST.get("color", color_default).strip(),
        priority_raw=request.POST.get("priority", priority_default),
        is_admin=request.POST.get("is_admin") == "on",
        require_2fa=request.POST.get("require_2fa") == "on",
        permission_codes=request.POST.getlist("permissions"),
    )


class RoleListView(WorkViewMixin, TemplateView):
    """List and manage roles."""

    template_name = "work/organization/roles.html"
    permission_required = "organization.manage_roles"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "roles"
        context["can_manage_faction"] = selectors.permission_checker(self.membership).has_permission("faction.manage")
        context["roles"] = selectors.roles_with_member_count(self.organization)
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
        context["permission_categories"] = get_permissions_by_category()
        return context

    def post(self, request, *args, **kwargs):
        try:
            role = services.create_role(self.organization, _role_input(request))
        except ServiceError as exc:
            flash_error(request, exc)
            return redirect("work:role_create", org_slug=self.organization.slug)
        messages.success(request, f"Rolle '{role.name}' wurde erstellt.")
        return redirect("work:roles", org_slug=self.organization.slug)


class RoleEditView(WorkViewMixin, TemplateView):
    """Edit an existing role."""

    template_name = "work/organization/role_form.html"
    permission_required = "organization.manage_roles"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["is_edit"] = True
        role = selectors.get_role_or_404(self.organization, kwargs["role_id"])
        context["role"] = role
        context["role_permissions"] = selectors.role_permission_codes(role)
        context["permission_categories"] = get_permissions_by_category()
        return context

    def post(self, request, *args, **kwargs):
        role = selectors.get_role_or_404(self.organization, kwargs["role_id"])
        try:
            services.update_role(self.organization, role, _role_input(request, role.color, str(role.priority)))
        except ServiceError as exc:
            flash_error(request, exc)
            return redirect("work:role_edit", org_slug=self.organization.slug, role_id=role.id)
        messages.success(request, f"Rolle '{role.name}' wurde aktualisiert.")
        return redirect("work:roles", org_slug=self.organization.slug)


class RoleDeleteView(WorkViewMixin, View):
    """Delete a role (POST only)."""

    permission_required = "organization.manage_roles"

    def post(self, request, *args, **kwargs):
        role = selectors.get_role_or_404(self.organization, kwargs["role_id"])
        try:
            name = services.delete_role(role)
        except ServiceError as exc:
            flash_error(request, exc)
        else:
            messages.success(request, f"Rolle '{name}' wurde gelöscht.")
        return redirect("work:roles", org_slug=self.organization.slug)


class RoleResetView(WorkViewMixin, View):
    """
    Setzt eine Standard-Rolle auf ihre Definition aus setup_roles zurück.

    Nur für Rollen, deren Name einer Standard-Rolle entspricht (POST only).
    """

    permission_required = "organization.manage_roles"

    def post(self, request, *args, **kwargs):
        role = selectors.get_role_or_404(self.organization, kwargs["role_id"])
        if services.reset_role(role):
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
        created = services.restore_default_roles(self.organization)
        if created:
            names = ", ".join(r.name for r in created)
            messages.success(request, f"{len(created)} Standard-Rolle(n) angelegt: {names}.")
        else:
            messages.info(request, "Alle Standard-Rollen sind bereits vorhanden.")
        return redirect("work:roles", org_slug=self.organization.slug)

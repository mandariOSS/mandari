# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollen- und Rechteverwaltung für die Verwaltung selbst.

Jeder Mandant pflegt seine Rollen mit einer Rechte-Matrix — von den
Standardrollen (Administrator, Sachbearbeitung, …) bis zu eigenen Rollen
wie „Geräteverwaltung" oder „Kämmerei". Damit bleibt das System modular:
Neue Funktionsbereiche (z. B. Endgeräte) sind einzelne Rechte, die die
Verwaltung frei kombinieren kann.

Schutzmechanismen:
- Die letzte Admin-Rolle mit aktiven Nutzern kann weder gelöscht noch
  entmachtet werden (sonst sperrt sich der Mandant aus).
- Rollen mit zugewiesenen Nutzern können nicht gelöscht werden.
"""

from django.contrib import messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from .. import audit
from ..models import SessionRole, SessionUser
from ..permissions import SessionViewMixin

# Gruppierung der Rechte für die Matrix; unbekannte can_*-Felder landen
# automatisch unter „Sonstiges" (zukunftssicher bei neuen Rechten).
PERMISSION_GROUPS = [
    (
        "Sitzungen",
        [
            "can_view_meetings",
            "can_create_meetings",
            "can_edit_meetings",
            "can_delete_meetings",
            "can_view_non_public_meetings",
            "can_manage_attendance",
        ],
    ),
    (
        "Vorlagen",
        [
            "can_view_papers",
            "can_create_papers",
            "can_edit_papers",
            "can_delete_papers",
            "can_approve_papers",
            "can_view_non_public_papers",
        ],
    ),
    (
        "Anträge & Protokolle",
        [
            "can_view_applications",
            "can_process_applications",
            "can_view_protocols",
            "can_create_protocols",
            "can_edit_protocols",
            "can_approve_protocols",
        ],
    ),
    (
        "Finanzen & Endgeräte",
        [
            "can_manage_allowances",
            "can_manage_devices",
        ],
    ),
    (
        "Verwaltung",
        [
            "can_manage_users",
            "can_manage_organizations",
            "can_manage_settings",
            "can_view_audit_log",
        ],
    ),
    (
        "Zugänge",
        [
            "can_view_dashboard",
            "can_access_api",
            "can_access_oparl_api",
        ],
    ),
]


def permission_fields():
    """Alle can_*-Felder der Rolle mit Label, gruppiert für die Matrix."""
    fields = {f.name: f.verbose_name for f in SessionRole._meta.get_fields() if f.name.startswith("can_")}
    grouped = []
    seen = set()
    for group_name, names in PERMISSION_GROUPS:
        entries = [(n, fields[n]) for n in names if n in fields]
        seen.update(n for n, _ in entries)
        if entries:
            grouped.append((group_name, entries))
    leftover = [(n, label) for n, label in sorted(fields.items()) if n not in seen]
    if leftover:
        grouped.append(("Sonstiges", leftover))
    return grouped


def _get_role(view, raw_id):
    if not raw_id:
        return None
    try:
        return SessionRole.objects.filter(tenant=view.session_tenant, pk=raw_id).first()
    except (ValueError, DjangoValidationError):
        return None


def _is_last_admin_role(view, role) -> bool:
    """Ist dies die letzte Admin-Rolle, über die aktive Nutzer Admin sind?"""
    if not role.is_admin:
        return False
    has_admins_via_role = SessionUser.objects.filter(tenant=view.session_tenant, is_active=True, roles=role).exists()
    if not has_admins_via_role:
        return False
    other_admin_access = (
        SessionUser.objects.filter(tenant=view.session_tenant, is_active=True, roles__is_admin=True)
        .exclude(roles=role)
        .exists()
    )
    return not other_admin_access


class RoleListView(SessionViewMixin, TemplateView):
    """Rollenübersicht mit Rechte-Matrix zum Bearbeiten."""

    template_name = "session/settings/roles.html"
    permission_required = "manage_users"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        roles = list(SessionRole.objects.filter(tenant=self.session_tenant).order_by("-is_admin", "name"))
        member_counts = {
            role.pk: SessionUser.objects.filter(tenant=self.session_tenant, roles=role).count() for role in roles
        }
        for role in roles:
            role.member_count = member_counts[role.pk]
        edit_role = _get_role(self, self.request.GET.get("edit"))
        edit_role_permissions = []
        if edit_role:
            edit_role_permissions = [
                field_name
                for _group, entries in permission_fields()
                for field_name, _label in entries
                if getattr(edit_role, field_name)
            ]
        import json

        context.update(
            {
                "roles": roles,
                "edit_role": edit_role,
                "edit_role_permissions": json.dumps(edit_role_permissions),
                "permission_groups": permission_fields(),
            }
        )
        return context


class RoleSaveView(SessionViewMixin, View):
    """Rolle anlegen oder bearbeiten (inkl. Rechte-Matrix)."""

    permission_required = "manage_users"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        name = request.POST.get("name", "").strip()[:100]
        if not name:
            messages.error(request, "Bitte einen Rollennamen angeben.")
            return redirect("session:settings_roles", tenant_slug=tenant_slug)

        role = _get_role(self, request.POST.get("role_id"))
        creating = role is None
        if creating:
            if SessionRole.objects.filter(tenant=self.session_tenant, name=name).exists():
                messages.error(request, f"Eine Rolle „{name}“ existiert bereits.")
                return redirect("session:settings_roles", tenant_slug=tenant_slug)
            role = SessionRole(tenant=self.session_tenant)

        wants_admin = request.POST.get("is_admin") == "1"
        # Schutz: Die letzte Admin-Rolle darf nicht entmachtet werden
        if not creating and role.is_admin and not wants_admin and _is_last_admin_role(self, role):
            messages.error(
                request,
                "Diese Rolle ist der letzte Administrator-Zugang des Mandanten und kann nicht entmachtet werden.",
            )
            return redirect("session:settings_roles", tenant_slug=tenant_slug)

        role.name = name
        role.description = request.POST.get("description", "").strip()
        role.is_admin = wants_admin
        for _group, entries in permission_fields():
            for field_name, _label in entries:
                setattr(role, field_name, request.POST.get(field_name) == "1")
        role.save()

        granted = [
            field_name
            for _group, entries in permission_fields()
            for field_name, _label in entries
            if getattr(role, field_name)
        ]
        audit.log_event(
            "create" if creating else "update",
            role,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "rolle": name,
                "admin": wants_admin,
                "rechte": [f[4:] for f in granted],
            },
        )
        messages.success(request, f"Rolle „{name}“ gespeichert.")
        return redirect("session:settings_roles", tenant_slug=tenant_slug)


class RoleDeleteView(SessionViewMixin, View):
    """Rolle löschen (nur ohne zugewiesene Nutzer, nie die letzte Admin-Rolle)."""

    permission_required = "manage_users"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        role = _get_role(self, request.POST.get("role_id"))
        if role is None:
            messages.error(request, "Rolle nicht gefunden.")
            return redirect("session:settings_roles", tenant_slug=tenant_slug)

        if _is_last_admin_role(self, role):
            messages.error(
                request,
                "Diese Rolle ist der letzte Administrator-Zugang und kann nicht gelöscht werden.",
            )
            return redirect("session:settings_roles", tenant_slug=tenant_slug)

        assigned = SessionUser.objects.filter(tenant=self.session_tenant, roles=role).count()
        if assigned:
            messages.error(
                request,
                f"Rolle „{role.name}“ ist noch {assigned} Benutzer(n) zugewiesen — "
                "bitte zuerst die Zuweisungen ändern.",
            )
            return redirect("session:settings_roles", tenant_slug=tenant_slug)

        audit.log_event(
            "delete",
            role,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={"rolle": role.name},
        )
        role.delete()
        messages.success(request, f"Rolle „{role.name}“ gelöscht.")
        return redirect("session:settings_roles", tenant_slug=tenant_slug)

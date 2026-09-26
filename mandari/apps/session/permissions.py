# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session permission system.

Provides:
- Permission checking utilities
- Permission-based view mixins
- Role-based access control
"""

import time
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404

from apps.common.mixins import HTMXMixin

USER_TENANTS_MAX_AGE = 600
USER_TENANTS_SESSION_KEY = "session_user_tenants"
USER_GROUPS_SESSION_KEY = "session_user_leitstellen"

#: Kontrollrechte (Issue #221): Protokoll einsehen bzw. exportieren und prüfen. Funktionstrennung:
#: Sie sind nicht in der Administrator-Vollmacht enthalten und werden je Rolle vergeben.
AUDIT_PERMISSIONS = frozenset({"view_audit_log", "export_audit_log"})


def user_tenants(request: Any) -> list[dict[str, str]]:
    """
    Aktive Mandanten des angemeldeten Nutzers für den Mandantenwechsel (slug, name).

    Liegt in der ohnehin geladenen Login-Session und wird höchstens alle zehn Minuten
    erneuert – so kostet die Seitenleiste keine zusätzliche Abfrage je Seitenaufruf
    (Performance-Budgets). Neue Mitgliedschaften erscheinen spätestens nach zehn Minuten.
    """
    user = request.user
    jetzt = time.time()
    eintrag = request.session.get(USER_TENANTS_SESSION_KEY)
    if (
        not isinstance(eintrag, dict)
        or eintrag.get("u") != str(user.pk)
        or jetzt - float(eintrag.get("t", 0)) > USER_TENANTS_MAX_AGE
    ):
        liste = [
            {"slug": slug, "name": name}
            for slug, name in user.session_memberships.filter(is_active=True, tenant__is_active=True)
            .order_by("tenant__name")
            .values_list("tenant__slug", "tenant__name")
        ]
        eintrag = {"u": str(user.pk), "t": jetzt, "list": liste}
        request.session[USER_TENANTS_SESSION_KEY] = eintrag
    return list(eintrag["list"])


def user_leitstellen(request: Any) -> list[dict[str, str]]:
    """
    Leitstellen (Mandantengruppen) des angemeldeten Nutzers für den Link in der Seitenleiste (Issue #317).

    Zwischengespeichert wie :func:`user_tenants` – die Seitenleiste kostet keine zusätzliche Abfrage.
    Der Link ist nur ein Wegweiser: Die Übersicht prüft die Mitgliedschaft bei jedem Aufruf selbst.
    """
    user = request.user
    jetzt = time.time()
    eintrag = request.session.get(USER_GROUPS_SESSION_KEY)
    if (
        not isinstance(eintrag, dict)
        or eintrag.get("u") != str(user.pk)
        or jetzt - float(eintrag.get("t", 0)) > USER_TENANTS_MAX_AGE
    ):
        liste = [
            {"slug": slug, "name": name}
            for slug, name in user.session_group_memberships.filter(is_active=True, group__is_active=True)
            .order_by("group__name")
            .values_list("group__slug", "group__name")
        ]
        eintrag = {"u": str(user.pk), "t": jetzt, "list": liste}
        request.session[USER_GROUPS_SESSION_KEY] = eintrag
    return list(eintrag["list"])


# Vollständiger Rechtesatz der Administrator-Rolle – ohne die Kontrollrechte (AUDIT_PERMISSIONS),
# die auch Administratoren nur über die Einzelhaken ihrer Rolle erhalten (Issue #221)
ALL_PERMISSIONS = frozenset(
    {
        "view_dashboard",
        "view_meetings",
        "create_meetings",
        "edit_meetings",
        "delete_meetings",
        "view_non_public_meetings",
        "view_papers",
        "create_papers",
        "edit_papers",
        "delete_papers",
        "approve_papers",
        "view_non_public_papers",
        "view_applications",
        "process_applications",
        "view_protocols",
        "create_protocols",
        "edit_protocols",
        "approve_protocols",
        "manage_attendance",
        "manage_allowances",
        "manage_devices",
        "manage_users",
        "manage_organizations",
        "manage_settings",
        "access_api",
        "access_oparl_api",
    }
)


def role_permissions(session_user: Any) -> set[str]:
    """
    Rechte aus den eigenen Rollen – ohne Vertretungen.

    Grundlage für Vertretungen (Issue #222): Eine Vertretung erhält höchstens diese Rechte
    der vertretenen Person, nie deren Rechte aus weiteren Vertretungen (keine Kettenvertretung).

    Administratoren haben alle Fachrechte; die Kontrollrechte (Protokoll einsehen und
    exportieren, Issue #221) kommen auch bei ihnen nur aus den Einzelhaken der Rolle.
    """
    if not session_user:
        return set()
    permissions: set[str] = set()
    for role in session_user.roles.all():
        if role.is_admin:
            permissions |= ALL_PERMISSIONS
        # Collect individual permissions from role
        for attr in dir(role):
            if attr.startswith("can_") and getattr(role, attr, False):
                # Convert can_view_meetings to view_meetings
                permissions.add(attr[4:])
    return permissions


def is_admin_user(session_user: Any) -> bool:
    """Hat die Person eine Administrator-Rolle? (nutzt vorgeladene Rollen)"""
    if not session_user:
        return False
    return any(role.is_admin for role in session_user.roles.all())


def grantable_permissions(session_user: Any) -> set[str]:
    """
    Rechte, die diese Person über Rollen, Rollenzuweisungen, Einladungen und Vertretungen
    vergeben oder entziehen darf: nur Rechte aus den eigenen Rollen – nie aus Vertretungen.

    Die Kontrollrechte (Issue #221) vergeben nur Administratoren. Sie haben sie selbst meist nicht
    (Funktionstrennung), richten aber die Rolle der Revision bzw. des Datenschutzes ein.
    """
    own = role_permissions(session_user)
    if is_admin_user(session_user):
        return own | AUDIT_PERMISSIONS
    return own - AUDIT_PERMISSIONS


def role_flags(role: Any) -> set[str]:
    """Rechte, die eine Rolle über ihre Einzelhaken gewährt (ohne ``can_``)."""
    return {
        field.name[4:]
        for field in role._meta.concrete_fields
        if field.name.startswith("can_") and getattr(role, field.name)
    }


def role_within_scope(role: Any, grantable: set[str], *, admin: bool) -> bool:
    """Darf jemand mit diesem Umfang die Rolle zuweisen oder entziehen? (Administratoren: jede Rolle)"""
    if admin:
        return True
    return not role.is_admin and role_flags(role) <= grantable


class SessionPermissionChecker:
    """
    Utility class for checking permissions.

    Usage:
        checker = SessionPermissionChecker(session_user)
        if checker.has_permission("view_meetings"):
            # ...
    """

    def __init__(self, session_user: Any) -> None:
        """
        Initialize with a SessionUser instance.

        Args:
            session_user: The SessionUser to check permissions for
        """
        self.session_user = session_user
        self._permissions_cache = None

    @property
    def permissions(self) -> set:
        """Get all permissions as a set (cached)."""
        if self._permissions_cache is None:
            self._permissions_cache = self._collect_permissions()
        return self._permissions_cache

    def _collect_permissions(self) -> set:
        """Rechte aus den eigenen Rollen plus Freigaberechte aus aktiven Vertretungen (Issue #222)."""
        if not self.session_user:
            return set()
        own = role_permissions(self.session_user)
        from apps.session.services import delegation_service

        return own | delegation_service.delegated_permissions(self.session_user, own)

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission."""
        return permission in self.permissions

    def has_any_permission(self, permissions: list[str]) -> bool:
        """Check if user has at least one of the permissions."""
        return bool(set(permissions) & self.permissions)

    def has_all_permissions(self, permissions: list[str]) -> bool:
        """Check if user has all of the permissions."""
        return set(permissions).issubset(self.permissions)

    def is_admin(self) -> bool:
        """Check if user is an administrator."""
        if not self.session_user:
            return False
        return self.session_user.roles.filter(is_admin=True).exists()


class SessionMixin(LoginRequiredMixin):
    """
    Base mixin for all Session views.

    Ensures:
    - User is logged in
    - Tenant context is set
    - Session user exists for this tenant

    Attributes:
        session_tenant: The current SessionTenant
        session_user: The current SessionUser
    """

    session_tenant = None
    session_user = None
    _session_permissions = None

    @property
    def session_permissions(self) -> set[str]:
        """Rechte der angemeldeten Person, einmal je Anfrage berechnet – Grundlage für ``visible_to()``."""
        if self._session_permissions is None:
            self._session_permissions = SessionPermissionChecker(self.session_user).permissions
        return self._session_permissions

    def dispatch(self, request, *args, **kwargs):
        """Set up session context before view processing."""
        from apps.session.models import SessionTenant, SessionUser

        # Get tenant slug from URL
        tenant_slug = kwargs.get("tenant_slug")
        if not tenant_slug:
            raise Http404("Kein Mandant angegeben")

        # Get tenant
        try:
            self.session_tenant = SessionTenant.objects.get(slug=tenant_slug, is_active=True)
        except SessionTenant.DoesNotExist:
            raise Http404("Mandant nicht gefunden") from None

        # Set on request
        request.session_tenant = self.session_tenant

        # Get session user for current user
        if request.user.is_authenticated:
            from apps.session.services.delegation_service import annotate_active

            try:
                # Vertretungen (Issue #222): ob heute eine wirkt, kommt ohne eigene Abfrage mit
                self.session_user = annotate_active(
                    SessionUser.objects.select_related("tenant").prefetch_related("roles")
                ).get(
                    user=request.user,
                    tenant=self.session_tenant,
                    is_active=True,
                )
            except SessionUser.DoesNotExist:
                raise PermissionDenied("Kein Zugang zu diesem Mandanten") from None
        else:
            # LoginRequiredMixin will handle redirect
            pass

        request.session_user = self.session_user

        # Sicherheit: Berechtigungen VOR der eigentlichen View-Verarbeitung
        # prüfen (insbesondere vor POST-Mutationen). Anonyme Nutzer werden
        # weiterhin von LoginRequiredMixin zum Login umgeleitet.
        if request.user.is_authenticated:
            self.check_view_permissions()

        return super().dispatch(request, *args, **kwargs)

    def check_view_permissions(self):
        """Hook für Berechtigungsprüfungen (Default: keine)."""
        return

    def get_context_data(self, **kwargs):
        """Add session context to templates."""
        context = super().get_context_data(**kwargs)
        context["session_tenant"] = self.session_tenant
        context["session_user"] = self.session_user
        context["tenant_slug"] = self.session_tenant.slug
        checker = SessionPermissionChecker(self.session_user)
        context["permission_checker"] = checker
        # Mandantenwechsel (z. B. Bezirke mit gemeinsamer Leitstelle): aktive Mandanten des Nutzers
        context["user_tenants"] = user_tenants(self.request)
        # Leitstellen-Übersicht (Issue #317): nur ein Wegweiser, der Zugriff wird dort selbst geprüft
        context["user_leitstellen"] = user_leitstellen(self.request)

        # Arbeitsvorrat-Badge (Issue #33): Anzahl zu prüfender Vorlagen
        if checker.has_permission("approve_papers"):
            from apps.session.models import SessionPaper

            review_qs = SessionPaper.objects.filter(tenant=self.session_tenant, status="review")
            context["papers_review_count"] = review_qs.visible_to(checker.permissions).count()

        # Arbeitsvorrat-Badge (Issue #81): offene Mitzeichnungen der eigenen Ämter
        if checker.has_permission("view_papers"):
            from apps.session.services import cosign_service

            context["cosign_count"] = cosign_service.my_pending_cosignatures(
                self.session_user, checker.permissions
            ).count()

        return context

    def get_queryset(self):
        """Filter queryset by tenant if applicable."""
        qs = super().get_queryset()
        if hasattr(qs.model, "tenant"):
            return qs.filter(tenant=self.session_tenant)
        return qs


class SessionPermissionMixin(SessionMixin):
    """
    Mixin that checks for specific permissions.

    Usage:
        class MyView(SessionPermissionMixin, TemplateView):
            permission_required = "view_meetings"
            # or
            permission_required = ["view_meetings", "edit_meetings"]

        # For any-of semantics:
        class MyView(SessionPermissionMixin, TemplateView):
            permission_required = ["edit_meetings", "manage_meetings"]
            permission_require_all = False
    """

    permission_required: str | list[str] | None = None
    permission_require_all: bool = True

    def check_view_permissions(self):
        """Berechtigungen prüfen — läuft VOR der View-Verarbeitung (SessionMixin)."""
        if self.permission_required:
            self.check_permissions()

    def check_permissions(self):
        """Verify the user has required permissions."""
        if not self.session_user:
            raise PermissionDenied("Nicht authentifiziert")

        checker = SessionPermissionChecker(self.session_user)

        # Normalize to list
        permissions = self.permission_required
        if isinstance(permissions, str):
            permissions = [permissions]

        # Check permissions
        if self.permission_require_all:
            if not checker.has_all_permissions(permissions):
                raise PermissionDenied("Fehlende Berechtigung")
        else:
            if not checker.has_any_permission(permissions):
                raise PermissionDenied("Fehlende Berechtigung")

    def has_permission(self, permission: str) -> bool:
        """
        Check if current user has a permission.

        Useful in templates: {% if view.has_permission "create_meetings" %}
        """
        if not self.session_user:
            return False
        return SessionPermissionChecker(self.session_user).has_permission(permission)


class SessionViewMixin(HTMXMixin, SessionPermissionMixin):
    """
    Combined mixin for Session views.

    Combines:
    - Login required
    - Tenant context
    - Permission checking
    - HTMX support
    """

    pass

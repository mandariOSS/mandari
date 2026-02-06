# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session permission system.

Provides:
- Permission checking utilities
- Permission-based view decorators
- Role-based access control
"""

from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404


class SessionPermissionChecker:
    """
    Utility class for checking permissions.

    Usage:
        checker = SessionPermissionChecker(session_user)
        if checker.has_permission("view_meetings"):
            # ...
    """

    def __init__(self, session_user):
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
        """Collect all permissions from roles."""
        if not self.session_user:
            return set()

        permissions = set()

        for role in self.session_user.roles.all():
            # Admin has all permissions
            if role.is_admin:
                # Return all possible permissions
                return {
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
                    "manage_users",
                    "manage_organizations",
                    "manage_settings",
                    "view_audit_log",
                    "access_api",
                    "access_oparl_api",
                }

            # Collect individual permissions from role
            for attr in dir(role):
                if attr.startswith("can_"):
                    if getattr(role, attr, False):
                        # Convert can_view_meetings to view_meetings
                        perm_name = attr[4:]  # Remove 'can_' prefix
                        permissions.add(perm_name)

        return permissions

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
            raise Http404("Mandant nicht gefunden")

        # Set on request
        request.session_tenant = self.session_tenant

        # Get session user for current user
        if request.user.is_authenticated:
            try:
                self.session_user = (
                    SessionUser.objects.select_related("tenant")
                    .prefetch_related("roles")
                    .get(
                        user=request.user,
                        tenant=self.session_tenant,
                        is_active=True,
                    )
                )
            except SessionUser.DoesNotExist:
                raise PermissionDenied("Kein Zugang zu diesem Mandanten")
        else:
            # LoginRequiredMixin will handle redirect
            pass

        request.session_user = self.session_user

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        """Add session context to templates."""
        context = super().get_context_data(**kwargs)
        context["session_tenant"] = self.session_tenant
        context["session_user"] = self.session_user
        context["tenant_slug"] = self.session_tenant.slug
        context["permission_checker"] = SessionPermissionChecker(self.session_user)
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

    def dispatch(self, request, *args, **kwargs):
        """Check permissions before view processing."""
        # First set up session context
        response = super().dispatch(request, *args, **kwargs)

        # Check permissions
        if self.permission_required:
            self.check_permissions()

        return response

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


class HTMXMixin:
    """
    Mixin for HTMX-enabled views.

    Provides:
    - is_htmx: Check if request is from HTMX
    - htmx_trigger: Trigger client-side events
    - htmx_redirect: Redirect with HX-Redirect header
    """

    @property
    def is_htmx(self) -> bool:
        """Check if the request is from HTMX."""
        return self.request.headers.get("HX-Request") == "true"

    def htmx_trigger(self, event: str, detail: dict = None) -> dict:
        """Create headers to trigger a client-side event."""
        if detail:
            return {event: detail}
        return event

    def get_template_names(self):
        """Select partial template for HTMX requests."""
        templates = super().get_template_names()

        if self.is_htmx:
            # Try to find partial versions
            partial_templates = []
            for template in templates:
                partial = template.replace(".html", "_partial.html")
                partial_templates.append(partial)
            return partial_templates + templates

        return templates


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


def session_permission_required(permission: str | list[str], require_all: bool = True):
    """
    Decorator for function-based views that require permissions.

    Usage:
        @session_permission_required("view_meetings")
        def my_view(request, tenant_slug):
            ...

        @session_permission_required(["edit_meetings", "create_meetings"])
        def my_view(request, tenant_slug):
            ...
    """

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            # Get session user from request
            session_user = getattr(request, "session_user", None)
            if not session_user:
                raise PermissionDenied("Nicht authentifiziert")

            checker = SessionPermissionChecker(session_user)

            # Normalize to list
            perms = permission if isinstance(permission, list) else [permission]

            # Check permissions
            if require_all:
                if not checker.has_all_permissions(perms):
                    raise PermissionDenied("Fehlende Berechtigung")
            else:
                if not checker.has_any_permission(perms):
                    raise PermissionDenied("Fehlende Berechtigung")

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    return decorator

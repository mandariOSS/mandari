# SPDX-License-Identifier: AGPL-3.0-or-later
"""
View mixins for the Work module.

Provides:
- OrganizationMixin: Base mixin for all organization-scoped views
- PermissionRequiredMixin: Permission checking for views
- HTMXMixin: HTMX-specific functionality
"""

from typing import List, Optional

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import redirect

from .permissions import PermissionChecker


class OrganizationMixin(LoginRequiredMixin):
    """
    Base mixin for all organization-scoped views.

    Extracts the organization from the URL (org_slug) and validates
    that the current user is a member.

    Attributes:
        organization: The current Organization instance
        membership: The user's Membership in the organization

    URL pattern should include: path('<slug:org_slug>/...', ...)
    """

    organization = None
    membership = None

    def setup_organization_context(self, request, **kwargs):
        """
        Set up organization and membership context.

        Returns True if context was set up successfully.
        Raises Http404 or PermissionDenied on failure.
        """
        # Import here to avoid circular imports
        from apps.tenants.models import Membership, Organization

        # Get organization slug from URL
        org_slug = kwargs.get("org_slug")
        if not org_slug:
            raise Http404("Keine Organisation angegeben")

        # Get organization
        try:
            self.organization = Organization.objects.get(
                slug=org_slug,
                is_active=True
            )
        except Organization.DoesNotExist:
            raise Http404("Organisation nicht gefunden")

        # Get membership for current user
        try:
            self.membership = Membership.objects.select_related(
                "organization"
            ).prefetch_related(
                "roles__permissions",
                "individual_permissions",
                "denied_permissions"
            ).get(
                user=request.user,
                organization=self.organization,
                is_active=True
            )
        except Membership.DoesNotExist:
            raise PermissionDenied("Kein Zugang zu dieser Organisation")

        # Set on request for easy access
        request.organization = self.organization
        request.membership = self.membership

        return True

    def dispatch(self, request, *args, **kwargs):
        """Set up organization context before view processing."""
        # IMPORTANT: First check if user is authenticated
        # If not, redirect to login (handled by LoginRequiredMixin)
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        # Set up organization context
        self.setup_organization_context(request, **kwargs)

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        """Add organization context to templates."""
        context = super().get_context_data(**kwargs)
        context["organization"] = self.organization
        context["membership"] = self.membership
        context["org_slug"] = self.organization.slug
        return context

    def get_queryset(self):
        """Filter queryset by organization if applicable."""
        qs = super().get_queryset()
        if hasattr(qs.model, "organization"):
            return qs.filter(organization=self.organization)
        return qs


class PermissionRequiredMixin(OrganizationMixin):
    """
    Mixin that checks for specific permissions.

    Usage:
        class MyView(PermissionRequiredMixin, TemplateView):
            permission_required = "motions.create"
            # or
            permission_required = ["motions.create", "motions.edit"]

        # For any-of semantics:
        class MyView(PermissionRequiredMixin, TemplateView):
            permission_required = ["motions.edit", "motions.edit_all"]
            permission_require_all = False
    """

    permission_required: Optional[str | List[str]] = None
    permission_require_all: bool = True  # Require all permissions by default

    def dispatch(self, request, *args, **kwargs):
        """Check permissions before view processing."""
        # IMPORTANT: First check if user is authenticated
        # If not, redirect to login (handled by LoginRequiredMixin)
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        # Set up organization context (reuses parent's method)
        self.setup_organization_context(request, **kwargs)

        # SECURITY: Check permissions BEFORE processing the view
        if self.permission_required:
            self.check_permissions()

        # Now process the actual view via parent's dispatch chain
        return LoginRequiredMixin.dispatch(self, request, *args, **kwargs)

    def check_permissions(self):
        """Verify the user has required permissions."""
        from django.conf import settings

        if not self.membership:
            raise PermissionDenied("Nicht authentifiziert")

        checker = PermissionChecker(self.membership)

        # Normalize to list
        permissions = self.permission_required
        if isinstance(permissions, str):
            permissions = [permissions]

        # Debug logging only in DEBUG mode
        if settings.DEBUG:
            import logging
            logger = logging.getLogger("apps.common.mixins")
            logger.info(f"[PermCheck] User: {self.membership.user.email}")
            logger.info(f"[PermCheck] Roles: {list(self.membership.roles.values_list('name', flat=True))}")
            logger.info(f"[PermCheck] Checking: {permissions}")
            logger.info(f"[PermCheck] is_admin: {checker.is_admin()}")

            # Check each permission individually for debugging
            for perm in permissions:
                has_perm = checker.has_permission(perm)
                logger.info(f"[PermCheck]   {perm}: {has_perm}")

        # Check permissions
        if self.permission_require_all:
            if not checker.has_all_permissions(permissions):
                if settings.DEBUG:
                    logger.warning(f"[PermCheck] DENIED - missing required permissions")
                raise PermissionDenied("Fehlende Berechtigung")
        else:
            if not checker.has_any_permission(permissions):
                if settings.DEBUG:
                    logger.warning(f"[PermCheck] DENIED - no matching permissions")
                raise PermissionDenied("Fehlende Berechtigung")

        if settings.DEBUG:
            logger.info(f"[PermCheck] GRANTED")

    def has_permission(self, permission: str) -> bool:
        """
        Check if current user has a permission.

        Useful in templates: {% if view.has_permission "motions.create" %}
        """
        if not self.membership:
            return False
        return PermissionChecker(self.membership).has_permission(permission)


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
        """
        Create headers to trigger a client-side event.

        Usage:
            response["HX-Trigger"] = json.dumps(self.htmx_trigger("itemCreated"))
        """
        if detail:
            return {event: detail}
        return event

    def htmx_redirect(self, url: str) -> HttpResponse:
        """
        Redirect for HTMX requests.

        HTMX ignores normal redirects, so we use HX-Redirect header.
        """
        if self.is_htmx:
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)

    def get_template_names(self):
        """
        Select partial template for HTMX requests.

        If is_htmx and a *_partial.html template exists, use it.
        """
        templates = super().get_template_names()

        if self.is_htmx:
            # Try to find partial versions
            partial_templates = []
            for template in templates:
                # Convert template.html to template_partial.html
                partial = template.replace(".html", "_partial.html")
                partial_templates.append(partial)
            # Partials first, then fallback to full templates
            return partial_templates + templates

        return templates


class WorkViewMixin(HTMXMixin, PermissionRequiredMixin):
    """
    Combined mixin for Work module views.

    Combines:
    - Login required
    - Organization context
    - Permission checking
    - HTMX support
    """
    pass

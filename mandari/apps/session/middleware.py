# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session middleware for tenant isolation and security.

Provides:
- SessionTenantMiddleware: Sets the current tenant context
- Automatic RLS enforcement via database connection
"""

from django.http import Http404
from django.utils.deprecation import MiddlewareMixin

from apps.session.audit import clear_current_request, set_current_request


class SessionTenantMiddleware(MiddlewareMixin):
    """
    Middleware that sets the tenant context for Session requests.

    For all requests to /session/<tenant_slug>/, this middleware:
    1. Extracts the tenant slug from the URL
    2. Validates the tenant exists and is active
    3. Sets request.session_tenant for views to use

    Security:
    - Validates tenant exists before processing
    - Ensures tenant isolation at the request level
    """

    def process_request(self, request):
        """Extract and validate tenant from URL."""
        # Only process /session/ URLs
        if not request.path.startswith("/session/"):
            request.session_tenant = None
            request.session_user = None
            return None

        # Extract tenant slug from path
        # Format: /session/<tenant_slug>/...
        parts = request.path.strip("/").split("/")
        if len(parts) < 2:
            request.session_tenant = None
            request.session_user = None
            return None

        tenant_slug = parts[1]

        # Skip for static paths like /session/static/ und die
        # tenant-unabhängige Einladungs-Annahme (/session/invite/<token>/)
        if tenant_slug in ("static", "api", "health", "invite"):
            request.session_tenant = None
            request.session_user = None
            return None

        # Import here to avoid circular imports
        from apps.session.models import SessionTenant, SessionUser

        # Get tenant
        try:
            tenant = SessionTenant.objects.get(slug=tenant_slug, is_active=True)
            request.session_tenant = tenant
        except SessionTenant.DoesNotExist:
            raise Http404("Mandant nicht gefunden")

        # Get session user if authenticated
        request.session_user = None
        if request.user.is_authenticated:
            try:
                request.session_user = (
                    SessionUser.objects.select_related("tenant")
                    .prefetch_related("roles")
                    .get(
                        user=request.user,
                        tenant=tenant,
                        is_active=True,
                    )
                )
                # Update last access
                from django.utils import timezone

                SessionUser.objects.filter(pk=request.session_user.pk).update(last_access=timezone.now())
            except SessionUser.DoesNotExist:
                # User is authenticated but not a member of this tenant
                pass

        # Request für das Audit-Logging bereitstellen (Thread-Local)
        set_current_request(request)

        return None

    def process_response(self, request, response):
        """Thread-Local-Request nach der Verarbeitung aufräumen."""
        clear_current_request()
        return response

    def process_exception(self, request, exception):
        """Thread-Local-Request auch bei Fehlern aufräumen."""
        clear_current_request()
        return None


def get_current_tenant(request):
    """
    Helper to get the current tenant from request.

    Usage in views:
        from apps.session.middleware import get_current_tenant
        tenant = get_current_tenant(self.request)
    """
    return getattr(request, "session_tenant", None)


def get_current_session_user(request):
    """
    Helper to get the current session user from request.

    Usage in views:
        from apps.session.middleware import get_current_session_user
        session_user = get_current_session_user(self.request)
    """
    return getattr(request, "session_user", None)

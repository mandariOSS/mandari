# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Middleware for tenant context management.

Extracts the organization from URLs and sets it on the request.
Handles subdomain redirects for organizations.
"""

import re

from django.conf import settings
from django.http import HttpResponseRedirect


class OrganizationMiddleware:
    """
    Middleware to extract organization context from URL.

    Sets request.organization and request.membership for Work module URLs.
    """

    # Pattern to extract org_slug from Work URLs
    WORK_URL_PATTERN = re.compile(r"^/work/([^/]+)/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Initialize as None
        request.organization = None
        request.membership = None

        # Try to extract organization from URL
        org_slug = self._get_org_slug(request.path)

        if org_slug:
            self._set_organization_context(request, org_slug)

        response = self.get_response(request)
        return response

    def _get_org_slug(self, path: str) -> str | None:
        """Extract organization slug from URL path."""
        match = self.WORK_URL_PATTERN.match(path)
        if match:
            return match.group(1)
        return None

    def _set_organization_context(self, request, org_slug: str):
        """Set organization and membership on request."""
        # Import here to avoid circular imports at startup
        from apps.tenants.models import Membership, Organization

        try:
            organization = Organization.objects.select_related("party_group", "body").get(slug=org_slug, is_active=True)
            request.organization = organization

            # Get membership if user is authenticated
            if request.user.is_authenticated:
                try:
                    membership = (
                        Membership.objects.select_related("organization")
                        .prefetch_related("roles__permissions", "individual_permissions", "denied_permissions")
                        .get(user=request.user, organization=organization, is_active=True)
                    )
                    request.membership = membership
                except Membership.DoesNotExist:
                    # User is not a member of this organization
                    pass

        except Organization.DoesNotExist:
            # Organization not found - let view handle 404
            pass


class SubdomainRedirectMiddleware:
    """
    Middleware to handle organization subdomain redirects.

    Redirects requests from subdomains like 'volt.mandari.de' to '/work/volt/'.
    Only redirects the root path - all other paths are passed through.

    Configuration in settings.py:
        MAIN_DOMAIN = 'mandari.de'
        SUBDOMAIN_REDIRECT_ENABLED = True  # Set to False to disable
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # Main domain without www (e.g., 'mandari.de')
        self.main_domain = getattr(settings, "MAIN_DOMAIN", "mandari.de")
        self.enabled = getattr(settings, "SUBDOMAIN_REDIRECT_ENABLED", True)

    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)

        # Get the host from the request (e.g., 'volt.mandari.de')
        host = request.get_host().lower()

        # Remove port if present
        if ":" in host:
            host = host.split(":")[0]

        # Check if this is a subdomain of our main domain
        subdomain = self._extract_subdomain(host)

        if subdomain and subdomain not in ("www", "mail", "api", "admin"):
            # This is an organization subdomain - redirect to work portal
            return self._handle_subdomain_redirect(request, subdomain)

        return self.get_response(request)

    def _extract_subdomain(self, host: str) -> str | None:
        """
        Extract subdomain from host.

        Examples:
            'volt.mandari.de' -> 'volt'
            'mandari.de' -> None
            'www.mandari.de' -> 'www'
            'localhost' -> None
        """
        # Skip localhost and IP addresses
        if host in ("localhost", "127.0.0.1") or host.startswith("192.168."):
            return None

        # Check if host ends with our main domain
        main_domain = self.main_domain.lower()
        if not host.endswith(f".{main_domain}"):
            return None

        # Extract the subdomain part
        subdomain = host[: -len(f".{main_domain}")]

        # Only return if it's a simple subdomain (no dots)
        if "." in subdomain:
            return None

        return subdomain

    def _handle_subdomain_redirect(self, request, subdomain: str):
        """
        Redirect subdomain request to organization work portal.

        Only redirects root path (/) to /work/<org_slug>/dashboard/
        Other paths get passed through with the subdomain stripped.
        """
        # Import here to avoid circular imports
        from apps.tenants.models import Organization

        # Check if organization exists with this slug
        if not Organization.objects.filter(slug=subdomain, is_active=True).exists():
            # Organization not found - let normal routing handle it
            return self.get_response(request)

        # Build the redirect URL
        path = request.path
        if path == "/" or path == "":
            # Root path -> redirect to dashboard
            redirect_path = f"/work/{subdomain}/dashboard/"
        elif path.startswith("/work/"):
            # Already a work path - don't redirect
            return self.get_response(request)
        else:
            # Other paths -> redirect to work portal with same path
            redirect_path = f"/work/{subdomain}{path}"

        # Build the full URL with the main domain
        scheme = "https" if request.is_secure() else "http"
        redirect_url = f"{scheme}://{self.main_domain}{redirect_path}"

        # Preserve query string
        if request.META.get("QUERY_STRING"):
            redirect_url += f"?{request.META['QUERY_STRING']}"

        return HttpResponseRedirect(redirect_url)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Middleware for tenant context management.

Extracts the organization from URLs and sets it on the request.
"""

import re
from typing import Optional

from django.http import Http404


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

    def _get_org_slug(self, path: str) -> Optional[str]:
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
            organization = Organization.objects.select_related(
                "party_group",
                "body"
            ).get(
                slug=org_slug,
                is_active=True
            )
            request.organization = organization

            # Get membership if user is authenticated
            if request.user.is_authenticated:
                try:
                    membership = Membership.objects.select_related(
                        "organization"
                    ).prefetch_related(
                        "roles__permissions",
                        "individual_permissions",
                        "denied_permissions"
                    ).get(
                        user=request.user,
                        organization=organization,
                        is_active=True
                    )
                    request.membership = membership
                except Membership.DoesNotExist:
                    # User is not a member of this organization
                    pass

        except Organization.DoesNotExist:
            # Organization not found - let view handle 404
            pass

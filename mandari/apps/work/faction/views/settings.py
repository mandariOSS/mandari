# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Faction meeting views for the Work module.

Simplified architecture: 4 views instead of 13.
- FactionMeetingListView: List + Create (POST)
- FactionMeetingDetailView: Detail/Protocol page
- FactionActionView: Central HTMX action handler
- FactionSettingsView: Legacy redirect to organization settings
"""

import logging

from django.shortcuts import redirect
from django.views.generic import View

from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 5. Settings (legacy redirect)
# ---------------------------------------------------------------------------


class FactionSettingsView(WorkViewMixin, View):
    """Legacy redirect - settings are now in organization settings."""

    permission_required = "faction.manage"

    def get(self, request, *args, **kwargs):
        return redirect("work:organization_faction_settings", org_slug=self.organization.slug)

    def post(self, request, *args, **kwargs):
        return redirect("work:organization_faction_settings", org_slug=self.organization.slug)

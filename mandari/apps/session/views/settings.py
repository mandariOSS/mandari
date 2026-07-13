# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session views.

Provides views for the Session RIS administration interface.
"""

from django.views.generic import (
    ListView,
    TemplateView,
)

from ..models import (
    SessionUser,
)
from ..permissions import SessionViewMixin

# =============================================================================
# SETTINGS
# =============================================================================


class SettingsView(SessionViewMixin, TemplateView):
    """Tenant settings view."""

    template_name = "session/settings/index.html"
    permission_required = "manage_settings"


class UserListView(SessionViewMixin, ListView):
    """List of session users."""

    model = SessionUser
    template_name = "session/settings/users.html"
    context_object_name = "session_users"
    paginate_by = 50
    permission_required = "manage_users"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.select_related("user").prefetch_related("roles").order_by("user__email")

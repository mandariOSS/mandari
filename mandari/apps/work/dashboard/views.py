# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dashboard views for the Work module.
"""

from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin


class DashboardView(WorkViewMixin, TemplateView):
    """Main dashboard view showing overview of all work areas."""

    template_name = "work/dashboard/index.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "dashboard"
        # TODO: Add dashboard data
        # context["upcoming_meetings"] = self.get_upcoming_meetings()
        # context["my_tasks"] = self.get_my_tasks()
        # context["recent_activity"] = self.get_recent_activity()
        return context

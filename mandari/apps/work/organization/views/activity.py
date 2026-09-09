# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Profil: Aktivitätsübersicht mit Kennzahlen und Zeitleiste.
"""

from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors


class ProfileActivityView(WorkViewMixin, TemplateView):
    """Activity overview with statistics and timeline."""

    template_name = "work/profile/activity.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "activity"
        context.update(selectors.activity_statistics(self.organization, self.membership))
        context["timeline"] = selectors.activity_timeline(self.organization, self.membership)
        return context

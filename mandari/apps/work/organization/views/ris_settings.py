# SPDX-License-Identifier: AGPL-3.0-or-later
"""Organisationseinstellungen: Verbindung zur Verwaltung (Einreichungs-Token, Issue #40)."""

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.motions import ris_submission

from .. import selectors, services


class OrganizationRisSettingsView(WorkViewMixin, TemplateView):
    """Reiter „Verwaltung“: Token hinterlegen, Status sehen, Verbindung trennen."""

    template_name = "work/organization/ris_settings.html"
    permission_required = "faction.manage"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        connection = ris_submission.get_connection(self.organization)
        usable, reason = ris_submission.connection_state(connection)
        context["active_nav"] = "organization"
        context["active_tab"] = "ris"
        context["can_manage_faction"] = True
        context["connection"] = connection
        context["connection_usable"] = usable
        context["connection_reason"] = reason
        context["submitted_applications"] = selectors.submitted_applications(self.organization)
        return context

    def post(self, request, *args, **kwargs):
        if request.POST.get("action") == "disconnect":
            if services.disconnect_ris(self.organization):
                messages.success(request, "Die Verbindung zur Verwaltung wurde getrennt.")
        else:
            try:
                connection = ris_submission.connect_with_token(
                    self.organization, request.POST.get("token", ""), self.membership
                )
            except ris_submission.SubmissionError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request, f"Verbunden mit {connection.tenant.name}. Anträge können jetzt eingereicht werden."
                )
        return redirect("work:organization_ris_settings", org_slug=self.organization.slug)

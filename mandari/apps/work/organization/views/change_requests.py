# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Profil: Änderungsanträge (Rollen, Gremien, Berechtigungen) einreichen und prüfen.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error


class ProfileChangeRequestsView(WorkViewMixin, TemplateView):
    """Change requests within profile tabs."""

    template_name = "work/profile/change_requests.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = None
        context["active_tab"] = "requests"

        can_review = selectors.can_review_change_requests(self.membership)
        context["can_review"] = can_review
        context["my_requests"] = selectors.my_change_requests(self.organization, self.membership)
        context["pending_requests"] = (
            selectors.pending_change_requests(self.organization, exclude=self.membership) if can_review else []
        )
        context.update(selectors.change_request_form_context(self.organization, self.membership))
        return context

    def post(self, request, *args, **kwargs):
        """Handle change request actions."""
        handler = {
            "submit_request": self._submit_request,
            "withdraw_request": self._withdraw_request,
            "approve_request": self._approve_request,
            "reject_request": self._reject_request,
        }.get(request.POST.get("action"))
        if handler is not None:
            try:
                handler(request)
            except ServiceError as exc:
                flash_error(request, exc)
        return redirect("work:profile_requests", org_slug=self.organization.slug)

    def _submit_request(self, request):
        request_type = request.POST.get("request_type")
        requested = {
            "role_change": request.POST.getlist("requested_roles"),
            "committee_change": request.POST.getlist("requested_committees"),
            "permission_request": request.POST.getlist("requested_permissions"),
        }.get(request_type, [])
        services.submit_change_request(
            self.organization,
            self.membership,
            request_type,
            request.POST.get("reason", "").strip(),
            requested,
        )
        messages.success(request, "Antrag eingereicht.")

    def _withdraw_request(self, request):
        services.withdraw_change_request(self.organization, self.membership, request.POST.get("request_id"))
        messages.success(request, "Antrag zurückgezogen.")

    def _approve_request(self, request):
        services.approve_change_request(self.organization, self.membership, request.POST.get("request_id"))
        messages.success(request, "Antrag genehmigt und Änderung angewendet.")

    def _reject_request(self, request):
        services.reject_change_request(
            self.organization,
            self.membership,
            request.POST.get("request_id"),
            request.POST.get("decision_comment", "").strip(),
        )
        messages.success(request, "Antrag abgelehnt.")

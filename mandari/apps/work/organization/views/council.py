# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratsfraktionen, Koalition und Verwaltungskontakte verwalten.
"""

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error


class CouncilPartyListView(WorkViewMixin, TemplateView):
    """Ratsfraktionen, Koalition und Verwaltungskontakte verwalten."""

    template_name = "work/organization/parties.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "parties"
        context["can_manage_faction"] = selectors.permission_checker(self.membership).has_permission("faction.manage")

        parties = selectors.council_parties(self.organization)
        context["parties"] = parties
        context["coalition_parties"] = parties.filter(is_coalition_member=True)
        context["other_parties"] = parties.filter(is_coalition_member=False)
        context["admin_contacts"] = selectors.admin_contacts(self.organization)
        context["coalition_name"] = self.organization.coalition_name
        return context

    def post(self, request, *args, **kwargs):
        handler = {
            "update_coalition": self._update_coalition,
            "add_admin_contact": self._add_admin_contact,
            "delete_admin_contact": self._delete_admin_contact,
            "add_party": self._add_party,
            "update_party": self._update_party,
            "delete_party": self._delete_party,
        }.get(request.POST.get("action"))
        if handler is not None:
            try:
                handler(request)
            except ServiceError as exc:
                flash_error(request, exc)
        return redirect("work:council_parties", org_slug=self.organization.slug)

    @staticmethod
    def _party_input(request) -> services.PartyInput:
        """Formularfelder einer Ratsfraktion einlesen."""
        return services.PartyInput(
            name=request.POST.get("name", "").strip(),
            short_name=request.POST.get("short_name", "").strip(),
            email=request.POST.get("email", "").strip(),
            contact_name=request.POST.get("contact_name", "").strip(),
            contact_phone=request.POST.get("contact_phone", "").strip(),
            color=request.POST.get("color", "#6b7280").strip(),
            is_coalition_member=request.POST.get("is_coalition_member") == "on",
            coalition_order=int(request.POST.get("coalition_order", 0) or 0),
            is_active=request.POST.get("is_active") == "on",
        )

    def _update_coalition(self, request):
        services.update_coalition_name(self.organization, request.POST.get("coalition_name", "").strip())
        messages.success(request, "Koalitionsname gespeichert.")

    def _add_admin_contact(self, request):
        contact = services.add_admin_contact(
            self.organization,
            request.POST.get("contact_label", "").strip(),
            request.POST.get("contact_email", "").strip(),
        )
        messages.success(request, f"Kontakt '{contact.label}' hinzugefügt.")

    def _delete_admin_contact(self, request):
        if services.delete_admin_contact(self.organization, request.POST.get("contact_id")):
            messages.success(request, "Kontakt entfernt.")

    def _add_party(self, request):
        party = services.add_party(self.organization, self._party_input(request))
        messages.success(request, f"Fraktion '{party.name}' hinzugefügt.")

    def _update_party(self, request):
        party = selectors.find_party(self.organization, request.POST.get("party_id"))
        if party is None:
            return
        services.update_party(self.organization, party, self._party_input(request))
        messages.success(request, f"Fraktion '{party.name}' aktualisiert.")

    def _delete_party(self, request):
        party = selectors.find_party(self.organization, request.POST.get("party_id"))
        if party is not None:
            name = services.delete_party(party)
            messages.success(request, f"Fraktion '{name}' gelöscht.")

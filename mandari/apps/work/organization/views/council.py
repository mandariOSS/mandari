# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratsfraktionen, Koalition und Verwaltungskontakte verwalten.

Im neuen Erscheinungsbild haben die Verwaltungskontakte seit Issue #1230 eine eigene Unterseite
(Einstellungen › Rat und Verwaltung › Verwaltungskontakte). Die Seite der Ratsfraktionen nimmt ihre Aktionen weiter an,
weil das bisherige Erscheinungsbild (``work/organization/parties.html``) sie dort zeigt.
"""

from typing import Any

from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.common.params import int_param
from apps.work.rahmen import NeueVorlageMixin

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error


class _KontakteMixin:
    """Verwaltungskontakte anlegen und entfernen; gemeinsame Ausführung der Formular-Aktionen (``action``)."""

    organization: Any

    def _aktion_ausfuehren(self, request, handlers) -> None:
        handler = handlers.get(request.POST.get("action"))
        if handler is not None:
            try:
                handler(request)
            except ServiceError as exc:
                flash_error(request, exc)

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


class CouncilPartyListView(_KontakteMixin, NeueVorlageMixin, WorkViewMixin, TemplateView):
    """Ratsfraktionen, Koalition und (im bisherigen Erscheinungsbild) Verwaltungskontakte verwalten."""

    template_name = "work/organization/parties.html"
    neue_vorlage = "work/organization/neu/ratsfraktionen.html"
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
        self._aktion_ausfuehren(
            request,
            {
                "update_coalition": self._update_coalition,
                "add_admin_contact": self._add_admin_contact,
                "delete_admin_contact": self._delete_admin_contact,
                "add_party": self._add_party,
                "update_party": self._update_party,
                "delete_party": self._delete_party,
            },
        )
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
            coalition_order=int_param(request.POST.get("coalition_order"), 0, minimum=-(10**6), maximum=10**6),
            is_active=request.POST.get("is_active") == "on",
        )

    def _update_coalition(self, request):
        services.update_coalition_name(self.organization, request.POST.get("coalition_name", "").strip())
        messages.success(request, "Koalitionsname gespeichert.")

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


class AdministrationContactListView(_KontakteMixin, NeueVorlageMixin, WorkViewMixin, TemplateView):
    """
    Einstellungen › Rat und Verwaltung › Verwaltungskontakte (Issue #1230): Empfänger von Anträgen, die ohne mandari
    Session per E-Mail an die Verwaltung gehen. Im bisherigen Erscheinungsbild stehen die Kontakte auf der Seite der
    Ratsfraktionen; die Adresse leitet dann dorthin weiter.
    """

    template_name = "work/organization/parties.html"
    neue_vorlage = "work/organization/neu/verwaltungskontakte.html"
    permission_required = "organization.edit"

    def get(self, request, *args, **kwargs):
        if not self.neue_ansicht():
            ziel = reverse("work:council_parties", kwargs={"org_slug": self.organization.slug})
            abfrage = request.GET.urlencode()
            return redirect(f"{ziel}?{abfrage}" if abfrage else ziel)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["admin_contacts"] = selectors.admin_contacts(self.organization)
        return context

    def post(self, request, *args, **kwargs):
        self._aktion_ausfuehren(
            request,
            {"add_admin_contact": self._add_admin_contact, "delete_admin_contact": self._delete_admin_contact},
        )
        return redirect("work:organization_admin_contacts", org_slug=self.organization.slug)

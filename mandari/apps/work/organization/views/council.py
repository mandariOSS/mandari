# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization settings views for the Work module.
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

logger = logging.getLogger(__name__)


# =============================================================================
# COUNCIL PARTY MANAGEMENT
# =============================================================================


class CouncilPartyListView(WorkViewMixin, TemplateView):
    """Ratsfraktionen, Koalition und Verwaltungskontakte verwalten."""

    template_name = "work/organization/parties.html"
    permission_required = "organization.edit"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "parties"

        from apps.common.permissions import PermissionChecker

        checker = PermissionChecker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")

        from apps.tenants.models import AdministrationContact, CouncilParty

        parties = CouncilParty.objects.filter(organization=self.organization).order_by("coalition_order", "name")
        context["parties"] = parties
        context["coalition_parties"] = parties.filter(is_coalition_member=True)
        context["other_parties"] = parties.filter(is_coalition_member=False)

        # Verwaltungskontakte
        context["admin_contacts"] = AdministrationContact.objects.filter(organization=self.organization)

        # Koalitionsname
        context["coalition_name"] = self.organization.coalition_name

        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")

        if action == "update_coalition":
            self.organization.coalition_name = request.POST.get("coalition_name", "").strip()
            self.organization.save(update_fields=["coalition_name"])
            messages.success(request, "Koalitionsname gespeichert.")

        elif action == "add_admin_contact":
            self._handle_add_admin_contact(request)

        elif action == "delete_admin_contact":
            self._handle_delete_admin_contact(request)

        elif action == "add_party":
            self._handle_add_party(request)

        elif action == "update_party":
            self._handle_update_party(request)

        elif action == "delete_party":
            self._handle_delete_party(request)

        return redirect("work:council_parties", org_slug=self.organization.slug)

    def _handle_add_admin_contact(self, request):
        from apps.tenants.models import AdministrationContact

        label = request.POST.get("contact_label", "").strip()
        email = request.POST.get("contact_email", "").strip()
        if label and email:
            AdministrationContact.objects.create(
                organization=self.organization,
                label=label,
                email=email,
            )
            messages.success(request, f"Kontakt '{label}' hinzugefügt.")
        else:
            messages.error(request, "Bezeichnung und E-Mail sind erforderlich.")

    def _handle_delete_admin_contact(self, request):
        from apps.tenants.models import AdministrationContact

        contact_id = request.POST.get("contact_id")
        deleted, _ = AdministrationContact.objects.filter(
            id=contact_id,
            organization=self.organization,
        ).delete()
        if deleted:
            messages.success(request, "Kontakt entfernt.")

    def _handle_add_party(self, request):
        from apps.tenants.models import CouncilParty

        name = request.POST.get("name", "").strip()
        short_name = request.POST.get("short_name", "").strip()
        if not name or not short_name:
            messages.error(request, "Name und Kurzname sind erforderlich.")
            return

        if CouncilParty.objects.filter(organization=self.organization, short_name=short_name).exists():
            messages.error(request, f"Kurzname '{short_name}' existiert bereits.")
            return

        CouncilParty.objects.create(
            organization=self.organization,
            name=name,
            short_name=short_name,
            email=request.POST.get("email", "").strip(),
            contact_name=request.POST.get("contact_name", "").strip(),
            contact_phone=request.POST.get("contact_phone", "").strip(),
            color=request.POST.get("color", "#6b7280").strip(),
            is_coalition_member=request.POST.get("is_coalition_member") == "on",
            coalition_order=int(request.POST.get("coalition_order", 0) or 0),
        )
        messages.success(request, f"Fraktion '{name}' hinzugefügt.")

    def _handle_update_party(self, request):
        from apps.tenants.models import CouncilParty

        party_id = request.POST.get("party_id")
        party = CouncilParty.objects.filter(id=party_id, organization=self.organization).first()
        if not party:
            return

        name = request.POST.get("name", "").strip()
        short_name = request.POST.get("short_name", "").strip()
        if not name or not short_name:
            messages.error(request, "Name und Kurzname sind erforderlich.")
            return

        if (
            CouncilParty.objects.filter(
                organization=self.organization,
                short_name=short_name,
            )
            .exclude(id=party_id)
            .exists()
        ):
            messages.error(request, f"Kurzname '{short_name}' existiert bereits.")
            return

        party.name = name
        party.short_name = short_name
        party.email = request.POST.get("email", "").strip()
        party.contact_name = request.POST.get("contact_name", "").strip()
        party.contact_phone = request.POST.get("contact_phone", "").strip()
        party.color = request.POST.get("color", "#6b7280").strip()
        party.is_coalition_member = request.POST.get("is_coalition_member") == "on"
        party.coalition_order = int(request.POST.get("coalition_order", 0) or 0)
        party.is_active = request.POST.get("is_active") == "on"
        party.save()
        messages.success(request, f"Fraktion '{name}' aktualisiert.")

    def _handle_delete_party(self, request):
        from apps.tenants.models import CouncilParty

        party_id = request.POST.get("party_id")
        party = CouncilParty.objects.filter(id=party_id, organization=self.organization).first()
        if party:
            name = party.name
            party.delete()
            messages.success(request, f"Fraktion '{name}' gelöscht.")

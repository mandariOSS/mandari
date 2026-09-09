# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teamverzeichnis, Teamprofil und die allgemeinen Organisationseinstellungen
(Stammdaten, Fraktionssitzungen, Dokumente).
"""

from django.conf import settings as django_settings
from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.faction.models import FactionMeetingSchedule

from .. import selectors, services
from ..services import ServiceError
from ._helpers import flash_error


class TeamDirectoryView(WorkViewMixin, TemplateView):
    """Internal team directory — visible to all active members."""

    template_name = "work/team/directory.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "team"

        search_query = self.request.GET.get("q", "").strip()
        members = selectors.team_members(self.organization, search_query)
        absence_map = selectors.current_absences_by_member(self.organization, timezone.now().date())

        member_list = list(members)
        for member in member_list:
            selectors.attach(member, current_absence=absence_map.get(member.id))

        context["members"] = member_list
        context["member_count"] = len(member_list) if search_query else members.count()
        context["search_query"] = search_query
        context["is_owner_user"] = self.organization.owner
        return context


class TeamMemberProfileView(WorkViewMixin, TemplateView):
    """Read-only member profile in team directory."""

    template_name = "work/team/profile.html"
    permission_required = "dashboard.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "team"

        member = selectors.get_team_member_or_404(self.organization, kwargs.get("member_id"))
        context["member"] = member
        context["is_self"] = member.user == self.request.user
        context["is_owner"] = self.organization.owner == member.user
        context["roles"] = member.roles.all()
        context["committees"] = member.oparl_committees.all()
        context["joined_at"] = member.joined_at
        context["current_absence"] = selectors.current_absence_for(self.organization, member, timezone.now().date())
        # Sichtbarkeit / Kontakt aus User.settings
        context.update(selectors.profile_settings(member.user))
        return context


class OrganizationSettingsView(WorkViewMixin, TemplateView):
    """Organization settings page."""

    template_name = "work/organization/settings.html"
    permission_required = "organization.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "general"

        checker = selectors.permission_checker(self.membership)
        context["can_manage_faction"] = checker.has_permission("faction.manage")
        context["can_edit"] = checker.has_permission("organization.edit")

        # Heimat-Kommune + Partei (Pflicht-Zuordnungen, prominent angezeigt)
        context["home_body"] = self.organization.body
        context["primary_party"] = self.organization.party_group
        # Weitere Kommunen (read-only: Änderungen nur über Support/Admin)
        context["linked_bodies"] = selectors.linked_bodies(self.organization)
        # Parteien (durch Org-Admins editierbar)
        context["org_parties"] = selectors.org_parties(self.organization)
        context["available_parties"] = selectors.available_parties()
        context["org_party_ids"] = [str(p.id) for p in context["org_parties"]]
        return context

    def post(self, request, *args, **kwargs):
        """Handle organization settings updates."""
        if not selectors.permission_checker(self.membership).has_permission("organization.edit"):
            messages.error(request, "Keine Berechtigung zum Bearbeiten.")
            return redirect("work:organization", org_slug=self.organization.slug)

        handler = {
            "update_general": self._update_general,
            "update_contact": self._update_contact,
            "update_parties": self._update_parties,
        }.get(request.POST.get("action"))
        if handler is not None:
            try:
                handler(request)
            except ServiceError as exc:
                flash_error(request, exc)
        return redirect("work:organization", org_slug=self.organization.slug)

    def _update_general(self, request):
        services.update_general_settings(
            self.organization,
            name=request.POST.get("name", "").strip(),
            description=request.POST.get("description", "").strip(),
            primary_color=request.POST.get("primary_color", "").strip(),
            logo=request.FILES.get("logo"),
            remove_logo=request.POST.get("remove_logo") == "1",
        )
        messages.success(request, "Organisationseinstellungen gespeichert.")

    def _update_contact(self, request):
        services.update_contact_settings(
            self.organization,
            contact_email=request.POST.get("contact_email", "").strip(),
            contact_phone=request.POST.get("contact_phone", "").strip(),
            website=request.POST.get("website", "").strip(),
            address=request.POST.get("address", "").strip(),
        )
        messages.success(request, "Kontaktdaten gespeichert.")

    def _update_parties(self, request):
        services.update_parties(
            self.organization, request.POST.getlist("parties"), request.POST.get("new_party", "").strip()
        )
        messages.success(request, "Parteizugehörigkeit gespeichert.")


class OrganizationFactionSettingsView(WorkViewMixin, TemplateView):
    """Faction meeting settings tab in organization settings."""

    template_name = "work/organization/faction_settings.html"
    permission_required = "faction.manage"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "faction"
        context["can_manage_faction"] = True  # This view requires faction.manage
        # Veröffentlichungs-Opt-in darf nur mit protocols.publish geändert werden
        context["can_publish_protocols"] = self.membership.has_permission("protocols.publish")
        context["faction_settings"] = services.faction_settings_with_defaults(self.organization)

        # Available placeholders for reference
        context["placeholders"] = [
            ("{datum_letzte_sitzung}", "Datum der letzten Sitzung (z.B. 15.01.2026)"),
            ("{titel_letzte_sitzung}", "Titel der letzten Sitzung"),
            ("{nr_letzte_sitzung}", "Nummer der letzten Sitzung"),
            ("{datum}", "Datum der aktuellen Sitzung"),
            ("{titel}", "Titel der aktuellen Sitzung"),
            ("{nr}", "Nummer der aktuellen Sitzung"),
        ]

        # Sitzungsreihen + Ausfallregeln (Issue #61)
        context["schedules"] = selectors.faction_schedules(self.organization)
        context["weekday_choices"] = FactionMeetingSchedule.WEEKDAY_CHOICES
        context["recurrence_choices"] = FactionMeetingSchedule.RECURRENCE_CHOICES
        # Gremien-Auswahl aus den OParl-Organizations der verknüpften Kommune(n)
        context["ris_organizations"] = selectors.all_ris_organizations(self.organization)
        context["schedule_horizon_days"] = getattr(django_settings, "FACTION_SCHEDULE_HORIZON_DAYS", 90)
        return context

    def post(self, request, *args, **kwargs):
        # Sitzungsreihen + Ausfallregeln (Issue #61) — eigene Formularaktionen
        section = request.POST.get("section", "")
        if section:
            handler = {
                "add_schedule": self._add_schedule,
                "toggle_schedule": self._toggle_schedule,
                "delete_schedule": self._delete_schedule,
                "add_exception": self._add_exception,
                "delete_exception": self._delete_exception,
                "add_rule": self._add_rule,
                "delete_rule": self._delete_rule,
            }.get(section)
            if handler is None:
                messages.error(request, "Ungültige Aktion.")
            else:
                try:
                    handler(request)
                except ServiceError as exc:
                    flash_error(request, exc)
            return redirect("work:organization_faction_settings", org_slug=self.organization.slug)

        services.save_faction_settings(self.organization, self.membership, request.POST)
        messages.success(request, "Einstellungen gespeichert.")
        return redirect("work:organization_faction_settings", org_slug=self.organization.slug)

    # -- Sitzungsreihen + Ausfallregeln (Issue #61) -----------------------

    def _add_schedule(self, request):
        schedule = services.add_schedule(
            self.organization,
            services.ScheduleInput(
                name=request.POST.get("name", "").strip(),
                time=request.POST.get("time", "").strip(),
                weekday_raw=request.POST.get("weekday", "0"),
                duration_raw=request.POST.get("duration_minutes", "120"),
                recurrence=request.POST.get("recurrence", "weekly"),
                default_location=request.POST.get("default_location", "").strip(),
                default_video_link=request.POST.get("default_video_link", "").strip(),
            ),
        )
        messages.success(request, f"Sitzungsreihe '{schedule.name}' angelegt. Termine werden automatisch erzeugt.")

    def _toggle_schedule(self, request):
        schedule = services.toggle_schedule(self.organization, request.POST.get("schedule_id"))
        state = "aktiviert" if schedule.is_active else "pausiert"
        messages.success(request, f"Sitzungsreihe '{schedule.name}' {state}.")

    def _delete_schedule(self, request):
        name = services.delete_schedule(self.organization, request.POST.get("schedule_id"))
        messages.success(request, f"Sitzungsreihe '{name}' gelöscht. Bereits erzeugte Sitzungen bleiben bestehen.")

    def _add_exception(self, request):
        services.add_schedule_exception(
            self.organization,
            request.POST.get("schedule_id"),
            original_date=request.POST.get("original_date", "").strip(),
            end_date=request.POST.get("end_date", "").strip(),
            reason=request.POST.get("reason", "").strip(),
        )
        messages.success(request, "Ausnahmezeitraum gespeichert — Termine im Zeitraum entfallen ersatzlos.")

    def _delete_exception(self, request):
        services.delete_schedule_exception(self.organization, request.POST.get("exception_id"))
        messages.success(request, "Ausnahme entfernt.")

    def _add_rule(self, request):
        name = services.add_suspension_rule(
            self.organization, request.POST.get("schedule_id"), request.POST.get("ris_organization_id")
        )
        messages.success(
            request,
            f"Ausfallregel gespeichert: Nach einer Sitzung von '{name}' entfällt die nächste Fraktionssitzung.",
        )

    def _delete_rule(self, request):
        services.delete_suspension_rule(self.organization, request.POST.get("rule_id"))
        messages.success(request, "Ausfallregel entfernt.")


class OrganizationDocumentsView(WorkViewMixin, TemplateView):
    """Document settings tab for organization (Anträge & Vorgänge)."""

    template_name = "work/organization/documents.html"
    permission_required = "organization.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "documents"
        context["can_manage_faction"] = selectors.permission_checker(self.membership).has_permission("faction.manage")
        context.update(selectors.document_settings_context(self.organization))
        return context

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Teamverzeichnis, Teamprofil und die allgemeinen Organisationseinstellungen
(Stammdaten, Fraktionssitzungen, Dokumente).
"""

from django.conf import settings as django_settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.faction import deletion
from apps.work.faction.models import FactionMeetingSchedule
from apps.work.faction.visibility import can_view_internal
from apps.work.rahmen import NeueVorlageMixin

from .. import einstellungen, fraktion_einstellungen, selectors, services
from ..services import ServiceError
from ._helpers import flash_error


class TeamDirectoryView(NeueVorlageMixin, WorkViewMixin, TemplateView):
    """Internal team directory — visible to all active members."""

    template_name = "work/team/directory.html"
    neue_vorlage = "work/team/neu/verzeichnis.html"
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


class TeamMemberProfileView(NeueVorlageMixin, WorkViewMixin, TemplateView):
    """Read-only member profile in team directory."""

    template_name = "work/team/profile.html"
    neue_vorlage = "work/team/neu/profil.html"
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


class OrganizationSettingsView(NeueVorlageMixin, WorkViewMixin, TemplateView):
    """Organization settings page."""

    template_name = "work/organization/settings.html"
    neue_vorlage = "work/organization/neu/allgemein.html"
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
            "update_security": self._update_security,
        }.get(request.POST.get("action"))
        if handler is not None:
            try:
                handler(request)
            except ServiceError as exc:
                flash_error(request, exc)
        # Reiter „Sicherheit“ im neuen Erscheinungsbild bleibt nach dem Speichern offen (Issue #951)
        return redirect(einstellungen.ziel_nach_speichern(request, self.organization))

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

    def _update_security(self, request):
        # Ungesetzte Checkboxen fehlen im POST ganz; die Anwesenheit des Feldes genügt
        services.update_two_factor_requirement(self.organization, required="require_2fa" in request.POST)
        messages.success(request, "Anmeldesicherheit gespeichert.")


#: Hinweis nach Änderungen an der Standard-Tagesordnung (Issue #872): sie gilt nur für neu angelegte Sitzungen
_STANDARD_AGENDA_SCOPE = (
    "Bereits angelegte Sitzungen, auch vorausgeplante Termine der Sitzungsreihen, bleiben unverändert; "
    "dort ergänzen Sie Punkte in der Sitzung selbst."
)


class OrganizationFactionSettingsView(NeueVorlageMixin, WorkViewMixin, TemplateView):
    """
    Einstellungen › Fraktionssitzungen. Ändern nur mit ``faction.manage``; im neuen Erscheinungsbild sehen Mitglieder
    mit ``faction.view_public`` die Seite zur Information (Nur-Lesen, Issue #1071, Muster einstellungen_mitglied).
    Die bisherige Vorlage (``?ansicht=bisher``) bleibt wie bisher der Verwaltung vorbehalten.

    Die neue Seite speichert jede Änderung sofort (htmx, ``data-autosave``): ``section=save_settings`` mit
    ``felder=…`` und die Automatik je Reihe (``section=update_schedule_automation``) antworten auf htmx-Anfragen mit
    204 bzw. 400 und dem Grund als Text. Ohne JavaScript speichert das Formular wie bisher als Ganzes.
    """

    template_name = "work/organization/faction_settings.html"
    neue_vorlage = "work/organization/neu/fraktion.html"
    permission_required = ["faction.manage", "faction.view_public"]
    permission_require_all = False

    #: Abschnitte, die die neue Seite per htmx sofort speichert
    SOFORT = ("save_settings", "update_schedule_automation")

    def darf_verwalten(self) -> bool:
        return bool(self.membership.has_permission("faction.manage"))

    def get_context_data(self, **kwargs):
        darf_verwalten = self.darf_verwalten()
        # Nur-Lesen gibt es nur in der neuen Vorlage; die bisherige zeigt Formulare
        if not darf_verwalten and not self.neue_ansicht():
            raise PermissionDenied("Fehlende Berechtigung")
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "faction"
        context["can_manage_faction"] = darf_verwalten
        context["nur_lesen"] = not darf_verwalten
        # Veröffentlichungs-Opt-in darf nur mit protocols.publish geändert werden
        context["can_publish_protocols"] = darf_verwalten and self.membership.has_permission("protocols.publish")
        context["can_manage_roles"] = self.membership.has_permission("organization.manage_roles")
        context["faction_settings"] = services.faction_settings_with_defaults(self.organization)
        context["felder"] = {key: fraktion_einstellungen.felder(key) for key in fraktion_einstellungen.ABSCHNITT_FELDER}
        context["top_rechte"] = fraktion_einstellungen.top_rechte(self.organization)

        # Available placeholders for reference
        context["placeholders"] = [
            ("{datum_letzte_sitzung}", "Datum der letzten Sitzung (z.B. 15.01.2026)"),
            ("{titel_letzte_sitzung}", "Titel der letzten Sitzung"),
            ("{nr_letzte_sitzung}", "Nummer der letzten Sitzung"),
            ("{datum}", "Datum der aktuellen Sitzung"),
            ("{titel}", "Titel der aktuellen Sitzung"),
            ("{nr}", "Nummer der aktuellen Sitzung"),
        ]

        # Sitzungsreihen + Ausfallregeln (Issue #61); Löschen nur mit faction.delete und Folgen im Dialog (Issue #897)
        schedules = list(selectors.faction_schedules(self.organization))
        context["can_delete_schedules"] = darf_verwalten and deletion.can_delete(self.membership)
        if context["can_delete_schedules"]:
            schedules = deletion.with_deletion_details(schedules)
        for schedule in schedules:
            schedule.angaben = fraktion_einstellungen.reihe_angaben(schedule)
        context["schedules"] = schedules
        context["weekday_choices"] = FactionMeetingSchedule.WEEKDAY_CHOICES
        context["recurrence_choices"] = FactionMeetingSchedule.RECURRENCE_CHOICES
        # Gremien-Auswahl aus den OParl-Organizations der verknüpften Kommune(n)
        context["ris_organizations"] = selectors.all_ris_organizations(self.organization) if darf_verwalten else []
        context["schedule_horizon_days"] = getattr(django_settings, "FACTION_SCHEDULE_HORIZON_DAYS", 90)

        # Standard-Tagesordnung (Issue #872): NÖ-Punkte nur für Vereidigte (NÖ strikt, Issue #64)
        context.update(
            selectors.standard_agenda_context(
                self.organization,
                may_view_internal=can_view_internal(self.membership),
                approval_item=bool(context["faction_settings"]["auto_create_approval_item"]),
            )
        )
        context["standard_agenda_suggestion"] = services.STANDARD_AGENDA_SUGGESTION
        return context

    def post(self, request, *args, **kwargs):
        # Mitglieder sehen die Seite nur zur Information
        if not self.darf_verwalten():
            raise PermissionDenied("Fehlende Berechtigung")
        section = request.POST.get("section", "")
        if section in self.SOFORT and self.is_htmx:
            return self._sofort_speichern(request, section)
        # Sitzungsreihen + Ausfallregeln (Issue #61) — eigene Formularaktionen
        if section:
            handler = {
                "save_settings": self._save_settings,
                "add_schedule": self._add_schedule,
                "update_schedule_automation": self._update_schedule_automation,
                "toggle_schedule": self._toggle_schedule,
                "delete_schedule": self._delete_schedule,
                "add_exception": self._add_exception,
                "delete_exception": self._delete_exception,
                "add_rule": self._add_rule,
                "delete_rule": self._delete_rule,
                "add_standard_item": self._add_standard_item,
                "add_standard_suggestion": self._add_standard_suggestion,
                "move_standard_item": self._move_standard_item,
                "delete_standard_item": self._delete_standard_item,
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

    # -- Sofort speichern (Issue #1071) ------------------------------------

    def _sofort_speichern(self, request, section: str) -> HttpResponse:
        """Ein Abschnitt der neuen Seite speichert per htmx: 204 ohne Meldung, sonst 400 mit dem Grund als Text."""
        try:
            if section == "save_settings":
                services.save_faction_settings(
                    self.organization,
                    self.membership,
                    request.POST,
                    fields=fraktion_einstellungen.felder_aus(request.POST),
                )
            else:
                services.save_schedule_automation(self.organization, request.POST)
        except ServiceError as exc:
            return HttpResponse(str(exc), status=400, content_type="text/plain; charset=utf-8")
        return HttpResponse(status=204)

    def _save_settings(self, request):
        services.save_faction_settings(
            self.organization, self.membership, request.POST, fields=fraktion_einstellungen.felder_aus(request.POST)
        )
        messages.success(request, "Einstellungen gespeichert.")

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
                automation=services.schedule_automation_from_form(request.POST),
            ),
        )
        messages.success(request, f"Sitzungsreihe '{schedule.name}' angelegt. Termine werden automatisch erzeugt.")

    def _update_schedule_automation(self, request):
        schedule, updated = services.save_schedule_automation(self.organization, request.POST)
        message = f"Automatik der Sitzungsreihe '{schedule.name}' gespeichert."
        if updated:
            message += f" Zu- und Absagen bei {updated} kommenden Terminen angepasst."
        messages.success(request, message)

    def _toggle_schedule(self, request):
        schedule = services.toggle_schedule(self.organization, request.POST.get("schedule_id"))
        state = "aktiviert" if schedule.is_active else "pausiert"
        messages.success(request, f"Sitzungsreihe '{schedule.name}' {state}.")

    def _delete_schedule(self, request):
        name = services.delete_schedule(self.organization, self.membership, request.POST)
        messages.success(request, f"Sitzungsreihe „{name}“ gelöscht. Bereits erzeugte Sitzungen bleiben bestehen.")

    def _add_exception(self, request):
        cancelled = services.add_schedule_exception(
            self.organization,
            request.POST.get("schedule_id"),
            original_date=request.POST.get("original_date", "").strip(),
            end_date=request.POST.get("end_date", "").strip(),
            reason=request.POST.get("reason", "").strip(),
        )
        message = "Ausnahmezeitraum gespeichert — Termine im Zeitraum entfallen ersatzlos."
        if cancelled:
            message += f" {cancelled} schon angelegte Termine entfallen."
        messages.success(request, message)

    def _delete_exception(self, request):
        services.delete_schedule_exception(self.organization, request.POST.get("exception_id"))
        messages.success(request, "Ausnahme entfernt.")

    def _add_rule(self, request):
        name, cancelled = services.add_suspension_rule(
            self.organization, request.POST.get("schedule_id"), request.POST.get("ris_organization_id")
        )
        message = f"Ausfallregel gespeichert: Nach einer Sitzung von '{name}' entfällt die nächste Fraktionssitzung."
        if cancelled:
            message += f" {cancelled} schon angelegte Termine entfallen."
        messages.success(request, message)

    def _delete_rule(self, request):
        services.delete_suspension_rule(self.organization, request.POST.get("rule_id"))
        messages.success(request, "Ausfallregel entfernt.")

    # -- Standard-Tagesordnung (Issue #872) --------------------------------

    def _add_standard_item(self, request):
        title = services.add_standard_agenda_item(
            self.organization, request.POST, may_view_internal=can_view_internal(self.membership)
        )
        messages.success(
            request,
            f"„{title}“ steht ab sofort auf der Tagesordnung jeder neu angelegten Sitzung. {_STANDARD_AGENDA_SCOPE}",
        )

    def _add_standard_suggestion(self, request):
        count = services.add_standard_agenda_suggestion(self.organization)
        messages.success(request, f"{count} Standardpunkte übernommen. {_STANDARD_AGENDA_SCOPE}")

    def _move_standard_item(self, request):
        services.move_standard_agenda_item(
            self.organization, request.POST, may_view_internal=can_view_internal(self.membership)
        )

    def _delete_standard_item(self, request):
        title = services.delete_standard_agenda_item(
            self.organization, request.POST, may_view_internal=can_view_internal(self.membership)
        )
        messages.success(request, f"„{title}“ entfernt. Bereits angelegte Sitzungen behalten ihre TOPs.")


class OrganizationDocumentsView(NeueVorlageMixin, WorkViewMixin, TemplateView):
    """Document settings tab for organization (Anträge & Vorgänge)."""

    template_name = "work/organization/documents.html"
    neue_vorlage = "work/organization/neu/dokumente.html"
    permission_required = "organization.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "organization"
        context["active_tab"] = "documents"
        context["can_manage_faction"] = selectors.permission_checker(self.membership).has_permission("faction.manage")
        context.update(selectors.document_settings_context(self.organization))
        return context

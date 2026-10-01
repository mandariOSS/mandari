# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wahlperioden-Verwaltung für das Session RIS (Issue #39).

- TermListView: Perioden anlegen/bearbeiten/löschen ohne Django-Admin
  (Einstellungen), inkl. Periodenwechsel-Assistent.
- TermChangeView: Periodenwechsel — neue Periode anlegen, laufende
  Gremienbesetzungen entweder in die neue Periode übernehmen oder zum
  Stichtag beenden (Neubesetzung von Hand).
- ArchiveView: Archiv-Ansicht vergangener Perioden mit Kennzahlen und
  Deep-Links in die gefilterten Listen (Sitzungen, Vorlagen, Gremien).

Alle Mutationen laufen über die Audit-Signale (signals.py) bzw. werden
zusätzlich als Audit-Ereignis dokumentiert.
"""

from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from apps.common.formatting import parse_iso_date
from apps.common.params import uuid_param

from .. import audit
from ..models import (
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganizationMembership,
    SessionPaper,
)
from ..permissions import SessionViewMixin
from ..services import membership_service

# =============================================================================
# HELPERS
# =============================================================================


def _parse_number(value):
    """Nummer der Wahlperiode (Platzhalter {wp} in Nummernkreisen, Issue #150)."""
    try:
        zahl = int(value)
    except (TypeError, ValueError):
        return None
    return zahl if 0 < zahl < 100 else None


def term_date_filter(term, field="date"):
    """Q-Filter: Datumsfeld liegt im Zeitraum der Periode (für Vorlagen u. Ä.)."""
    q = Q()
    if term.start_date:
        q &= Q(**{f"{field}__gte": term.start_date})
    if term.end_date:
        q &= Q(**{f"{field}__lte": term.end_date})
    return q


# =============================================================================
# VERWALTUNG (Einstellungen)
# =============================================================================


class TermListView(SessionViewMixin, TemplateView):
    """Wahlperioden verwalten (Liste + Formulare, Issue #39)."""

    template_name = "session/settings/terms.html"
    permission_required = "manage_settings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        terms = list(
            SessionLegislativeTerm.objects.filter(tenant=self.session_tenant).annotate(
                meeting_count=Count("meetings", distinct=True),
                membership_count=Count("memberships", distinct=True),
            )
        )
        current = SessionLegislativeTerm.current_for(self.session_tenant)
        context["terms"] = terms
        context["current_term"] = current
        today = timezone.localdate()
        context["active_membership_count"] = (
            SessionOrganizationMembership.objects.filter(organization__tenant=self.session_tenant)
            .filter(membership_service.active_q(today))
            .count()
        )
        context["today"] = today
        return context


class TermSaveView(SessionViewMixin, View):
    """Wahlperiode anlegen oder bearbeiten (Name, Zeitraum)."""

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Bitte einen Namen für die Wahlperiode angeben.")
            return redirect("session:terms", tenant_slug=tenant_slug)

        start_date = parse_iso_date(request.POST.get("start_date"))
        end_date = parse_iso_date(request.POST.get("end_date"))
        if start_date and end_date and start_date > end_date:
            messages.error(request, "Der Beginn der Wahlperiode liegt nach ihrem Ende.")
            return redirect("session:terms", tenant_slug=tenant_slug)

        term_id = request.POST.get("term_id")
        term = None
        if term_id:
            # Ungültige Kennung: Meldung statt Serverfehler
            term = SessionLegislativeTerm.objects.filter(pk=uuid_param(term_id), tenant=self.session_tenant).first()
            if term is None:
                messages.error(request, "Die Wahlperiode wurde nicht gefunden.")
                return redirect("session:terms", tenant_slug=tenant_slug)

        # Perioden dürfen sich nicht überschneiden, sonst ist „aktuelle Periode“ bzw. die Periode eines
        # Datums (Sitzungen, Besetzungen, Platzhalter {wp}) nicht eindeutig
        conflict = membership_service.term_overlap(
            self.session_tenant, start_date, end_date, exclude_pk=term.pk if term else None
        )
        if conflict is not None and (start_date or end_date):
            messages.error(request, f"Der Zeitraum überschneidet sich mit der Wahlperiode „{conflict.name}“.")
            return redirect("session:terms", tenant_slug=tenant_slug)

        if term is not None:
            term.name = name
            term.start_date = start_date
            term.end_date = end_date
            term.number = _parse_number(request.POST.get("number"))
            term.save()
            messages.success(request, f"Wahlperiode „{term.name}“ wurde aktualisiert.")
        else:
            term = SessionLegislativeTerm.objects.create(
                number=_parse_number(request.POST.get("number")),
                tenant=self.session_tenant,
                name=name,
                start_date=start_date,
                end_date=end_date,
            )
            messages.success(request, f"Wahlperiode „{term.name}“ wurde angelegt.")
        return redirect("session:terms", tenant_slug=tenant_slug)


class TermDeleteView(SessionViewMixin, View):
    """Wahlperiode löschen (nur, wenn keine Daten zugeordnet sind)."""

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug, term_id):
        term = get_object_or_404(SessionLegislativeTerm, pk=term_id, tenant=self.session_tenant)
        if term.meetings.exists() or term.memberships.exists():
            messages.error(
                request,
                f"Wahlperiode „{term.name}“ kann nicht gelöscht werden — "
                "es sind Sitzungen oder Besetzungen zugeordnet.",
            )
            return redirect("session:terms", tenant_slug=tenant_slug)
        name = term.name
        term.delete()
        messages.success(request, f"Wahlperiode „{name}“ wurde gelöscht.")
        return redirect("session:terms", tenant_slug=tenant_slug)


class TermChangeView(SessionViewMixin, View):
    """
    Periodenwechsel-Assistent (Issue #39).

    Legt die neue Wahlperiode an und behandelt die laufenden Besetzungen:

    - Modus "carry": Laufende Besetzungen werden zum Stichtag beendet und
      mit gleicher Funktion/gleichem Stimmrecht in der neuen Periode neu
      angelegt (Übernahme).
    - Modus "fresh": Laufende Besetzungen werden nur beendet — die Gremien
      werden anschließend von Hand neu besetzt.

    Alt-Daten bleiben unter der alten Periode auffindbar (Archiv).
    """

    permission_required = "manage_settings"
    http_method_names = ["post"]

    def post(self, request, tenant_slug):
        name = (request.POST.get("name") or "").strip()
        start_date = parse_iso_date(request.POST.get("start_date"))
        if not name or start_date is None:
            messages.error(request, "Bitte Name und Beginn der neuen Wahlperiode angeben.")
            return redirect("session:terms", tenant_slug=tenant_slug)
        end_date = parse_iso_date(request.POST.get("end_date"))
        if end_date and start_date > end_date:
            messages.error(request, "Der Beginn der Wahlperiode liegt nach ihrem Ende.")
            return redirect("session:terms", tenant_slug=tenant_slug)

        mode = request.POST.get("mode", membership_service.MODE_CARRY)
        if mode not in (membership_service.MODE_CARRY, membership_service.MODE_FRESH):
            mode = membership_service.MODE_CARRY

        error = membership_service.term_change_error(self.session_tenant, start_date, end_date)
        if error:
            messages.error(request, error)
            return redirect("session:terms", tenant_slug=tenant_slug)
        # Eine Transaktion: Ein Fehler hinterlässt keine halb gewechselte Periode
        change = membership_service.change_term(
            self.session_tenant,
            name=name,
            number=_parse_number(request.POST.get("number")),
            start_date=start_date,
            end_date=end_date,
            mode=mode,
        )

        old_term, new_term = change.old_term, change.new_term
        previous_day = start_date - timedelta(days=1)
        audit.log_event(
            "update",
            new_term,
            tenant=self.session_tenant,
            user=self.session_user,
            request=request,
            changes={
                "periodenwechsel": {
                    "alte_periode": old_term.name if old_term else None,
                    "neue_periode": new_term.name,
                    "modus": "Besetzungen übernommen" if mode == membership_service.MODE_CARRY else "Neu besetzen",
                    "beendete_besetzungen": change.ended,
                    "uebernommene_besetzungen": change.carried,
                    "bereits_neue_besetzungen": change.already_new,
                }
            },
        )

        if mode == membership_service.MODE_CARRY:
            messages.success(
                request,
                f"Wahlperiode „{new_term.name}“ angelegt — {change.carried} Besetzung(en) übernommen, "
                f"{change.ended} Alt-Besetzung(en) zum {previous_day:%d.%m.%Y} beendet.",
            )
        else:
            messages.success(
                request,
                f"Wahlperiode „{new_term.name}“ angelegt — {change.ended} Besetzung(en) "
                f"zum {previous_day:%d.%m.%Y} beendet. Die Gremien können jetzt neu besetzt werden.",
            )
        return redirect("session:terms", tenant_slug=tenant_slug)


# =============================================================================
# ARCHIV
# =============================================================================


class ArchiveView(SessionViewMixin, TemplateView):
    """
    Archiv-Ansicht vergangener Wahlperioden (Issue #39).

    Zeigt je Periode Kennzahlen (Sitzungen, Vorlagen, Besetzungen) und
    verlinkt in die perioden-gefilterten Listen. Ö/NÖ wird in den
    Ziel-Listen durchgesetzt; die Zählung hier respektiert sie ebenfalls.
    """

    template_name = "session/archive.html"
    permission_required = "view_meetings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_np_meetings = self.has_permission("view_non_public_meetings")
        can_np_papers = self.has_permission("view_non_public_papers")

        current = SessionLegislativeTerm.current_for(self.session_tenant)
        rows = []
        for term in SessionLegislativeTerm.objects.filter(tenant=self.session_tenant):
            meetings = SessionMeeting.objects.filter(tenant=self.session_tenant, legislative_term=term)
            if not can_np_meetings:
                meetings = meetings.filter(is_public=True)
            papers = SessionPaper.objects.filter(tenant=self.session_tenant).filter(term_date_filter(term))
            if not can_np_papers:
                papers = papers.filter(is_public=True)
            memberships = SessionOrganizationMembership.objects.filter(
                organization__tenant=self.session_tenant, legislative_term=term
            )
            rows.append(
                {
                    "term": term,
                    "is_current": current is not None and term.pk == current.pk,
                    "meeting_count": meetings.count(),
                    "paper_count": papers.count(),
                    "membership_count": memberships.count(),
                    "organization_count": memberships.values("organization_id").distinct().count(),
                }
            )
        context["rows"] = rows
        context["current_term"] = current
        context["can_manage_terms"] = self.has_permission("manage_settings")
        return context

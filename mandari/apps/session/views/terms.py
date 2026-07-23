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

from datetime import date, timedelta

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from ..models import (
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganizationMembership,
    SessionPaper,
)
from ..permissions import SessionViewMixin

# =============================================================================
# HELPERS
# =============================================================================


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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
        context["active_membership_count"] = SessionOrganizationMembership.objects.filter(
            organization__tenant=self.session_tenant, end_date__isnull=True
        ).count()
        context["today"] = timezone.localdate()
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

        start_date = _parse_date(request.POST.get("start_date"))
        end_date = _parse_date(request.POST.get("end_date"))
        if start_date and end_date and start_date > end_date:
            messages.error(request, "Der Beginn der Wahlperiode liegt nach ihrem Ende.")
            return redirect("session:terms", tenant_slug=tenant_slug)

        term_id = request.POST.get("term_id")
        if term_id:
            term = get_object_or_404(SessionLegislativeTerm, pk=term_id, tenant=self.session_tenant)
            term.name = name
            term.start_date = start_date
            term.end_date = end_date
            term.save()
            messages.success(request, f"Wahlperiode „{term.name}“ wurde aktualisiert.")
        else:
            term = SessionLegislativeTerm.objects.create(
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
        from .. import audit

        name = (request.POST.get("name") or "").strip()
        start_date = _parse_date(request.POST.get("start_date"))
        if not name or start_date is None:
            messages.error(request, "Bitte Name und Beginn der neuen Wahlperiode angeben.")
            return redirect("session:terms", tenant_slug=tenant_slug)
        end_date = _parse_date(request.POST.get("end_date"))
        if end_date and start_date > end_date:
            messages.error(request, "Der Beginn der Wahlperiode liegt nach ihrem Ende.")
            return redirect("session:terms", tenant_slug=tenant_slug)

        mode = request.POST.get("mode", "carry")
        if mode not in ("carry", "fresh"):
            mode = "carry"

        old_term = SessionLegislativeTerm.current_for(self.session_tenant)
        previous_day = start_date - timedelta(days=1)

        # Alte Periode sauber abschließen (Enddatum setzen, falls offen)
        if old_term is not None and old_term.end_date is None:
            old_term.end_date = previous_day
            old_term.save(update_fields=["end_date", "updated_at"])

        new_term = SessionLegislativeTerm.objects.create(
            tenant=self.session_tenant,
            name=name,
            start_date=start_date,
            end_date=end_date,
        )

        # Laufende Besetzungen zum Stichtag beenden — Alt-Daten bleiben
        # unter der alten Periode auffindbar
        active_memberships = list(
            SessionOrganizationMembership.objects.filter(
                organization__tenant=self.session_tenant, end_date__isnull=True
            ).select_related("organization", "person")
        )
        carried = 0
        for membership in active_memberships:
            membership.end_date = previous_day
            if membership.legislative_term_id is None and old_term is not None:
                membership.legislative_term = old_term
            membership.save()
            if mode == "carry":
                SessionOrganizationMembership.objects.create(
                    organization=membership.organization,
                    person=membership.person,
                    role=membership.role,
                    has_voting_rights=membership.has_voting_rights,
                    start_date=start_date,
                    legislative_term=new_term,
                )
                carried += 1

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
                    "modus": "Besetzungen übernommen" if mode == "carry" else "Neu besetzen",
                    "beendete_besetzungen": len(active_memberships),
                    "uebernommene_besetzungen": carried,
                }
            },
        )

        if mode == "carry":
            messages.success(
                request,
                f"Wahlperiode „{new_term.name}“ angelegt — {carried} Besetzung(en) übernommen, "
                f"{len(active_memberships)} Alt-Besetzung(en) zum {previous_day:%d.%m.%Y} beendet.",
            )
        else:
            messages.success(
                request,
                f"Wahlperiode „{new_term.name}“ angelegt — {len(active_memberships)} Besetzung(en) "
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

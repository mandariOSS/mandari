# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beratungsfolge für Vorlagen (Issue #34).

Eine Vorlage durchläuft mehrere Gremien (Vorberatung im Fachausschuss ->
Entscheidung im Rat). Die Views verwalten die Kette der Beratungsstationen
(SessionConsultation) aus der Vorlagen-Detailansicht heraus:

- Station anlegen/bearbeiten/löschen/umsortieren (Berechtigung: edit_papers)
- Station terminieren = TOP auf der Zielsitzung anlegen (edit_meetings),
  mit Ö/NÖ- und Nachtrags-Logik der Tagesordnungsverwaltung (Issue #26)
- Weiterleitung an die nächste Station nach vorliegendem Ergebnis

Das Beschlussergebnis je Station schreibt signals.sync_consultation_result
automatisch vom TOP zurück (Issues #31/#32).
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views import View

from ..models import (
    SessionAgendaItem,
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
)
from ..permissions import SessionViewMixin
from ..services import agenda_service

ROLE_VALUES = {value for value, _ in SessionConsultation.ROLE_CHOICES}
RESULT_VALUES = {value for value, _ in SessionConsultation.RESULT_CHOICES}


class ConsultationBaseView(SessionViewMixin, View):
    """Basisklasse: POST-only, Tenant- und Ö/NÖ-gefilterte Objektzugriffe."""

    http_method_names = ["post"]

    def get_paper(self, paper_id):
        qs = SessionPaper.objects.filter(tenant=self.session_tenant)
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(is_public=True)
        return get_object_or_404(qs, pk=paper_id)

    def get_consultation(self, consultation_id):
        qs = SessionConsultation.objects.select_related("paper", "organization", "meeting", "agenda_item").filter(
            paper__tenant=self.session_tenant
        )
        if not self.has_permission("view_non_public_papers"):
            qs = qs.filter(paper__is_public=True)
        return get_object_or_404(qs, pk=consultation_id)

    def redirect_to_paper(self, paper):
        return redirect(
            "session:paper_detail",
            tenant_slug=self.session_tenant.slug,
            paper_id=paper.id,
        )

    def resolve_meeting(self, raw_meeting_id, organization):
        """
        Zielsitzung validieren: eigener Tenant und passendes Gremium.

        Returns:
            (meeting|None, error|None)
        """
        if not raw_meeting_id:
            return None, None
        meeting = SessionMeeting.objects.filter(pk=raw_meeting_id, tenant=self.session_tenant).first()
        if meeting is None:
            return None, "Die gewählte Sitzung wurde nicht gefunden."
        if meeting.organization_id != organization.id:
            return None, "Die gewählte Sitzung gehört nicht zum Gremium dieser Station."
        return meeting, None


class ConsultationCreateView(ConsultationBaseView):
    """Beratungsstation an einer Vorlage anlegen."""

    permission_required = "edit_papers"

    def post(self, request, tenant_slug, paper_id):
        paper = self.get_paper(paper_id)

        organization = SessionOrganization.objects.filter(
            pk=request.POST.get("organization") or None,
            tenant=self.session_tenant,
        ).first()
        if organization is None:
            messages.error(request, "Bitte ein Gremium für die Beratungsstation wählen.")
            return self.redirect_to_paper(paper)

        role = request.POST.get("role", "preliminary")
        if role not in ROLE_VALUES:
            role = "preliminary"

        meeting, error = self.resolve_meeting(request.POST.get("meeting"), organization)
        if error:
            messages.error(request, error)
            return self.redirect_to_paper(paper)

        last_order = paper.consultations.order_by("-order").values_list("order", flat=True).first() or 0
        consultation = SessionConsultation.objects.create(
            paper=paper,
            organization=organization,
            meeting=meeting,
            role=role,
            # Entscheidungs-Stationen sind standardmäßig authoritative (OParl)
            authoritative=bool(request.POST.get("authoritative")) or role == "decision",
            order=last_order + 1,
        )
        messages.success(
            request,
            f"Beratungsstation {consultation.order} ({organization.name}, "
            f"{consultation.get_role_display()}) wurde angelegt.",
        )
        return self.redirect_to_paper(paper)


class ConsultationUpdateView(ConsultationBaseView):
    """Beratungsstation bearbeiten (Rolle, Zielsitzung, authoritative, Ergebnis)."""

    permission_required = "edit_papers"

    def post(self, request, tenant_slug, consultation_id):
        consultation = self.get_consultation(consultation_id)
        paper = consultation.paper

        role = request.POST.get("role", consultation.role)
        if role in ROLE_VALUES:
            consultation.role = role

        raw_meeting = request.POST.get("meeting", "")
        if consultation.agenda_item_id and str(consultation.meeting_id or "") != raw_meeting:
            messages.error(
                request,
                "Die Zielsitzung kann nicht geändert werden, solange die Station "
                "einen Tagesordnungspunkt hat. Bitte zuerst den TOP entfernen.",
            )
            return self.redirect_to_paper(paper)
        meeting, error = self.resolve_meeting(raw_meeting, consultation.organization)
        if error:
            messages.error(request, error)
            return self.redirect_to_paper(paper)
        consultation.meeting = meeting

        consultation.authoritative = bool(request.POST.get("authoritative"))

        # Manuelles Ergebnis nur für Stationen ohne TOP (sonst schreibt der
        # TOP das Ergebnis über das Signal zurück und bleibt führend).
        result = request.POST.get("result", "")
        if result and result in RESULT_VALUES and not consultation.agenda_item_id:
            consultation.result = result

        consultation.save()
        messages.success(request, f"Beratungsstation {consultation.order} wurde aktualisiert.")
        return self.redirect_to_paper(paper)


class ConsultationDeleteView(ConsultationBaseView):
    """Beratungsstation entfernen (ein bereits angelegter TOP bleibt bestehen)."""

    permission_required = "edit_papers"

    def post(self, request, tenant_slug, consultation_id):
        consultation = self.get_consultation(consultation_id)
        paper = consultation.paper
        label = f"Station {consultation.order} ({consultation.organization.name})"
        consultation.delete()
        # Lücken in der Reihenfolge schließen
        for index, station in enumerate(paper.consultations.order_by("order", "created_at"), start=1):
            if station.order != index:
                station.order = index
                station.save(update_fields=["order", "updated_at"])
        messages.success(request, f"{label} wurde aus der Beratungsfolge entfernt.")
        return self.redirect_to_paper(paper)


class ConsultationMoveView(ConsultationBaseView):
    """Beratungsstation in der Kette nach oben/unten verschieben."""

    permission_required = "edit_papers"

    def post(self, request, tenant_slug, consultation_id):
        consultation = self.get_consultation(consultation_id)
        paper = consultation.paper
        direction = request.POST.get("direction")
        stations = list(paper.consultations.order_by("order", "created_at"))
        try:
            idx = [s.id for s in stations].index(consultation.id)
        except ValueError:
            return self.redirect_to_paper(paper)
        target = idx - 1 if direction == "up" else idx + 1
        if direction not in ("up", "down") or target < 0 or target >= len(stations):
            return self.redirect_to_paper(paper)
        other = stations[target]
        consultation.order, other.order = other.order, consultation.order
        consultation.save(update_fields=["order", "updated_at"])
        other.save(update_fields=["order", "updated_at"])
        return self.redirect_to_paper(paper)


def schedule_consultation(view, request, consultation):
    """
    Station terminieren: TOP auf der Zielsitzung anlegen und verknüpfen.

    Nutzt die Nummerierungs-/Ö-NÖ-Logik der Tagesordnungsverwaltung
    (Issue #26): Der TOP übernimmt die Sichtbarkeit der Vorlage, wird bei
    bereits versandter Ladung als Nachtrag gekennzeichnet und die
    Tagesordnung wird Ö/NÖ-getrennt neu nummeriert.
    """
    paper = consultation.paper

    if consultation.agenda_item_id:
        messages.info(request, "Diese Station hat bereits einen Tagesordnungspunkt.")
        return False

    meeting = consultation.meeting
    if meeting is None:
        raw_meeting = request.POST.get("meeting")
        meeting, error = view.resolve_meeting(raw_meeting, consultation.organization)
        if meeting is None:
            messages.error(
                request,
                error or "Bitte zuerst eine Zielsitzung für die Station wählen.",
            )
            return False
        consultation.meeting = meeting

    item = SessionAgendaItem.objects.create(
        meeting=meeting,
        number="?",  # wird durch renumber_agenda gesetzt
        name=f"{paper.reference}: {paper.name}"[:500],
        order=(meeting.agenda_items.count() + 1) * 100,
        is_public=paper.is_public,
        is_supplementary=bool(meeting.invitation_sent_at or meeting.meeting_state == "invitation_sent"),
        paper=paper,
    )
    agenda_service.renumber_agenda(meeting)

    consultation.agenda_item = item
    consultation.save()

    # Vorlage gilt mit der ersten Terminierung als „Terminiert“
    if paper.status == "approved":
        paper.status = "scheduled"
        paper.save()

    item.refresh_from_db(fields=["number"])
    messages.success(
        request,
        f"TOP {item.number} für {consultation.organization.name} "
        f"({meeting.start:%d.%m.%Y}) wurde aus der Beratungsfolge angelegt.",
    )
    return True


class ConsultationScheduleView(ConsultationBaseView):
    """Station terminieren (TOP anlegen) — Tagesordnungs-Mutation."""

    permission_required = "edit_meetings"

    def post(self, request, tenant_slug, consultation_id):
        consultation = self.get_consultation(consultation_id)
        schedule_consultation(self, request, consultation)
        return self.redirect_to_paper(consultation.paper)


class ConsultationForwardView(ConsultationBaseView):
    """
    Vorlage an die nächste Station weiterleiten.

    Voraussetzung: Die aktuelle Station hat ein Ergebnis. Die nächste
    Station wird terminiert (TOP angelegt), sofern eine Zielsitzung
    hinterlegt ist — sonst Hinweis, die Zielsitzung zu wählen.
    """

    permission_required = "edit_meetings"

    def post(self, request, tenant_slug, consultation_id):
        consultation = self.get_consultation(consultation_id)
        paper = consultation.paper

        if not consultation.is_done:
            messages.error(
                request,
                "Weiterleitung erst möglich, wenn das Ergebnis dieser Station vorliegt.",
            )
            return self.redirect_to_paper(paper)

        next_station = paper.consultations.filter(order__gt=consultation.order).order_by("order", "created_at").first()
        if next_station is None:
            messages.info(request, "Dies ist die letzte Station der Beratungsfolge.")
            return self.redirect_to_paper(paper)

        if next_station.agenda_item_id:
            messages.info(
                request,
                f"Station {next_station.order} ({next_station.organization.name}) ist bereits terminiert.",
            )
            return self.redirect_to_paper(paper)

        if next_station.meeting_id is None:
            messages.warning(
                request,
                f"Bitte zuerst eine Zielsitzung für Station {next_station.order} "
                f"({next_station.organization.name}) wählen.",
            )
            return self.redirect_to_paper(paper)

        schedule_consultation(self, request, next_station)
        return self.redirect_to_paper(paper)

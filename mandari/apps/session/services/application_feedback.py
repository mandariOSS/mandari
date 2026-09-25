# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückmeldestand eines eingereichten Antrags für die einreichende Fraktion (Issue #316).

Einzige Stelle, an der Session-Daten für die Rückmeldung an die Fraktion aufbereitet werden – für das
Work-Portal derselben Installation ebenso wie für die Session-API v1. Maßstab ist, was die Fraktion
auch öffentlich sehen dürfte. Es gelten dieselben Ö/NÖ-Regeln wie für die OParl-Schnittstelle
(:mod:`apps.session.oparl_publication`):

- Die Vorlagen- bzw. Drucksachennummer erscheint nur, wenn die Vorlage veröffentlicht ist
  (öffentlich und über Entwurf und Prüfung hinaus).
- Eine Station der Beratungsfolge zeigt Gremium, Rolle, Sitzung (Datum) und Ergebnis nur, wenn
  Vorlage, Sitzung und Tagesordnungspunkt öffentlich sind. Sonst erscheint sie nur als
  „nicht-öffentlich beraten“ – ohne Gremium, Sitzung, Ergebnis oder Beschlussnummer.
- Das Beschlussergebnis stammt ausschließlich aus einer öffentlichen, entscheidenden Station.

Interna der Verwaltung (Bearbeitungsnotizen, nicht-öffentliche Beschluss- und Protokolltexte,
Abstimmungsdetails) werden hier nie gelesen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from django.db.models import Q
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAPIToken,
    SessionApplication,
    SessionConsultation,
    SessionMeeting,
    SessionPaper,
)
from apps.session.oparl_publication import UNVEROEFFENTLICHT

#: Anzeige einer Station, deren Beratung nicht öffentlich ist
NOT_PUBLIC_LABEL = "nicht-öffentlich beraten"
#: Ergebnisse, die eine Vorlage abschließend erledigen (vertagt oder ausstehend zählen nicht)
FINAL_RESULTS = ("approved", "rejected", "noted", "withdrawn")
#: Rollenbezeichnung eines TOP, der die Vorlage ohne Station der Beratungsfolge behandelt
DIRECT_ROLE_LABEL = "Beratung"

#: Farbton je Ergebnis für Badges (gray, green, amber, red, blue)
RESULT_TONES = {"approved": "green", "rejected": "red", "deferred": "amber", "noted": "blue", "withdrawn": "gray"}

_RESULT_LABELS: dict[str, str] = {str(value): str(label) for value, label in SessionConsultation.RESULT_CHOICES}
_ROLE_LABELS: dict[str, str] = {str(value): str(label) for value, label in SessionConsultation.ROLE_CHOICES}
_STATUS_LABELS: dict[str, str] = {
    str(value): str(label) for value, label in SessionApplication._meta.get_field("status").flatchoices
}


@dataclass(frozen=True)
class Station:
    """Eine Station der Beratungsfolge, so wie die Fraktion sie sehen darf."""

    key: str
    order: int
    public: bool
    organization: str = ""
    role: str = ""
    role_label: str = ""
    decisive: bool = False
    meeting_key: str = ""
    meeting_name: str = ""
    start: datetime | None = None
    cancelled: bool = False
    agenda_number: str = ""
    removed_from_agenda: bool = False
    result: str = ""
    result_label: str = ""
    resolution_number: str = ""

    @property
    def scheduled(self) -> bool:
        """Einer (öffentlichen) Sitzung zugeordnet?"""
        return bool(self.meeting_key)

    @property
    def not_public_label(self) -> str:
        return NOT_PUBLIC_LABEL

    @property
    def result_tone(self) -> str:
        return RESULT_TONES.get(self.result, "gray")

    def as_dict(self) -> dict[str, Any]:
        """Form für die API: nicht-öffentliche Stationen nur mit Reihenfolge und Hinweis."""
        if not self.public:
            return {"order": self.order, "public": False, "label": NOT_PUBLIC_LABEL}
        return {
            "order": self.order,
            "public": True,
            "organization": self.organization,
            "role": self.role,
            "role_label": self.role_label,
            "decisive": self.decisive,
            "meeting_name": self.meeting_name or None,
            "start": self.start,
            "cancelled": self.cancelled,
            "agenda_number": self.agenda_number or None,
            "removed_from_agenda": self.removed_from_agenda,
            "result": self.result or None,
            "result_label": self.result_label or None,
            "resolution_number": self.resolution_number or None,
        }


@dataclass(frozen=True)
class Decision:
    """Abschließendes Ergebnis aus einer öffentlichen, entscheidenden Station."""

    station_key: str
    result: str
    result_label: str
    organization: str
    decided_on: date | None
    resolution_number: str

    @property
    def result_tone(self) -> str:
        return RESULT_TONES.get(self.result, "gray")

    def as_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "result_label": self.result_label,
            "organization": self.organization,
            "decided_on": self.decided_on,
            "resolution_number": self.resolution_number or None,
        }


@dataclass(frozen=True)
class Feedback:
    """Rückmeldestand eines Antrags: Eingang, Vorlage, Beratungsfolge, Beschluss."""

    application_id: str
    application_reference: str
    application_status: str
    application_status_label: str
    submitted_at: datetime | None
    received_at: datetime | None
    tenant_name: str
    reference_label: str
    converted: bool
    paper_public: bool
    paper_reference: str
    stations: tuple[Station, ...]
    decision: Decision | None

    @property
    def public_stations(self) -> tuple[Station, ...]:
        return tuple(station for station in self.stations if station.public)

    @property
    def display_reference(self) -> str:
        """Drucksachennummer, sonst Eingangsnummer – für Überschriften und Benachrichtigungen."""
        return self.paper_reference or self.application_reference

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.application_id,
            "reference": self.application_reference,
            "status": self.application_status,
            "status_label": self.application_status_label,
            "submitted_at": self.submitted_at,
            "received_at": self.received_at,
            "paper": {
                "converted": self.converted,
                "public": self.paper_public,
                "reference": self.paper_reference or None,
                "reference_label": self.reference_label,
            },
            "stations": [station.as_dict() for station in self.stations],
            "decision": self.decision.as_dict() if self.decision else None,
        }


# =============================================================================
# Aufbau
# =============================================================================


def build(application: SessionApplication) -> Feedback:
    """Rückmeldestand eines Antrags nach den Ö/NÖ-Regeln aufbauen (reine Leseoperation)."""
    tenant = application.tenant
    paper = paper_for(application)
    paper_public = paper is not None and _paper_published(paper)
    stations = _stations(paper, paper_public) if paper is not None else []
    reference = paper.reference if paper is not None and paper_public else ""
    return Feedback(
        application_id=str(application.pk),
        application_reference=application.reference,
        application_status=application.status,
        application_status_label=_STATUS_LABELS.get(application.status, application.status),
        submitted_at=application.submitted_at,
        received_at=application.received_at,
        tenant_name=tenant.name,
        reference_label=tenant.reference_label or "Vorlagen-Nr.",
        converted=paper is not None,
        paper_public=paper_public,
        paper_reference=reference,
        stations=tuple(stations),
        decision=_decision(stations),
    )


def application_for_token(token: SessionAPIToken, application_id: UUID | str) -> SessionApplication | None:
    """
    Antrag, dessen Rückmeldestand dieses API-Token abrufen darf (Session-API v1).

    Erlaubt sind Anträge desselben Mandanten, die mit diesem Token eingereicht wurden, und – für den
    Tokenwechsel innerhalb einer Installation – Anträge der Organisation, die das Token in mandari Work
    verbunden hat. Alles andere ist für das Token nicht vorhanden.
    """
    from apps.session.services.application_service import submitting_organization_for_token

    access = Q(submitted_via_token=token)
    bound = submitting_organization_for_token(token)
    if bound is not None:
        access |= Q(submitting_organization=bound)
    return (
        SessionApplication.objects.select_related("tenant")
        .filter(access, pk=application_id, tenant_id=token.tenant_id)
        .first()
    )


def paper_for(application: SessionApplication) -> SessionPaper | None:
    """Die aus dem Antrag entstandene Vorlage (die erste, falls mehrere angelegt wurden)."""
    return (
        SessionPaper.objects.filter(tenant_id=application.tenant_id, source_application=application)
        .order_by("created_at")
        .first()
    )


def _paper_published(paper: SessionPaper) -> bool:
    """Dieselbe Regel wie ``oparl_publication.visible_papers``."""
    return bool(paper.is_public) and paper.status not in UNVEROEFFENTLICHT


def _meeting_public(meeting: SessionMeeting | None, tenant_id: Any) -> bool:
    return meeting is None or (bool(meeting.is_public) and meeting.tenant_id == tenant_id)


def _item_public(item: SessionAgendaItem | None, tenant_id: Any) -> bool:
    """Ö-TOP einer Ö-Sitzung desselben Mandanten (wie ``oparl_publication.visible_agenda_items``)."""
    if item is None:
        return True
    return bool(item.is_public) and bool(item.meeting.is_public) and item.meeting.tenant_id == tenant_id


def _stations(paper: SessionPaper, paper_public: bool) -> list[Station]:
    consultations = list(
        SessionConsultation.objects.filter(paper=paper)
        .select_related("organization", "meeting", "agenda_item__meeting")
        .order_by("order", "created_at")
    )
    has_decisive = any(c.authoritative or c.role == "decision" for c in consultations)
    stations = [
        _from_consultation(consultation, index, paper, paper_public)
        for index, consultation in enumerate(consultations, start=1)
    ]
    # TOPs, die die Vorlage ohne Station der Beratungsfolge behandeln (direkt auf die Tagesordnung gesetzt)
    direct = (
        SessionAgendaItem.objects.filter(paper=paper, meeting__tenant_id=paper.tenant_id, consultation__isnull=True)
        .select_related("meeting__organization")
        .order_by("meeting__start", "order")
    )
    for item in direct:
        stations.append(_from_agenda_item(item, len(stations) + 1, paper, paper_public, decisive=not has_decisive))
    return stations


def _from_consultation(
    consultation: SessionConsultation, order: int, paper: SessionPaper, paper_public: bool
) -> Station:
    key = f"station:{consultation.pk}"
    item = consultation.agenda_item
    meeting = consultation.meeting or (item.meeting if item is not None else None)
    tenant_id = paper.tenant_id
    public = (
        paper_public
        and consultation.organization.tenant_id == tenant_id
        and _meeting_public(meeting, tenant_id)
        and _item_public(item, tenant_id)
    )
    if not public:
        return Station(key=key, order=order, public=False)
    # Ergebnis nur mit öffentlicher Sitzung – ohne Sitzung lässt sich nicht prüfen, ob es öffentlich gefasst wurde
    result = consultation.result if meeting is not None and consultation.result != "pending" else ""
    return Station(
        key=key,
        order=order,
        public=True,
        organization=consultation.organization.name,
        role=consultation.role,
        role_label=_ROLE_LABELS.get(consultation.role, consultation.role),
        decisive=bool(consultation.authoritative or consultation.role == "decision"),
        **_meeting_fields(meeting, item),
        result=result,
        result_label=_RESULT_LABELS.get(result, "") if result else "",
    )


def _from_agenda_item(
    item: SessionAgendaItem, order: int, paper: SessionPaper, paper_public: bool, *, decisive: bool
) -> Station:
    key = f"top:{item.pk}"
    meeting = item.meeting
    tenant_id = paper.tenant_id
    public = paper_public and _item_public(item, tenant_id) and meeting.organization.tenant_id == tenant_id
    if not public:
        return Station(key=key, order=order, public=False)
    result = item.vote_result if item.vote_result != "pending" else ""
    return Station(
        key=key,
        order=order,
        public=True,
        organization=meeting.organization.name,
        role="",
        role_label=DIRECT_ROLE_LABEL,
        decisive=decisive,
        **_meeting_fields(meeting, item),
        result=result,
        result_label=_RESULT_LABELS.get(result, "") if result else "",
    )


def _meeting_fields(meeting: SessionMeeting | None, item: SessionAgendaItem | None) -> dict[str, Any]:
    """Sitzungsangaben einer öffentlichen Station (Aufrufer hat die Sichtbarkeit geprüft)."""
    if meeting is None:
        return {}
    fields: dict[str, Any] = {
        "meeting_key": str(meeting.pk),
        "meeting_name": meeting.name,
        "start": meeting.start,
        "cancelled": bool(meeting.cancelled),
    }
    if item is not None:
        fields.update(
            agenda_number=item.number if item.number and item.number != "?" else "",
            removed_from_agenda=bool(item.is_withdrawn),
            resolution_number=item.resolution_number,
        )
    return fields


def _decision(stations: list[Station]) -> Decision | None:
    """Letzte öffentliche, entscheidende Station mit abschließendem Ergebnis."""
    for station in reversed(stations):
        if (
            station.public
            and station.decisive
            and station.result in FINAL_RESULTS
            and not station.removed_from_agenda
            and not station.cancelled
        ):
            return Decision(
                station_key=station.key,
                result=station.result,
                result_label=station.result_label,
                organization=station.organization,
                decided_on=timezone.localtime(station.start).date() if station.start else None,
                resolution_number=station.resolution_number,
            )
    return None

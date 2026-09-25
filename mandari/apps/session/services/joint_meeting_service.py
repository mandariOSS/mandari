# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Sitzungen mehrerer Gremien (Issue #317).

Eine Sitzung hat ein federführendes Gremium (``SessionMeeting.organization``) und optional weitere
Gremien desselben Mandanten (``SessionMeeting.joint_organizations``), etwa eine gemeinsame Sitzung
von Bau- und Umweltausschuss.

- **Besetzung:** aktive Mitgliedschaften aller beteiligten Gremien zum Sitzungsdatum. Wer mehreren
  beteiligten Gremien angehört, hat genau einen Sitz: eine Ladung, eine Anwesenheitszeile und damit
  eine Stimme (Einzelstimmen sind je TOP und Person eindeutig).
- **Zusammenführung je Person:** Stimmberechtigt ist, wer in mindestens einem beteiligten Gremium
  Stimmrecht hat; die vollständige Tagesordnung erhält, wer in mindestens einem Gremium mehr als Gast
  ist. Funktion und Vertretungshinweis stammen aus der maßgeblichen Mitgliedschaft: bevorzugt im
  federführenden Gremium, dann mit Stimmrecht, dann nach Funktion (Vorsitz vor Mitglied).
- **Schutz:** Weitere Gremien müssen zum Mandanten der Sitzung gehören und dürfen nicht das
  federführende Gremium sein – geprüft beim Zuordnen selbst (``m2m_changed``), nicht nur im Formular.
- **Nachvollziehbarkeit:** Änderungen der Zuordnung stehen im Audit-Log und setzen ``updated_at`` der
  Sitzung, damit OParl-Clients sie beim inkrementellen Abgleich erhalten.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q, QuerySet, prefetch_related_objects
from django.utils import timezone

from apps.session.models import SessionMeeting, SessionOrganization, SessionOrganizationMembership, SessionPerson

#: Rangfolge der Funktionen bei mehreren Mitgliedschaften derselben Person (kleiner = maßgeblicher)
ROLE_RANK = {"chair": 0, "deputy_chair": 1, "member": 2, "expert_citizen": 3, "advisor": 4, "guest": 5}


class JointOrganizationError(ValueError):
    """Unzulässige Zuordnung eines weiteren Gremiums (fremder Mandant oder federführendes Gremium)."""


@dataclass
class Seat:
    """Ein Sitz in der Sitzung: eine Person, auch wenn sie mehreren beteiligten Gremien angehört."""

    person: SessionPerson
    #: maßgebliche Mitgliedschaft (Funktion, Vertretungshinweis)
    membership: SessionOrganizationMembership
    #: Stimmrecht in mindestens einem beteiligten Gremium
    has_voting_rights: bool
    #: in mindestens einem beteiligten Gremium mehr als Gast (vollständige Tagesordnung)
    full_agenda: bool
    #: Namen der beteiligten Gremien, denen die Person angehört
    organizations: list[str] = field(default_factory=list)


def active_memberships(meeting: SessionMeeting) -> QuerySet[SessionOrganizationMembership]:
    """Aktive Mitgliedschaften aller beteiligten Gremien zum Sitzungsdatum (aktive Personen)."""
    meeting_date = timezone.localtime(meeting.start).date()
    # Weitere Gremien als Unterabfrage: keine zusätzliche Abfrage, auch ohne vorgeladene Gremien
    joint_ids = SessionMeeting.joint_organizations.through.objects.filter(sessionmeeting_id=meeting.pk).values(
        "sessionorganization_id"
    )
    return (
        SessionOrganizationMembership.objects.filter(
            Q(organization_id=meeting.organization_id) | Q(organization_id__in=joint_ids)
        )
        .select_related("person", "substitute_for", "organization")
        .filter(person__is_active=True)
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=meeting_date))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=meeting_date))
        .order_by("person__family_name", "person__given_name")
    )


def _rank(membership: SessionOrganizationMembership, lead_id: Any) -> tuple[bool, bool, int]:
    return (
        membership.organization_id != lead_id,
        not membership.has_voting_rights,
        ROLE_RANK.get(membership.role, len(ROLE_RANK)),
    )


def merge_seats(meeting: SessionMeeting, memberships: Iterable[SessionOrganizationMembership]) -> list[Seat]:
    """Mitgliedschaften zu Sitzen je Person zusammenführen; die Reihenfolge der Personen bleibt erhalten."""
    lead_id = meeting.organization_id
    seats: dict[Any, Seat] = {}
    for membership in memberships:
        seat = seats.get(membership.person_id)
        org_name = membership.organization.name
        if seat is None:
            seats[membership.person_id] = Seat(
                person=membership.person,
                membership=membership,
                has_voting_rights=membership.has_voting_rights,
                full_agenda=membership.role != "guest",
                organizations=[org_name],
            )
            continue
        seat.has_voting_rights = seat.has_voting_rights or membership.has_voting_rights
        seat.full_agenda = seat.full_agenda or membership.role != "guest"
        if org_name not in seat.organizations:
            seat.organizations.append(org_name)
        if _rank(membership, lead_id) < _rank(seat.membership, lead_id):
            seat.membership = membership
    lead_name = meeting.organization.name
    for seat in seats.values():
        # Federführendes Gremium zuerst, danach alphabetisch
        seat.organizations.sort(key=lambda name: (name != lead_name, name))
    return list(seats.values())


def seats(meeting: SessionMeeting) -> list[Seat]:
    """Sitze der Sitzung: je Person genau einer, über alle beteiligten Gremien."""
    return merge_seats(meeting, active_memberships(meeting))


def seat_for(meeting: SessionMeeting, person: SessionPerson) -> Seat | None:
    """Sitz einer Person in der Sitzung (oder None, wenn sie keinem beteiligten Gremium angehört)."""
    found = merge_seats(meeting, active_memberships(meeting).filter(person=person))
    return found[0] if found else None


def prefetch_joint(meetings: Iterable[SessionMeeting]) -> None:
    """
    Weitere Gremien nur für markierte gemeinsame Sitzungen nachladen (``SessionMeeting.with_joint_flag``).

    Eine Abfrage, wenn die Liste gemeinsame Sitzungen enthält, sonst keine (Performance-Budgets).
    """
    flagged = [meeting for meeting in meetings if getattr(meeting, SessionMeeting.JOINT_FLAG, False)]
    if flagged:
        prefetch_related_objects(flagged, "joint_organizations")


def selectable_organizations(meeting_tenant: Any) -> QuerySet[SessionOrganization]:
    """Gremien, die an einer Sitzung beteiligt sein können (aktiv, eigener Mandant, keine Ämter)."""
    return (
        SessionOrganization.objects.filter(tenant=meeting_tenant, is_active=True)
        .exclude(organization_type="department")
        .order_by("name")
    )


# =============================================================================
# Schutz und Nachvollziehbarkeit beim Zuordnen (m2m_changed)
# =============================================================================


def _check_assignment(meeting: SessionMeeting, organization_ids: Iterable[Any]) -> None:
    ids = set(organization_ids)
    if not ids:
        return
    if meeting.organization_id in ids:
        raise JointOrganizationError("Das federführende Gremium kann nicht zugleich weiteres Gremium sein.")
    foreign = SessionOrganization.objects.filter(pk__in=ids).exclude(tenant_id=meeting.tenant_id).exists()
    if foreign:
        raise JointOrganizationError("Weitere Gremien müssen zum Mandanten der Sitzung gehören.")


def _log_change(meeting: SessionMeeting, action: str, organization_ids: Iterable[Any]) -> None:
    from apps.session import audit

    names = sorted(SessionOrganization.objects.filter(pk__in=list(organization_ids)).values_list("name", flat=True))
    # Zwischengespeicherte Gremienliste der Instanz verwerfen (SessionMeeting.joint_organization_list)
    meeting.__dict__.pop("_joint_organization_list", None)
    SessionMeeting.objects.filter(pk=meeting.pk).update(updated_at=timezone.now())
    key = "hinzugefuegt" if action == "post_add" else "entfernt"
    audit.log_event("update", meeting, tenant=meeting.tenant, changes={"weitere_gremien": {key: names}})


def joint_organizations_changed(
    sender: Any, instance: Any, action: str, reverse: bool, pk_set: set[Any] | None, **kwargs: Any
) -> None:
    """
    Receiver für ``SessionMeeting.joint_organizations`` (beide Richtungen).

    Vor dem Hinzufügen: nur Gremien desselben Mandanten, nie das federführende Gremium.
    Nach Hinzufügen, Entfernen oder Leeren: Audit-Eintrag und ``updated_at`` der Sitzung.
    """
    if kwargs.get("raw"):
        return
    if action == "pre_clear":
        # Beim Leeren kennt post_clear die Gremien nicht mehr: vorher merken
        meetings = [instance] if not reverse else list(instance.joint_meetings.all())
        for meeting in meetings:
            meeting.__dict__["_joint_cleared_ids"] = list(meeting.joint_organizations.values_list("pk", flat=True))
        if reverse:
            instance.__dict__["_joint_cleared_meetings"] = meetings
        return
    if action == "post_clear":
        meetings = [instance] if not reverse else instance.__dict__.pop("_joint_cleared_meetings", [])
        for meeting in meetings:
            cleared = meeting.__dict__.pop("_joint_cleared_ids", [])
            if cleared:
                _log_change(meeting, "post_remove", cleared)
        return
    if not pk_set or action not in ("pre_add", "post_add", "post_remove"):
        return
    if not reverse:
        if action == "pre_add":
            _check_assignment(instance, pk_set)
        else:
            _log_change(instance, action, pk_set)
        return
    # Rückrichtung: organization.joint_meetings.add(meeting, …)
    for meeting in SessionMeeting.objects.filter(pk__in=pk_set).select_related("tenant"):
        if action == "pre_add":
            _check_assignment(meeting, [instance.pk])
        else:
            _log_change(meeting, action, [instance.pk])

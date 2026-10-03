# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gremienbesetzungen: eine Regel für „laufend“, Plausibilität und Periodenwechsel (Issues #27, #39).

**Laufend** ist eine Besetzung an einem Stichtag, wenn sie spätestens an diesem Tag beginnt und
frühestens an diesem Tag endet (``end_date`` ist der letzte Tag der Mitgliedschaft). Als **aktiv**
zählt sie, wenn zusätzlich die Person aktiv ist. Dieselbe Regel verwendet die Ladung und
Anwesenheit (:func:`apps.session.services.joint_meeting_service.active_memberships`); Gremienliste,
Gremienansicht, Aufnahme und Periodenwechsel bestimmen die Besetzung jetzt ebenso.

**Plausibilität**: Das Ende liegt nie vor dem Beginn, und eine Person hat in einem Gremium nie zwei
Besetzungen, deren Zeiträume sich überschneiden. Damit kann auch die Datenbankregel
„ein Beginn je Person und Gremium“ (``unique_together``) nicht mehr verletzt werden.

**Periodenwechsel** (:func:`change_term`): Alles in einer Transaktion. Besetzungen, die vor dem
Stichtag beginnen und über ihn hinaus laufen, enden am Vortag; im Modus „übernehmen“ beginnt am
Stichtag eine gleiche Besetzung in der neuen Periode (nur für aktive Personen). Besetzungen, die erst am oder nach dem Stichtag
beginnen, gehören bereits zur neuen Periode und bleiben unverändert; beginnen sie erst nach dem Ende der
neuen Periode, behalten sie ihre bisherige Periode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, cast

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.session.models import SessionLegislativeTerm, SessionOrganizationMembership


def _terms() -> Any:
    """Wahlperioden-Modell für die (noch untypisierten) Klassenmethoden ``for_date``/``current_for``."""
    return cast(Any, SessionLegislativeTerm)


MODE_CARRY = "carry"
MODE_FRESH = "fresh"


def running_q(day: date, prefix: str = "") -> Q:
    """Besetzung läuft am Stichtag: Beginn spätestens, Ende frühestens an diesem Tag."""
    return (Q(**{f"{prefix}start_date__isnull": True}) | Q(**{f"{prefix}start_date__lte": day})) & (
        Q(**{f"{prefix}end_date__isnull": True}) | Q(**{f"{prefix}end_date__gte": day})
    )


def active_q(day: date, prefix: str = "") -> Q:
    """Laufende Besetzung einer aktiven Person – wie in Ladung und Anwesenheit."""
    return running_q(day, prefix) & Q(**{f"{prefix}person__is_active": True})


def period_error(start: date | None, end: date | None) -> str:
    """Meldung, wenn das Ende vor dem Beginn liegt, sonst leer."""
    if start is not None and end is not None and end < start:
        return f"Das Ende ({end:%d.%m.%Y}) liegt vor dem Beginn ({start:%d.%m.%Y})."
    return ""


def overlapping(
    organization: Any, person: Any, start: date | None, end: date | None, *, exclude_pk: Any = None
) -> QuerySet[SessionOrganizationMembership]:
    """Besetzungen derselben Person im selben Gremium, deren Zeitraum [start, end] berührt (offen = unbegrenzt)."""
    qs = SessionOrganizationMembership.objects.filter(organization=organization, person=person)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if end is not None:
        qs = qs.filter(Q(start_date__isnull=True) | Q(start_date__lte=end))
    if start is not None:
        qs = qs.filter(Q(end_date__isnull=True) | Q(end_date__gte=start))
    return qs


def overlap_error(
    organization: Any, person: Any, start: date | None, end: date | None, *, exclude_pk: Any = None
) -> str:
    """Meldung, wenn die Person im Zeitraum schon eine Besetzung im Gremium hat, sonst leer."""
    other = overlapping(organization, person, start, end, exclude_pk=exclude_pk).order_by("start_date").first()
    if other is None:
        return ""
    if other.end_date is None:
        return f"{person.display_name} ist in diesem Zeitraum bereits Mitglied dieses Gremiums."
    return (
        f"{person.display_name} ist bis einschließlich {other.end_date:%d.%m.%Y} Mitglied dieses Gremiums. "
        "Eine neue Besetzung kann frühestens am Folgetag beginnen; ein versehentliches Ende heben Sie unter "
        "„Verwalten“ auf (Feld „bis“ leeren)."
    )


def term_for(tenant: Any, day: date | None) -> SessionLegislativeTerm | None:
    """Wahlperiode, die den Tag enthält – ohne Rückfall auf die aktuelle Periode."""
    term: SessionLegislativeTerm | None = _terms().for_date(tenant, day, fallback=False)
    return term


# ---------------------------------------------------------------------------
# Funktionen nach Landesrecht (Issue #757)
# ---------------------------------------------------------------------------

#: Ämter, die das Landesrecht erst ab 18 zulässt – Schlüssel wie im Sitzungsrecht (``adult_offices``)
OFFICE_LABELS = {
    "local_chair": "Vorsitz und Stellvertretung im Orts- bzw. Stadtbezirksrat",
    "local_mayor": "Ortsvorsteherin bzw. Ortsvorsteher",
    "member_municipality_mayor": "Bürgermeisterin bzw. Bürgermeister der Mitgliedsgemeinde",
    "municipal_director": "Gemeindedirektorin bzw. Gemeindedirektor",
    "hvb": "Hauptverwaltungsbeamtin bzw. Hauptverwaltungsbeamter",
}


def office_of(role: str, organization: Any) -> str:
    """Amt, das eine Funktion in einem Gremium bedeutet (für die Altersgrenze); leer, wenn keines."""
    if role in ("hvb", "local_mayor", "municipal_director"):
        return role
    if organization.organization_type == "local_council" and role in ("chair", "deputy_chair"):
        return "local_chair"
    if organization.organization_type == "council" and role == "chair":
        from apps.session.services import body_service

        body = organization.body if organization.body_id else body_service.default_body(organization.tenant)
        if (body.body_type or organization.tenant.body_type) == "mitgliedsgemeinde":
            # Den Ratsvorsitz einer Mitgliedsgemeinde führt die Bürgermeisterin bzw. der Bürgermeister (§ 105)
            return "member_municipality_mayor"
    return ""


def role_problems(
    organization: Any, person: Any, role: str, start: date | None, *, exclude_pk: Any = None
) -> list[str]:
    """
    Funktion nach dem Landesprofil in der Fassung zum Beginn der Besetzung prüfen (Meldungen, leer = zulässig):

    - Ämter erst ab 18 (z. B. § 80 Abs. 4, § 92 Abs. 1, § 96 Abs. 1, § 105 Abs. 1, § 106 Abs. 1 NKomVG ab
      01.11.2026), wenn an der Person „volljährig ab“ eingetragen ist
    - Sperrvermerk: Wer als Ausschussvorsitz abberufen wurde, wird in dieser Wahlperiode nicht erneut benannt
      (§ 71 Abs. 8 NKomVG ab 01.11.2026)
    - Höchstzahl der ehrenamtlichen Stellvertretungen des HVB im Gremium am Beginn der Besetzung (§ 81 Abs. 2
      NKomVG ab 01.11.2026: bis zu fünf)
    """
    from apps.session.services import state_law_service

    profile = organization.tenant.state_profile
    if profile is None:
        return []
    day = start or timezone.localdate()
    law = state_law_service.effective(profile, day)
    problems = []
    office = office_of(role, organization)
    adult_from = getattr(person, "adult_from", None)
    if office and office in (law.value("adult_offices") or []) and adult_from is not None and day < adult_from:
        norm = law.norm("adult_offices")
        problems.append(
            f"{OFFICE_LABELS[office]}: erst ab 18 Jahren{f' ({norm})' if norm else ''}. {person.display_name} ist "
            f"erst ab {adult_from:%d.%m.%Y} volljährig."
        )
    if role == "chair" and organization.committee_kind == "main" and law.value("main_committee_chair") == "hvb":
        norm = law.norm("main_committee_chair")
        problems.append(
            "Den Vorsitz im Hauptausschuss führt die Hauptverwaltungsbeamtin bzw. der Hauptverwaltungsbeamte"
            f"{f' ({norm})' if norm else ''}. Bitte die Funktion „Hauptverwaltungsbeamtin/-beamter (kraft Amtes)“ "
            "wählen."
        )
    if role == "chair" and organization.organization_type == "committee" and law.value("chair_recall_bar", False):
        recalled = SessionOrganizationMembership.objects.filter(
            organization=organization,
            person=person,
            role="chair",
            end_reason=SessionOrganizationMembership.END_RECALLED,
        )
        if exclude_pk is not None:
            recalled = recalled.exclude(pk=exclude_pk)
        term = term_for(organization.tenant, day)
        if term is not None:
            recalled = recalled.filter(legislative_term=term)
        if recalled.exists():
            norm = law.norm("chair_recall_bar")
            problems.append(
                f"{person.display_name} wurde als Vorsitz dieses Ausschusses abberufen und kann in dieser "
                f"Wahlperiode nicht erneut benannt werden{f' ({norm})' if norm else ''}."
            )
    limit = law.value("hvb_deputies_max")
    if role == SessionOrganizationMembership.ROLE_HVB_DEPUTY and isinstance(limit, int) and limit > 0:
        running = SessionOrganizationMembership.objects.filter(
            Q(start_date__isnull=True) | Q(start_date__lte=day),
            Q(end_date__isnull=True) | Q(end_date__gte=day),
            organization=organization,
            role=SessionOrganizationMembership.ROLE_HVB_DEPUTY,
        )
        if exclude_pk is not None:
            running = running.exclude(pk=exclude_pk)
        count = running.count()
        if count >= limit:
            norm = law.norm("hvb_deputies_max")
            problems.append(
                f"Höchstens {limit} ehrenamtliche Stellvertretungen der bzw. des HVB{f' ({norm})' if norm else ''}; "
                f"in diesem Gremium sind am {day:%d.%m.%Y} schon {count} besetzt."
            )
    return problems


def voting_rights(role: str, requested: bool) -> bool:
    """Stimmrecht einer Besetzung: Grundmandat und Hinzugewählte stimmen nach dem Gesetz nie mit."""
    return False if role in SessionOrganizationMembership.ROLES_WITHOUT_VOTE else requested


# ---------------------------------------------------------------------------
# Periodenwechsel
# ---------------------------------------------------------------------------


@dataclass
class TermChange:
    """Ergebnis eines Periodenwechsels für Meldung und Audit-Eintrag."""

    old_term: SessionLegislativeTerm | None
    new_term: SessionLegislativeTerm
    ended: int
    carried: int
    already_new: int


def term_overlap(tenant: Any, start: date | None, end: date | None, *, exclude_pk: Any = None) -> Any:
    """Erste andere Wahlperiode des Mandanten, die sich mit [start, end] überschneidet (offen = unbegrenzt)."""
    for term in SessionLegislativeTerm.objects.filter(tenant=tenant).exclude(pk=exclude_pk):
        if term.start_date is None and term.end_date is None:
            continue  # Perioden ohne Zeitraum enthalten keinen Tag
        if term.overlaps(start, end):
            return term
    return None


def term_change_error(tenant: Any, start_date: date, end_date: date | None) -> str:
    """
    Meldung, wenn der Periodenwechsel so nicht möglich ist, sonst leer.

    Der Stichtag muss nach dem Beginn der laufenden Periode liegen, und die neue Periode darf sich mit
    keiner weiteren Periode überschneiden (die laufende endet am Vortag des Stichtags).
    """
    old_term = _terms().current_for(tenant)
    if old_term is not None and old_term.start_date is not None and old_term.start_date >= start_date:
        return (
            f"Der Beginn der neuen Wahlperiode muss nach dem Beginn der laufenden Wahlperiode "
            f"„{old_term.name}“ ({old_term.start_date:%d.%m.%Y}) liegen."
        )
    conflict = term_overlap(tenant, start_date, end_date, exclude_pk=old_term.pk if old_term else None)
    if conflict is not None:
        return f"Die neue Wahlperiode überschneidet sich mit „{conflict.name}“."
    return ""


def change_term(
    tenant: Any,
    *,
    name: str,
    number: int | None,
    start_date: date,
    end_date: date | None,
    mode: str,
) -> TermChange:
    """
    Neue Wahlperiode anlegen und die laufenden Besetzungen behandeln – ganz oder gar nicht.

    Vorher :func:`term_change_error` prüfen; die Funktion selbst prüft den Zeitraum nicht noch einmal.
    """
    previous_day = start_date - timedelta(days=1)
    with transaction.atomic():
        old_term = _terms().current_for(tenant)

        # Alte Periode zum Vortag abschließen (offen oder über den Stichtag hinaus geplant)
        if old_term is not None and (old_term.end_date is None or old_term.end_date > previous_day):
            old_term.end_date = previous_day
            old_term.save(update_fields=["end_date", "updated_at"])

        new_term = SessionLegislativeTerm.objects.create(
            tenant=tenant, name=name, number=number, start_date=start_date, end_date=end_date
        )

        memberships = SessionOrganizationMembership.objects.filter(organization__tenant=tenant).select_related(
            "organization", "person"
        )
        # Beginnen am oder nach dem Stichtag (und vor dem Ende der neuen Periode): gehören schon zur neuen
        # Periode. Besetzungen nach deren Ende gehören zu einer späteren Periode und bleiben unverändert.
        starts_in_new = Q(start_date__gte=start_date)
        if end_date is not None:
            starts_in_new &= Q(start_date__lte=end_date)
        already_new = 0
        for membership in memberships.filter(starts_in_new).exclude(legislative_term=new_term):
            membership.legislative_term = new_term
            membership.save(update_fields=["legislative_term", "updated_at"])
            already_new += 1

        crossing = list(
            memberships.filter(Q(start_date__isnull=True) | Q(start_date__lt=start_date)).filter(
                Q(end_date__isnull=True) | Q(end_date__gte=start_date)
            )
        )
        carried = 0
        for membership in crossing:
            planned_end = membership.end_date
            membership.end_date = previous_day
            if membership.legislative_term_id is None and old_term is not None:
                membership.legislative_term = old_term
            membership.save()
            # Deaktivierte Personen gehören nicht mehr zur Besetzung (wie in Ladung und Anwesenheit)
            if mode != MODE_CARRY or not membership.person.is_active:
                continue
            # Schon neu besetzt (eigene Besetzung ab dem Stichtag)? Dann nichts doppelt anlegen
            if overlapping(membership.organization, membership.person, start_date, planned_end).exists():
                continue
            SessionOrganizationMembership.objects.create(
                organization=membership.organization,
                person=membership.person,
                role=membership.role,
                has_voting_rights=membership.has_voting_rights,
                start_date=start_date,
                end_date=planned_end,
                legislative_term=new_term,
            )
            carried += 1
    return TermChange(
        old_term=old_term, new_term=new_term, ended=len(crossing), carried=carried, already_new=already_new
    )

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Öffentliche Gremienbesetzungen einer Körperschaft im RIS-Bestand (Issue #762).

Lesender Zugang für Fachmodule, z. B. auf den Bestand aus dem SessionNet-Adapter:

- :func:`current_members` für die Gegenprobe des Stammdaten-Imports in Session,
- :func:`public_roster` zum Vorbefüllen der Importvorlagen (Wahlperioden, Gremien, Personen, Besetzungen).

Liefert nur Namen und Funktionen, keine Kontaktdaten.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date

from django.db.models import Q, QuerySet

from ..models import OParlBody, OParlLegislativeTerm, OParlMembership, OParlOrganization


def find_body(reference: str) -> OParlBody | None:
    """Body über seine UUID oder seine OParl-Kennung (``external_id``); gelöschte zählen nicht."""
    reference = reference.strip()
    bodies = OParlBody.objects.filter(deleted=False)
    try:
        return bodies.filter(pk=uuid.UUID(reference)).first()
    except ValueError:
        return bodies.filter(external_id=reference).first()


def _running_memberships(body: OParlBody, day: date) -> QuerySet[OParlMembership]:
    """
    Am Stichtag laufende Besetzungen des Bodys.

    Besetzungen ohne Zeitraum gelten als laufend (Quellen wie SessionNet nennen keinen). Gelöschte Gremien,
    Personen und Besetzungen bleiben außen vor.
    """
    return (
        OParlMembership.objects.filter(
            organization__body=body,
            deleted=False,
            organization__deleted=False,
            person__deleted=False,
        )
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=day))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=day))
        .select_related("organization", "person")
    )


def _person_name(person: object) -> str:
    name = getattr(person, "name", None) or " ".join(
        part for part in (getattr(person, "given_name", None), getattr(person, "family_name", None)) if part
    )
    return (name or "").strip()


def current_members(body: OParlBody, day: date) -> dict[str, list[str]]:
    """Gremienname -> Namen der am Stichtag laufenden Mitglieder (sortiert, ohne Dubletten)."""
    result: dict[str, set[str]] = {}
    for membership in _running_memberships(body, day):
        name = _person_name(membership.person)
        org_name = (membership.organization.name or "").strip()
        if name and org_name:
            result.setdefault(org_name, set()).add(name)
    return {org: sorted(names) for org, names in result.items()}


@dataclass(frozen=True)
class PublicPerson:
    key: str  # OParl-Kennung der Person (bei SessionNet mit __kpenr)
    name: str
    given_name: str
    family_name: str
    title: str


@dataclass(frozen=True)
class PublicMembership:
    person_key: str
    organization: str  # Name des Gremiums
    role: str
    voting_right: bool | None
    start_date: date | None
    end_date: date | None


@dataclass
class PublicRoster:
    """Öffentlicher Bestand eines Bodys: Grundlage für vorbefüllte Importvorlagen."""

    terms: list[OParlLegislativeTerm] = field(default_factory=list)
    organizations: list[OParlOrganization] = field(default_factory=list)
    persons: list[PublicPerson] = field(default_factory=list)
    memberships: list[PublicMembership] = field(default_factory=list)


def public_roster(body: OParlBody, day: date) -> PublicRoster:
    """
    Wahlperioden, Gremien, Personen und die am Stichtag laufenden Besetzungen eines Bodys.

    Gremien ohne Namen, Personen ohne Namen und gelöschte Objekte fallen weg. Personen erscheinen nur, wenn sie
    am Stichtag eine Besetzung haben; je Person und Gremium zählt eine Besetzung (die erste nach Beginn).
    """
    roster = PublicRoster(
        terms=list(OParlLegislativeTerm.objects.filter(body=body, deleted=False).order_by("start_date", "name")),
        organizations=[
            org
            for org in OParlOrganization.objects.filter(body=body, deleted=False).order_by("name")
            if (org.name or "").strip()
        ],
    )
    persons: dict[str, PublicPerson] = {}
    seen: set[tuple[str, str]] = set()
    rows = sorted(
        _running_memberships(body, day),
        key=lambda m: ((m.organization.name or "").casefold(), m.start_date or date.min, _person_name(m.person)),
    )
    for membership in rows:
        person = membership.person
        name = _person_name(person)
        org_name = (membership.organization.name or "").strip()
        if not name or not org_name or (person.external_id, org_name) in seen:
            continue
        seen.add((person.external_id, org_name))
        persons.setdefault(
            person.external_id,
            PublicPerson(
                key=person.external_id,
                name=name,
                given_name=(person.given_name or "").strip(),
                family_name=(person.family_name or "").strip(),
                title=(person.title or "").strip(),
            ),
        )
        roster.memberships.append(
            PublicMembership(
                person_key=person.external_id,
                organization=org_name,
                role=(membership.role or "").strip(),
                voting_right=membership.voting_right,
                start_date=membership.start_date,
                end_date=membership.end_date,
            )
        )
    roster.persons = list(persons.values())
    return roster

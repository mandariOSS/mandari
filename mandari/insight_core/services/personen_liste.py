# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Angaben der Personenliste im Bürgerportal (Issue #841): Fraktion, Funktion und Gremien je Person.

Die Spalte „Funktion“ zeigte bisher nur Rollen im Gremium mit dem Namen „Rat“ aus einer festen Liste – in Kommunen,
deren Hauptorgan anders heißt oder deren RIS andere Rollennamen führt, blieb sie überall leer. Jetzt kommt sie aus den
laufenden Mitgliedschaften: die wichtigste Rolle je Person, eine Abfrage für die ganze Seite.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from django.db.models import Q
from django.utils import timezone

from ..models import OParlMembership, OParlOrganization, OParlPerson, withdrawn_q
from .question_service import COUNCIL_ORG_NAMES

#: Rollen ohne eigene Aussage („Mitglied“) – sie sagen nur, dass jemand dabei ist
ALLGEMEINE_ROLLEN = {"", "-", "–", "—", "mitglied", "ordentliches mitglied", "member", "ratsmitglied"}
#: So viele Gremien nennt die Liste namentlich, der Rest als Zahl
GREMIEN_NAMENTLICH = 2


@dataclass
class PersonAngaben:
    """Was die Personenliste zu einer Person zeigt."""

    fraktion: OParlOrganization | None = None
    funktion: str = ""
    gremien: list[str] = field(default_factory=list)

    @property
    def gremien_kurz(self) -> str:
        """„Hauptausschuss, Jugendhilfeausschuss und 2 weitere“ bzw. leer."""
        namen = self.gremien[:GREMIEN_NAMENTLICH]
        rest = len(self.gremien) - len(namen)
        text = ", ".join(namen)
        if rest:
            text += f" und {rest} weitere"
        return text


def ist_fraktion(org: OParlOrganization) -> bool:
    return any("fraktion" in (wert or "").lower() for wert in (org.organization_type, org.classification, org.name))


def ist_hauptorgan(org: OParlOrganization) -> bool:
    return (
        (org.name or "") in COUNCIL_ORG_NAMES
        or (org.name or "").lower().startswith("rat der ")
        or (org.classification or "").lower() == "rat"
        or (org.organization_type or "").lower() == "hauptorgan"
    )


def _allgemein(rolle: str | None) -> bool:
    return (rolle or "").strip().lower() in ALLGEMEINE_ROLLEN


def _org_name(org: OParlOrganization) -> str:
    return org.short_name or org.name or ""


def funktion_aus(mitgliedschaften: Iterable[OParlMembership]) -> str:
    """
    Wichtigste laufende Rolle einer Person.

    Rangfolge: besondere Rolle im Hauptorgan (Oberbürgermeisterin, Bürgermeister, Beigeordneter) vor besonderer Rolle
    in der Fraktion (Vorsitz) vor dem Mandat im Hauptorgan („Ratsmitglied“) vor sachkundigen Bürgerinnen und Bürgern
    vor besonderen Rollen in Ausschüssen („Vorsitz, Hauptausschuss“). Nur „Mitglied“ in Ausschüssen ergibt nichts –
    das zeigt die Spalte „Gremien“.
    """
    beste: tuple[int, str] = (0, "")
    for m in mitgliedschaften:
        org = m.organization
        rolle = (m.role or "").strip()
        if ist_hauptorgan(org):
            if not _allgemein(rolle):
                kandidat = (100, rolle)
            elif "rat" in (org.name or "").lower():
                kandidat = (60, "Ratsmitglied")
            else:
                kandidat = (60, f"Mitglied {_org_name(org)}")
        elif ist_fraktion(org):
            kandidat = (80, rolle) if not _allgemein(rolle) else (0, "")
        elif "sachkundig" in rolle.lower():
            kandidat = (50, rolle)
        elif not _allgemein(rolle):
            kandidat = (40, f"{rolle}, {_org_name(org)}")
        else:
            kandidat = (0, "")
        if kandidat[0] > beste[0]:
            beste = kandidat
    return beste[1]


def angaben_fuer(personen: Iterable[OParlPerson], stichtag: date | None = None) -> dict:
    """``{person_id: PersonAngaben}`` aus den laufenden Mitgliedschaften, eine Abfrage für alle Personen."""
    ids = [p.pk for p in personen]
    if not ids:
        return {}
    stichtag = stichtag or timezone.localdate()
    mitgliedschaften = (
        OParlMembership.objects.filter(person_id__in=ids, deleted=False, organization__deleted=False)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=stichtag))
        .filter(Q(organization__end_date__isnull=True) | Q(organization__end_date__gte=stichtag))
        .exclude(withdrawn_q("organization"))
        .select_related("organization")
        .order_by("organization__name")
    )
    je_person: dict = {pid: [] for pid in ids}
    for m in mitgliedschaften:
        je_person[m.person_id].append(m)
    ergebnis = {}
    for pid, liste in je_person.items():
        angaben = PersonAngaben(funktion=funktion_aus(liste))
        for m in liste:
            if ist_fraktion(m.organization):
                angaben.fraktion = angaben.fraktion or m.organization
            elif not ist_hauptorgan(m.organization) and _org_name(m.organization) not in angaben.gremien:
                angaben.gremien.append(_org_name(m.organization))
        ergebnis[pid] = angaben
    return ergebnis

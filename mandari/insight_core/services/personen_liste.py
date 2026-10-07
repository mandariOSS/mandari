# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Angaben der Personenliste im Bürgerportal (Issue #841): Fraktion, Funktion und Gremien je Person.

Die Spalte „Funktion“ zeigte bisher nur Rollen im Gremium mit dem Namen „Rat“ aus einer festen Liste – in Kommunen,
deren Hauptorgan anders heißt oder deren RIS andere Rollennamen führt, blieb sie überall leer. Jetzt kommt sie aus den
laufenden Mitgliedschaften: die wichtigste Rolle je Person, eine Abfrage für die ganze Seite.

Nennt OParl keine Fraktion (viele Kommunen führen Fraktionen nicht als Gremium), gilt die bestätigte
Fraktionszuordnung aus Insight (Issue #916, ``services/fraktionen.py``) – mit Hinweis auf ihre Quelle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.db.models import Q
from django.utils import timezone

from ..models import OParlMembership, OParlOrganization, OParlPerson, PersonFraktion, withdrawn_q
from .fraktionen import aktuelle_fraktionen
from .question_service import COUNCIL_ORG_NAMES

#: Rollen ohne eigene Aussage („Mitglied“) – sie sagen nur, dass jemand dabei ist
ALLGEMEINE_ROLLEN = {"", "-", "–", "—", "mitglied", "ordentliches mitglied", "member", "ratsmitglied"}
#: Auch allgemein: „Ausschussmitglied“, „Stellv. Mitglied“, „stellvertretendes Mitglied“, „Fraktionsmitglied“ …
_ALLGEMEIN_MUSTER = re.compile(
    r"(ordentliches|beratendes|stellv\.?|stellvertretendes|ausschuss|fraktions|rats|gremien)?[\s-]*mitglied(er)?"
)
#: Sachkundige Bürgerinnen und Bürger in allen üblichen Schreibweisen („Sachk. Bürger/in (mit Stimmr.)“)
_SACHKUNDIG = re.compile(r"(^|\W)sachk(undig|\.)", re.IGNORECASE)
#: Vorsitz-Rollen, die ohne Gremium nichtssagend sind („Vorsitz“, „stellv. Vorsitzende“)
_VORSITZ = re.compile(r"^(stellv\.?\s*|stellvertretende[rs]?\s*|\d\.\s*stellv\.?\s*)?vorsitz", re.IGNORECASE)
#: Kurznamen ohne Aussage: dann lieber der volle Name des Gremiums
_NICHTSSAGEND = {"ausschuss", "gremium", "fraktion", "beirat", "rat", "kommission", "arbeitskreis", "sonstiges"}

#: So viele Gremien nennt die Liste namentlich, der Rest als Zahl
GREMIEN_NAMENTLICH = 2


@dataclass
class PersonAngaben:
    """Was die Personenliste zu einer Person zeigt."""

    fraktion: OParlOrganization | None = None
    funktion: str = ""
    gremien: list[str] = field(default_factory=list)
    #: Fraktionszuordnung aus Insight, nur wenn OParl keine Fraktion nennt (Issue #916)
    fraktion_lokal: PersonFraktion | None = None

    @property
    def fraktion_name(self) -> str:
        """Fraktion für die Anzeige: aus OParl (Kurzname, sonst Name), sonst aus der Zuordnung in Insight."""
        if self.fraktion is not None:
            return self.fraktion.short_name or self.fraktion.name or ""
        return self.fraktion_lokal.bezeichnung if self.fraktion_lokal is not None else ""

    @property
    def fraktion_hinweis(self) -> str:
        """Quelle einer Fraktion, die nicht aus OParl kommt („laut Einblendung der Live-Übertragung“), sonst leer."""
        return self.fraktion_lokal.hinweis if self.fraktion is None and self.fraktion_lokal is not None else ""

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
    """Allgemeine Rolle in Ausschuss oder Fraktion: „Mitglied“, „Ausschussmitglied“, „stellv. Mitglied“ …"""
    text = (rolle or "").strip().lower()
    return text in ALLGEMEINE_ROLLEN or bool(_ALLGEMEIN_MUSTER.fullmatch(text))


def _schlicht(rolle: str | None) -> bool:
    """Schlichte Mitgliedschaft im Hauptorgan (leer, „Mitglied“, „Ratsmitglied“, „ordentliches Mitglied“).

    Stellvertretende und beratende Mitglieder haben kein volles Mandat und dürfen nicht als „Ratsmitglied“ erscheinen.
    """
    return (rolle or "").strip().lower() in ALLGEMEINE_ROLLEN


def _ratsname(org: OParlOrganization) -> bool:
    """Heißen die Mitglieder dieses Hauptorgans „Ratsmitglied“ (Rat, Stadtrat, Gemeinderat, „Rat der Stadt …“)?"""
    name = (org.name or "").strip().lower()
    return name in {"rat", "stadtrat", "gemeinderat"} or name.startswith("rat der ") or name.startswith("rat des ")


def _org_name(org: OParlOrganization) -> str:
    """Name eines Gremiums für die Liste: Kurzname nur, wenn er etwas sagt, sonst der volle Name."""
    kurz = (org.short_name or "").strip()
    voll = (org.name or "").strip()
    nichtssagend = {wert.lower() for wert in (org.classification, org.organization_type) if wert} | _NICHTSSAGEND
    # Abgeschnittene Kurznamen („Ausschuss für Soziales, Gesundheit und A“) enden mitten in einem Wort des vollen
    # Namens; echte Kürzel wie „SPD“ vor „SPD-Fraktion“ bleiben
    abgeschnitten = (
        bool(kurz)
        and len(voll) > len(kurz)
        and voll.startswith(kurz)
        and kurz[-1].isalnum()
        and voll[len(kurz)].isalnum()
    )
    if kurz and kurz.lower() not in nichtssagend and len(kurz) >= 3 and not abgeschnitten:
        return kurz
    return voll or kurz


def funktion_aus(mitgliedschaften: Iterable[OParlMembership]) -> str:
    """
    Wichtigste laufende Rolle einer Person.

    Rangfolge: besondere Rolle im Hauptorgan (Oberbürgermeisterin, Bürgermeister, Beigeordneter) vor besonderer Rolle
    in der Fraktion (Vorsitz) vor dem Mandat im Hauptorgan („Ratsmitglied“, „Mitglied Regionalrat“) vor
    stellvertretenden oder beratenden Mitgliedern des Hauptorgans („stellv. Mitglied, Regionalrat“) vor sachkundigen
    Bürgerinnen und Bürgern vor besonderen Rollen in Ausschüssen („Vorsitz, Hauptausschuss“). Nur „Mitglied“ in Ausschüssen ergibt nichts –
    das zeigt die Spalte „Gremien“.
    """
    beste: tuple[int, str] = (0, "")
    for m in mitgliedschaften:
        org = m.organization
        rolle = (m.role or "").strip()
        if ist_hauptorgan(org):
            if _VORSITZ.match(rolle):
                # „Vorsitz“ allein sagt nicht, wovon: mit Gremium
                kandidat = (100, f"{rolle}, {_org_name(org)}")
            elif _schlicht(rolle):
                kandidat = (60, "Ratsmitglied" if _ratsname(org) else f"Mitglied {_org_name(org)}")
            elif _allgemein(rolle):
                # Stellvertretende oder beratende Mitglieder: wörtlich und mit Gremium, nie „Ratsmitglied“
                kandidat = (55, f"{rolle}, {_org_name(org)}")
            else:
                kandidat = (100, rolle)
        elif ist_fraktion(org):
            kandidat = (80, rolle) if not _allgemein(rolle) else (0, "")
        elif _SACHKUNDIG.search(rolle):
            kandidat = (50, rolle)
        elif not _allgemein(rolle):
            kandidat = (40, f"{rolle}, {_org_name(org)}")
        else:
            kandidat = (0, "")
        if kandidat[0] > beste[0]:
            beste = kandidat
    return beste[1]


def angaben_fuer(personen: Iterable[OParlPerson], stichtag: date | None = None) -> dict[Any, PersonAngaben]:
    """
    ``{person_id: PersonAngaben}`` aus den laufenden Mitgliedschaften, eine Abfrage für alle Personen.

    Ohne Fraktion aus OParl kommt sie aus den bestätigten Zuordnungen in Insight (eine weitere Abfrage).
    """
    personen = list(personen)
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
    je_person: dict[Any, list[OParlMembership]] = {pid: [] for pid in ids}
    for m in mitgliedschaften:
        je_person[m.person_id].append(m)
    ergebnis: dict[Any, PersonAngaben] = {}
    for pid, liste in je_person.items():
        angaben = PersonAngaben(funktion=funktion_aus(liste))
        for m in liste:
            if ist_fraktion(m.organization):
                angaben.fraktion = angaben.fraktion or m.organization
            elif not ist_hauptorgan(m.organization) and _org_name(m.organization) not in angaben.gremien:
                angaben.gremien.append(_org_name(m.organization))
        ergebnis[pid] = angaben
    ohne_fraktion = [p for p in personen if ergebnis[p.pk].fraktion is None]
    for pid, zuordnung in aktuelle_fraktionen(ohne_fraktion, stichtag=stichtag).items():
        ergebnis[pid].fraktion_lokal = zuordnung
    return ergebnis

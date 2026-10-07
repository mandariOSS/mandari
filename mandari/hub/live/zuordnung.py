# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zuordnung gelesener Angaben zum RIS-Bestand (Issue #915).

- **TOP:** gelesene Nummer → öffentlicher Tagesordnungspunkt der Sitzung mit derselben Nummer (normalisiert: ``5``,
  ``5.``, ``Ö 5`` und ``TOP 5`` sind gleich; ``1.1`` bleibt ``1.1``). Die Ähnlichkeit des gelesenen Titels zum
  Titel im RIS wird zur Kontrolle gespeichert.
- **Person:** gelesener Name → Person. Verglichen wird in einer Vergleichsform ohne akademische Titel, Umlaute
  ausgeschrieben. Zuerst unter den Mitgliedern des Gremiums am Sitzungstag, dann in der ganzen Kommune.
  ``eindeutig`` nur bei genau einem Treffer; mehrere Treffer oder nur ein ähnlicher Name (ab ``AEHNLICH``) unter
  den Mitgliedern ergeben ``unsicher``, sonst ``keine``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Final

from django.db.models import Q

from insight_core.models import OParlAgendaItem, OParlMeeting, OParlMembership, OParlPerson

from .lesung import vereinfacht
from .models import SpeechAssignment

#: Ab dieser Ähnlichkeit gilt ein Name unter den Gremiumsmitgliedern als unsicherer Treffer
AEHNLICH: Final = 0.88
#: Vorsprung des besten unsicheren Treffers vor dem zweitbesten
VORSPRUNG: Final = 0.05
_NUMMER = re.compile(r"\d{1,3}(?:\.\d{1,3}){0,3}")
_TITEL = re.compile(
    r"\b(dr|prof|dipl|ing|med|phil|rer|nat|jur|pol|oec|habil|mdl|mdb)\b",
    re.IGNORECASE,
)


def normalisiere_nummer(nummer: str | None) -> str | None:
    """``"Ö 5."`` → ``"5"``, ``"TOP 1.1"`` → ``"1.1"``; ohne Ziffern ``None``."""
    if not nummer:
        return None
    treffer = _NUMMER.search(nummer.replace(" ", ""))
    return treffer.group(0).rstrip(".") if treffer else None


def aehnlichkeit(gelesen: str | None, im_ris: str | None) -> float | None:
    """Ähnlichkeit (0 bis 1) des gelesenen Titels zum Anfang des Titels im RIS; ohne beide ``None``."""
    if not gelesen or not im_ris:
        return None
    a = vereinfacht(gelesen).rstrip(". ")
    b = vereinfacht(im_ris)
    return round(difflib.SequenceMatcher(None, a, b[: len(a) + 10]).ratio(), 2)


def tagesordnungspunkt(meeting_id: object, nummer: str) -> OParlAgendaItem | None:
    """Öffentlicher, nicht gelöschter Tagesordnungspunkt der Sitzung mit der Nummer."""
    gesucht = normalisiere_nummer(nummer)
    if gesucht is None:
        return None
    kandidaten = OParlAgendaItem.objects.filter(meeting_id=meeting_id, deleted=False, public=True).only(
        "id", "number", "name", "order"
    )
    for punkt in sorted(kandidaten, key=lambda p: (p.order is None, p.order or 0)):
        if normalisiere_nummer(punkt.number) == gesucht:
            return punkt
    return None


def vergleichsname(name: str) -> str:
    """Name ohne akademische Titel in Vergleichsform: ``"Dr. Erika Muster-Beispiel"`` → ``"erika muster beispiel"``."""
    ohne_titel = _TITEL.sub(" ", name.replace(".", ". "))
    return vereinfacht(ohne_titel)


def _namen(person: OParlPerson) -> set[str]:
    formen = {person.name or ""}
    if person.family_name:
        formen.add(f"{person.given_name or ''} {person.family_name}")
    return {v for v in (vergleichsname(f) for f in formen) if v}


@dataclass(frozen=True)
class Personentreffer:
    person: OParlPerson | None
    zuordnung: str


KEIN_TREFFER: Final = Personentreffer(None, SpeechAssignment.KEINE)


def _exakt(name: str, personen: Iterable[OParlPerson]) -> list[OParlPerson]:
    return [p for p in personen if name in _namen(p)]


def gremiumsmitglieder(organization_id: object, stichtag: date) -> list[OParlPerson]:
    """Personen mit Mitgliedschaft im Gremium am Stichtag (ohne gelöschte)."""
    mitgliedschaften = (
        OParlMembership.objects.filter(organization_id=organization_id, deleted=False, person__deleted=False)
        .filter(Q(start_date__isnull=True) | Q(start_date__lte=stichtag))
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=stichtag))
        .select_related("person")
    )
    personen: dict[object, OParlPerson] = {}
    for mitgliedschaft in mitgliedschaften:
        personen.setdefault(mitgliedschaft.person_id, mitgliedschaft.person)
    return list(personen.values())


def person_zuordnen(gelesen: str, *, meeting: OParlMeeting, organization_id: object) -> Personentreffer:
    """Ordnet einen gelesenen Namen einer Person zu (siehe Moduldokumentation)."""
    name = vergleichsname(gelesen)
    if len(name) < 3:
        return KEIN_TREFFER
    stichtag = meeting.start.date() if meeting.start else date.today()
    mitglieder = gremiumsmitglieder(organization_id, stichtag)
    treffer = _exakt(name, mitglieder)
    if len(treffer) == 1:
        return Personentreffer(treffer[0], SpeechAssignment.EINDEUTIG)
    if len(treffer) > 1:
        return Personentreffer(None, SpeechAssignment.UNSICHER)

    nachname = gelesen.split()[-1] if gelesen.split() else ""
    if len(nachname) >= 2:
        in_der_kommune = OParlPerson.objects.filter(body_id=meeting.body_id, deleted=False).filter(
            Q(family_name__iexact=nachname) | Q(name__icontains=nachname)
        )[:200]
        treffer = _exakt(name, in_der_kommune)
        if len(treffer) == 1:
            return Personentreffer(treffer[0], SpeechAssignment.EINDEUTIG)
        if len(treffer) > 1:
            return Personentreffer(None, SpeechAssignment.UNSICHER)

    bewertet = sorted(
        (
            (max(difflib.SequenceMatcher(None, name, n).ratio() for n in _namen(p)), str(p.pk), p)
            for p in mitglieder
            if _namen(p)
        ),
        key=lambda t: (t[0], t[1]),
        reverse=True,
    )
    if bewertet and bewertet[0][0] >= AEHNLICH and (len(bewertet) == 1 or bewertet[0][0] - bewertet[1][0] >= VORSPRUNG):
        return Personentreffer(bewertet[0][2], SpeechAssignment.UNSICHER)
    return KEIN_TREFFER

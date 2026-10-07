# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zuordnung gelesener Angaben zum RIS-Bestand (Issue #915).

- **TOP** (``top_zuordnen``): gelesene Nummer und gelesener Titel → öffentlicher Tagesordnungspunkt **derselben
  Sitzung** (Kandidaten sind nur deren Punkte). Nummern werden normalisiert (``5``, ``5.``, ``Ö 5`` und ``TOP 5``
  sind gleich; ``1.1`` bleibt ``1.1``). Die Texterkennung verliert Punkte („TOP 1.1“ → „11“), deshalb zählen auch
  die Lesarten mit Punkten (``nummer_varianten``: „11“ → 11, 1.1). Entschieden wird in dieser Reihenfolge:

  1. eine Lesart der Nummer, deren Titel dem gelesenen Titel mindestens ``titel_zur_nummer`` (Profil, Standard
     0,6) gleicht; bei mehreren der ähnlichste Titel, bei Gleichstand die gelesene Nummer selbst
     (Sicherheit ``nummer_titel``);
  2. sonst der Tagesordnungspunkt mit dem ähnlichsten Titel ab ``titel_allein`` (Standard 0,75), auch ohne
     passende Nummer, wenn der gelesene Titel lang genug ist (``TITEL_MINDESTLAENGE``) und der zweitbeste um
     ``TITEL_VORSPRUNG`` zurückliegt (``titel``);
  3. sonst die gelesene Nummer selbst bzw. die einzige passende Lesart, mit niedriger Sicherheit (``nummer``);
  4. sonst kein Tagesordnungspunkt (``keine``).

  Die Titelähnlichkeit (``aehnlichkeit``) vergleicht in der Vergleichsform (``lesung.vereinfacht``: klein,
  Umlaute auf den Grundbuchstaben, Ersatzzeichen der Texterkennung entfernt, ohne Satzzeichen und „…“) mit dem
  Anfang des Titels im RIS. Sie verzeiht einen fehlenden ersten Buchstaben (``_PRAEFIX_FEHLT``) und einen am
  Ende abgeschnittenen Titel.
- **Person:** gelesener Name → Person. Verglichen wird in einer Vergleichsform ohne akademische Titel, Umlaute
  ausgeschrieben. Zuerst unter den Mitgliedern des Gremiums am Sitzungstag, dann in der ganzen Kommune.
  ``eindeutig`` nur bei genau einem Treffer; mehrere Treffer oder nur ein ähnlicher Name (ab ``AEHNLICH``) unter
  den Mitgliedern ergeben ``unsicher``, sonst ``keine``.
"""

from __future__ import annotations

import difflib
import itertools
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from django.db.models import Q

from insight_core.models import OParlAgendaItem, OParlMeeting, OParlMembership, OParlPerson

from .lesung import vereinfacht
from .models import SectionConfidence, SpeechAssignment
from .profil import TITEL_ALLEIN, TITEL_ZUR_NUMMER

#: Ab dieser Ähnlichkeit gilt ein Name unter den Gremiumsmitgliedern als unsicherer Treffer
AEHNLICH: Final = 0.88
#: Vorsprung des besten unsicheren Treffers vor dem zweitbesten
VORSPRUNG: Final = 0.05
#: Vorsprung des ähnlichsten Titels vor dem zweitbesten, damit der Titel allein entscheidet (gleich lautende TOPs)
TITEL_VORSPRUNG: Final = 0.05
#: Mindestlänge des gelesenen Titels (Vergleichsform), damit er allein entscheiden darf
TITEL_MINDESTLAENGE: Final = 8
#: So viele Zeichen darf der gelesene Titel am Anfang verloren haben (Ausschnitt beginnt zu weit rechts)
_PRAEFIX_FEHLT: Final = 2
#: Spielraum am Ende des Vergleichsausschnitts im RIS-Titel (verlesene oder fehlende Zeichen)
_SPIELRAUM: Final = 3
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


def nummer_varianten(nummer: str | None) -> list[str]:
    """
    Lesarten einer gelesenen Nummer, deren Punkte die Texterkennung verloren haben kann, die gelesene zuerst:
    ``"11"`` → ``["11", "1.1"]``, ``"111"`` → ``["111", "11.1", "1.11", "1.1.1"]``. Mit Punkt nur sie selbst.
    """
    gelesen = normalisiere_nummer(nummer)
    if gelesen is None:
        return []
    if "." in gelesen:
        return [gelesen]
    lesarten: list[str] = []
    for punkte in range(1, len(gelesen)):
        # mehr Ziffern vorn zuerst: „11.1“ vor „1.11“
        for stellen in sorted(itertools.combinations(range(1, len(gelesen)), punkte), reverse=True):
            teile = [gelesen[a:b] for a, b in itertools.pairwise((0, *stellen, len(gelesen)))]
            lesarten.append(".".join(teile))
    return [gelesen, *lesarten]


def aehnlichkeit(gelesen: str | None, im_ris: str | None) -> float | None:
    """
    Ähnlichkeit (0 bis 1, zwei Stellen) des gelesenen Titels zum Anfang des Titels im RIS; ohne beide ``None``.
    Verziehen werden bis zu ``_PRAEFIX_FEHLT`` fehlende Zeichen am Anfang und ein abgeschnittenes Ende.
    """
    if not gelesen or not im_ris:
        return None
    a = vereinfacht(gelesen)
    b = vereinfacht(im_ris)
    if not a or not b:
        return None
    beste = 0.0
    for anfang in range(min(_PRAEFIX_FEHLT, len(b) - 1) + 1):
        stueck = b[anfang : anfang + len(a) + _SPIELRAUM]
        beste = max(beste, difflib.SequenceMatcher(None, a, stueck, autojunk=False).ratio())
    return round(beste, 2)


@dataclass(frozen=True)
class TopTreffer:
    """Ergebnis von ``top_zuordnen``."""

    #: zugeordneter Tagesordnungspunkt (``None`` bei ``keine``)
    punkt: OParlAgendaItem | None
    #: Nummer des Abschnitts: die des Tagesordnungspunkts in Normalform, ohne Treffer die gelesene
    nummer: str
    #: gelesene Nummer in Normalform
    gelesen: str
    #: Ähnlichkeit des gelesenen Titels zum Titel des Tagesordnungspunkts
    titel_aehnlichkeit: float | None
    #: ``SectionConfidence``
    sicherheit: str

    @property
    def schluessel(self) -> str:
        """Schlüssel der Entprellung: der Tagesordnungspunkt, ohne Treffer die Nummer."""
        return abschnittsschluessel(self.punkt.pk if self.punkt is not None else None, self.nummer)


def abschnittsschluessel(agenda_item_id: uuid.UUID | None, nummer: str) -> str:
    """Schlüssel eines Abschnitts für die Entprellung (``TopTreffer.schluessel``)."""
    return f"top:{agenda_item_id}" if agenda_item_id is not None else f"nummer:{nummer}"


@dataclass(frozen=True)
class _Bewertung:
    punkt: OParlAgendaItem
    #: Nummer des Tagesordnungspunkts in Normalform
    nummer: str | None
    #: Titelähnlichkeit zur Lesung
    wert: float | None


def kandidaten(meeting_id: uuid.UUID) -> list[OParlAgendaItem]:
    """Öffentliche, nicht gelöschte Tagesordnungspunkte der Sitzung in ihrer Reihenfolge."""
    punkte = OParlAgendaItem.objects.filter(meeting_id=meeting_id, deleted=False, public=True).only(
        "id", "meeting_id", "number", "name", "order"
    )
    return sorted(punkte, key=lambda p: (p.order is None, p.order or 0, str(p.pk)))


def top_zuordnen(
    meeting_id: uuid.UUID,
    nummer: str | None,
    titel: str | None,
    *,
    titel_zur_nummer: float = TITEL_ZUR_NUMMER,
    titel_allein: float = TITEL_ALLEIN,
    varianten: bool = True,
    punkte: Sequence[OParlAgendaItem] | None = None,
) -> TopTreffer | None:
    """
    Ordnet eine gelesene TOP-Angabe einem Tagesordnungspunkt der Sitzung zu (siehe Moduldokumentation); ohne
    Ziffern in der Nummer ``None``. ``varianten=False`` prüft nur die Nummer selbst (von Hand gesetzt).
    ``punkte`` ersetzt die Abfrage der Kandidaten (mehrere Lesungen derselben Sitzung).
    """
    gelesen = normalisiere_nummer(nummer)
    if gelesen is None:
        return None
    alle = list(kandidaten(meeting_id) if punkte is None else punkte)
    lesarten = nummer_varianten(gelesen) if varianten else [gelesen]
    bewertet = [_Bewertung(p, normalisiere_nummer(p.number), aehnlichkeit(titel, p.name)) for p in alle]

    def treffer(b: _Bewertung, sicherheit: str) -> TopTreffer:
        return TopTreffer(b.punkt, b.nummer or gelesen, gelesen, b.wert, sicherheit)

    # 1. Lesart der Nummer, die der Titel bestätigt: ähnlichster Titel, bei Gleichstand die frühere Lesart
    bestaetigt = [b for b in bewertet if b.nummer in lesarten and b.wert is not None and b.wert >= titel_zur_nummer]
    if bestaetigt:
        beste = max(bestaetigt, key=lambda b: (b.wert or 0.0, -lesarten.index(b.nummer or "")))
        return treffer(beste, SectionConfidence.NUMMER_UND_TITEL)

    # 2. Titel allein (Nummer verlesen), nur mit deutlichem Vorsprung vor dem zweitbesten
    nach_titel = sorted((b for b in bewertet if b.wert is not None), key=lambda b: -(b.wert or 0.0))
    if (
        titel
        and len(vereinfacht(titel)) >= TITEL_MINDESTLAENGE
        and nach_titel
        and (nach_titel[0].wert or 0.0) >= titel_allein
        and (len(nach_titel) == 1 or (nach_titel[0].wert or 0.0) - (nach_titel[1].wert or 0.0) >= TITEL_VORSPRUNG)
    ):
        return treffer(nach_titel[0], SectionConfidence.TITEL)

    # 3. Nummer allein: die gelesene, sonst die einzige passende Lesart
    genau = [b for b in bewertet if b.nummer == gelesen]
    passend = genau or [b for b in bewertet if b.nummer in lesarten]
    if genau or len(passend) == 1:
        return treffer(passend[0], SectionConfidence.NUMMER)
    return TopTreffer(None, gelesen, gelesen, None, SectionConfidence.KEINE)


def tagesordnungspunkt(meeting_id: uuid.UUID, nummer: str) -> OParlAgendaItem | None:
    """Öffentlicher, nicht gelöschter Tagesordnungspunkt der Sitzung mit genau dieser Nummer (ohne Titelprüfung)."""
    gesucht = normalisiere_nummer(nummer)
    if gesucht is None:
        return None
    for punkt in kandidaten(meeting_id):
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


def gremiumsmitglieder(organization_id: uuid.UUID, stichtag: date) -> list[OParlPerson]:
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


def person_zuordnen(gelesen: str, *, meeting: OParlMeeting, organization_id: uuid.UUID) -> Personentreffer:
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

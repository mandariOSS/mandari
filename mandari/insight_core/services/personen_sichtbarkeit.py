# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Welche Personen mandari öffentlich zeigt (Issue #1560, Datensparsamkeit).

Öffentlich ist nur, wer **derzeit ein Amt hat**: mindestens eine laufende Mitgliedschaft in einem bestehenden Gremium
(Rat, Ausschuss, Bezirksvertretung, Fraktion, Aufsichtsrat …, auch sachkundige Bürgerinnen und Bürger). Alle anderen
bleiben im Bestand, erscheinen aber nirgends: nicht in Listen, Suche, Sitemap, Merkliste, KI-Assistent, MCP und
Schnittstelle; ihre Seite gibt es nicht (404). Im Zweifel verborgen: ohne Mitgliedschaften aus der Quelle, mit
beendeter Mitgliedschaft, in einem beendeten oder gelöschten Gremium, mit bloßem Zugang zum RIS (Systemrolle) und als
Test- oder Platzhalterkonto.

Eine Regel an einer Stelle: Jede Ausgabe fragt hier, keine entscheidet selbst. ``oeffentlich()`` ist die Unterabfrage
für Querysets, ``nur_oeffentliche()`` der fertige Filter, ``oeffentliche_ids()`` und ``ist_oeffentlich()`` prüfen
geladene Objekte. Laufend heißt wie überall (``hub.ris.selectors.public_current_memberships``): bis heute begonnen und
nicht beendet; ohne Datum gilt eine Mitgliedschaft als laufend.

Fotos, E-Mail-Adresse, Telefonnummer, Geschlecht und Lebenslauf einer Person zeigt mandari gar nicht, auch nicht bei
öffentlichen Personen (Vorlagen, ``hub.ris.mapping.bestand``).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from ..models import OParlMembership, OParlPerson, withdrawn_q
from .testkonten import ist_testkonto, systemrolle_q, testkonto_q


def laufend_q(on: date | None = None, prefix: str = "") -> Q:
    """Bedingung „Mitgliedschaft läuft am Tag ``on``“ (Standard heute); ``prefix`` für Beziehungen zur Mitgliedschaft."""
    on = on or timezone.localdate()
    feld = f"{prefix}__" if prefix else ""
    return (
        (Q(**{f"{feld}start_date__isnull": True}) | Q(**{f"{feld}start_date__lte": on}))
        & (Q(**{f"{feld}end_date__isnull": True}) | Q(**{f"{feld}end_date__gte": on}))
        & (Q(**{f"{feld}organization__end_date__isnull": True}) | Q(**{f"{feld}organization__end_date__gte": on}))
    )


def aemter(on: date | None = None) -> QuerySet[OParlMembership]:
    """Mitgliedschaften, die als laufendes Amt zählen (siehe Modulbeschreibung)."""
    return (
        OParlMembership.objects.filter(laufend_q(on), deleted=False, organization__deleted=False)
        .exclude(withdrawn_q("organization"))
        .exclude(systemrolle_q())
    )


def oeffentlich(person: str = "pk", on: date | None = None) -> Exists:
    """
    Unterabfrage „die Person hat derzeit ein Amt“. ``person`` nennt das Feld der äußeren Abfrage (``"pk"`` für
    Personen, ``"person_id"`` für Mitgliedschaften, ``"recipient"`` für Fragen).
    """
    return Exists(aemter(on).filter(person=OuterRef(person)))


def nur_oeffentliche(queryset: QuerySet[OParlPerson], on: date | None = None) -> QuerySet[OParlPerson]:
    """Die Personen des Querysets, die mandari öffentlich zeigt."""
    return queryset.filter(oeffentlich("pk", on), deleted=False).exclude(testkonto_q())


def mitgliedschaften_oeffentlicher(
    mitgliedschaften: QuerySet[OParlMembership], on: date | None = None
) -> QuerySet[OParlMembership]:
    """Die Mitgliedschaften, deren Person mandari öffentlich zeigt (für Mitgliederlisten von Gremien)."""
    return mitgliedschaften.filter(oeffentlich("person_id", on), person__deleted=False).exclude(testkonto_q("person"))


def an_oeffentliche(fragen: QuerySet[Any], on: date | None = None) -> QuerySet[Any]:
    """
    Die Ratsfragen, deren Empfängerin oder Empfänger mandari öffentlich zeigt: Eine veröffentlichte Frage nennt
    den Namen, sie erscheint deshalb nur, solange die Person ein Amt hat.
    """
    return fragen.filter(oeffentlich("recipient_id", on), recipient__deleted=False).exclude(testkonto_q("recipient"))


def oeffentliche_ids(person_ids: Iterable[Any], on: date | None = None) -> set[Any]:
    """Die Kennungen aus ``person_ids``, deren Person mandari öffentlich zeigt (eine Abfrage)."""
    ids = [pk for pk in person_ids if pk]
    if not ids:
        return set()
    return set(nur_oeffentliche(OParlPerson.objects.filter(pk__in=ids), on).values_list("pk", flat=True))


def ist_oeffentlich(person: OParlPerson | None, on: date | None = None) -> bool:
    """Zeigt mandari diese Person öffentlich?"""
    if person is None or person.deleted or ist_testkonto(person):
        return False
    return aemter(on).filter(person=person).exists()

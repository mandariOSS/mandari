# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lese-Fassade des RIS-Bestands (``docs/adr/20260929-kanonisches-modell.md``, Issue #522).

Fachmodule (Work, Session, Bürgerportal) lesen RIS-Daten über diese fachlichen Abfragen statt direkt
über ``OParl*.objects``. So hängen sie nicht am Tabellenschema von ``insight_core``, und Regeln wie
„zurückgenommene Beratungen zählen nicht“ stehen an einer Stelle.

- Abfragen über „die Kommunen“ nehmen ``bodies`` entgegen: ein QuerySet oder eine Liste von
  ``OParlBody`` (bzw. deren Kennungen), in der Regel ``organization.get_all_bodies()``.
- Einzelabfragen (``meeting``, ``paper`` …) nehmen die Kennung, wie sie aus einer URL oder einem
  Formular kommt; eine ungültige Kennung ergibt ``None`` bzw. ``False`` statt eines Fehlers.
- Rückgaben sind vorerst QuerySets der Bestandsmodelle, damit Aufrufer weiter vorladen
  (``prefetch_related``), begrenzen und zählen können. Filter auf Tabellenfelder gehören in neue
  Funktionen hier, nicht in den Aufrufer.

Die Fassade liest nur. Geschrieben wird der RIS-Bestand vom Ingestor und von der Abbildung für
Session-Mandanten. ``scripts/check_ris_access_ratchet.py`` zählt Direktzugriffe außerhalb von ``hub``
und ``insight_core``; die Zahl darf nur sinken.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date, datetime

from django.db.models import Q, QuerySet
from django.utils import timezone

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    withdrawn_q,
)

#: Kommunen, auf die sich eine Abfrage beschränkt.
Bodies = QuerySet[OParlBody] | Iterable[OParlBody | uuid.UUID]
#: Gremien als Objekte oder Kennungen.
Organizations = Iterable[OParlOrganization | uuid.UUID]


def _uuid(value: object) -> uuid.UUID | None:
    """Kennung aus URL oder Formular; ``None``, wenn es keine gültige UUID ist."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


# =============================================================================
# Sitzungen
# =============================================================================


def meetings(bodies: Bodies) -> QuerySet[OParlMeeting]:
    """Sitzungen der Kommunen."""
    return OParlMeeting.objects.filter(body__in=bodies)


def meeting(bodies: Bodies, meeting_id: object) -> OParlMeeting | None:
    """Eine Sitzung der Kommunen; ``None``, wenn es sie dort nicht gibt."""
    pk = _uuid(meeting_id)
    return meetings(bodies).filter(pk=pk).first() if pk else None


def meeting_by_id(meeting_id: object) -> OParlMeeting | None:
    """
    Eine Sitzung über alle Kommunen – für Ausgaben, die nicht auf Kommunen beschränkt sind (der
    OParl-Aggregator). Auch eine in der Quelle gelöschte oder zurückgenommene Sitzung: Der Aufrufer
    entscheidet, was er dafür ausgibt. ``None``, wenn es sie nicht gibt.
    """
    pk = _uuid(meeting_id)
    return OParlMeeting.objects.filter(pk=pk).first() if pk else None


def upcoming_meetings(bodies: Bodies, *, since: datetime | None = None) -> QuerySet[OParlMeeting]:
    """Nicht abgesagte Sitzungen ab ``since`` (Standard: jetzt), nach Beginn sortiert."""
    since = since or timezone.now()
    return meetings(bodies).filter(start__gte=since, cancelled=False).order_by("start")


def search_meetings(bodies: Bodies, text: str = "", *, on: date | None = None) -> QuerySet[OParlMeeting]:
    """Sitzungen an einem Tag (``on``) oder mit ``text`` im Namen, neueste zuerst."""
    found = meetings(bodies)
    found = found.filter(start__date=on) if on else found.filter(name__icontains=text)
    return found.order_by("-start")


def meetings_of_organizations(
    organizations: Organizations,
    *,
    starts_from: datetime | None = None,
    starts_after: datetime | None = None,
    starts_until: datetime | None = None,
    include_cancelled: bool = True,
) -> QuerySet[OParlMeeting]:
    """
    Sitzungen, an denen mindestens eines der Gremien beteiligt ist, nach Beginn sortiert.

    Der Zeitraum ist wahlweise ab ``starts_from`` (einschließlich) oder nach ``starts_after``
    (ausschließlich) und bis ``starts_until`` (einschließlich); mit einer Grenze fallen Sitzungen
    ohne Beginn heraus. Jede Sitzung erscheint einmal, auch wenn mehrere Gremien sie abhalten.
    """
    found = OParlMeeting.objects.filter(organizations__in=list(organizations))
    if starts_from is not None:
        found = found.filter(start__gte=starts_from)
    if starts_after is not None:
        found = found.filter(start__gt=starts_after)
    if starts_until is not None:
        found = found.filter(start__lte=starts_until)
    if not include_cancelled:
        found = found.filter(cancelled=False)
    return found.distinct().order_by("start")


def meetings_by_external_id(external_ids: Iterable[str]) -> QuerySet[OParlMeeting]:
    """Sitzungen zu OParl-Kennungen (URLs), etwa aus einer Beratung."""
    return OParlMeeting.objects.filter(external_id__in=list(external_ids))


# =============================================================================
# Tagesordnungspunkte
# =============================================================================


def agenda_items(meeting: OParlMeeting) -> QuerySet[OParlAgendaItem]:
    """Tagesordnungspunkte einer Sitzung in ihrer Reihenfolge."""
    return OParlAgendaItem.objects.filter(meeting=meeting).order_by("order", "number")


def agenda_item_exists(agenda_item_id: object) -> bool:
    """Gibt es diesen Tagesordnungspunkt im RIS-Bestand?"""
    pk = _uuid(agenda_item_id)
    return pk is not None and OParlAgendaItem.objects.filter(pk=pk).exists()


def agenda_items_by_external_id(external_ids: Iterable[str]) -> QuerySet[OParlAgendaItem]:
    """Tagesordnungspunkte zu OParl-Kennungen (URLs), etwa aus einer Beratung."""
    return OParlAgendaItem.objects.filter(external_id__in=list(external_ids))


def papers_of_agenda_item(agenda_item: OParlAgendaItem) -> QuerySet[OParlPaper]:
    """Vorlagen, die unter einem Tagesordnungspunkt beraten werden (ohne zurückgenommene Beratungen)."""
    consultations = (
        OParlConsultation.objects.filter(agenda_item_external_id=agenda_item.external_id, paper__isnull=False)
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("paper"))
    )
    return OParlPaper.objects.filter(id__in=consultations.values("paper_id")).distinct()


# =============================================================================
# Vorlagen
# =============================================================================


def papers(bodies: Bodies) -> QuerySet[OParlPaper]:
    """Vorlagen der Kommunen."""
    return OParlPaper.objects.filter(body__in=bodies)


def paper(bodies: Bodies, paper_id: object) -> OParlPaper | None:
    """Eine Vorlage der Kommunen; ``None``, wenn es sie dort nicht gibt."""
    pk = _uuid(paper_id)
    return papers(bodies).filter(pk=pk).first() if pk else None


def paper_exists(paper_id: object) -> bool:
    """Gibt es diese Vorlage im RIS-Bestand?"""
    pk = _uuid(paper_id)
    return pk is not None and OParlPaper.objects.filter(pk=pk).exists()


def search_papers(bodies: Bodies, text: str) -> QuerySet[OParlPaper]:
    """Vorlagen mit ``text`` im Namen oder in der Drucksachennummer, neueste zuerst."""
    return papers(bodies).filter(Q(name__icontains=text) | Q(reference__icontains=text)).order_by("-date")


def recent_papers(bodies: Bodies, *, limit: int = 5) -> QuerySet[OParlPaper]:
    """Neueste Vorlagen (nach Datum, dann Anlage im RIS)."""
    return papers(bodies).order_by("-date", "-oparl_created")[:limit]


def consultations_of_paper(paper: OParlPaper) -> QuerySet[OParlConsultation]:
    """Beratungsfolge einer Vorlage (ohne zurückgenommene Beratungen)."""
    return OParlConsultation.objects.filter(paper=paper).exclude(withdrawn_q())


def files_of_paper(paper: OParlPaper) -> QuerySet[OParlFile]:
    """Dateien (Anlagen) einer Vorlage."""
    return OParlFile.objects.filter(paper=paper)


# =============================================================================
# Gremien und Personen
# =============================================================================


def organizations(bodies: Bodies) -> QuerySet[OParlOrganization]:
    """Gremien der Kommunen."""
    return OParlOrganization.objects.filter(body__in=bodies)


def organization(bodies: Bodies, organization_id: object) -> OParlOrganization | None:
    """Ein Gremium der Kommunen; ``None``, wenn es das dort nicht gibt."""
    pk = _uuid(organization_id)
    return organizations(bodies).filter(pk=pk).first() if pk else None


def active_organizations(bodies: Bodies, *, on: date | None = None) -> QuerySet[OParlOrganization]:
    """Gremien ohne Enddatum oder mit Enddatum ab ``on`` (Standard: heute)."""
    on = on or timezone.localdate()
    return organizations(bodies).filter(Q(end_date__isnull=True) | Q(end_date__gte=on))


def organizations_by_external_id(external_ids: Iterable[str]) -> QuerySet[OParlOrganization]:
    """Gremien zu OParl-Kennungen (URLs), etwa aus den Rohdaten einer Sitzung."""
    return OParlOrganization.objects.filter(external_id__in=list(external_ids))


def persons(bodies: Bodies) -> QuerySet[OParlPerson]:
    """Personen der Kommunen."""
    return OParlPerson.objects.filter(body__in=bodies)


def memberships_of_person(person: OParlPerson, *, bodies: Bodies | None = None) -> QuerySet[OParlMembership]:
    """Mitgliedschaften einer Person in Gremien, wahlweise nur in Gremien der Kommunen."""
    found = OParlMembership.objects.filter(person=person)
    if bodies is not None:
        found = found.filter(organization__body__in=bodies)
    return found


# =============================================================================
# Orte
# =============================================================================


def locations(bodies: Bodies) -> QuerySet[OParlLocation]:
    """Orte der Kommunen (eigene Location-Objekte der Quellen, nicht die Ortsangaben an Sitzungen)."""
    return OParlLocation.objects.filter(body__in=bodies)


# =============================================================================
# Dateien
# =============================================================================


def files(bodies: Bodies) -> QuerySet[OParlFile]:
    """Dateien der Kommunen."""
    return OParlFile.objects.filter(body__in=bodies)

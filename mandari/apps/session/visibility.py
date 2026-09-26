# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichtbarkeit nichtöffentlicher Inhalte im Session RIS – eine Regel für alle Ansichten.

Listen, Auswahlfelder, Suche, Detailseiten, Aktionen und Arbeitsvorräte filtern über diese Bausteine
statt über eigene ``is_public``-Bedingungen:

- ``SessionMeeting.objects.visible_to(permissions)``: nichtöffentliche Sitzungen nur mit
  ``view_non_public_meetings``
- ``SessionAgendaItem.objects.visible_to(permissions)``: nichtöffentliche TOPs und alle TOPs
  nichtöffentlicher Sitzungen nur mit ``view_non_public_meetings``
- ``SessionPaper.objects.visible_to(permissions)``: nichtöffentliche Vorlagen nur mit
  ``view_non_public_papers``
- ``SessionFile.objects.visible_to(permissions)``: Gegenstück zu ``file_service.file_visible`` –
  Sichtrecht des Elternobjekts (Vorlage: ``view_papers``, Sitzung/TOP: ``view_meetings``), bei einer
  nichtöffentlichen Anlage oder einem nichtöffentlichen Elternobjekt zusätzlich das NÖ-Recht

Für Beziehungen liefern ``meeting_q``, ``agenda_item_q``, ``paper_q`` und ``file_q`` dieselbe
Bedingung mit Präfix, z. B. ``SessionCosignature.objects.filter(paper_q(permissions, "paper__"))``.

``permissions`` ist die Menge der Rechte einer Person (``SessionPermissionChecker.permissions``).
Die Basis-Sichtrechte für Sitzungen und Vorlagen prüfen die Views selbst; nur bei Anlagen, deren
Elternobjekte verschiedene Rechte verlangen, gehören sie zur Regel.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from django.db import models
from django.db.models import Q

if TYPE_CHECKING:
    # Nur für die Typparameter der QuerySets unten (als Zeichenkette, sonst zirkulärer Import)
    from .models import SessionAgendaItem, SessionFile, SessionMeeting, SessionPaper  # noqa: F401

NON_PUBLIC_MEETINGS = "view_non_public_meetings"
NON_PUBLIC_PAPERS = "view_non_public_papers"


def meeting_q(permissions: Collection[str], prefix: str = "") -> Q:
    """Sitzungen: nichtöffentliche nur mit dem NÖ-Sichtrecht für Sitzungen."""
    if NON_PUBLIC_MEETINGS in permissions:
        return Q()
    return Q(**{f"{prefix}is_public": True})


def agenda_item_q(permissions: Collection[str], prefix: str = "") -> Q:
    """TOPs: nichtöffentliche TOPs und TOPs nichtöffentlicher Sitzungen nur mit dem NÖ-Sichtrecht."""
    if NON_PUBLIC_MEETINGS in permissions:
        return Q()
    return Q(**{f"{prefix}is_public": True, f"{prefix}meeting__is_public": True})


def paper_q(permissions: Collection[str], prefix: str = "") -> Q:
    """Vorlagen: nichtöffentliche nur mit dem NÖ-Sichtrecht für Vorlagen."""
    if NON_PUBLIC_PAPERS in permissions:
        return Q()
    return Q(**{f"{prefix}is_public": True})


def optional(condition: Q, relation: str) -> Q:
    """
    Optionale Beziehung: leer oder sichtbar. Eine leere Bedingung (volles Recht) bleibt leer –
    ``Q() | Q(x__isnull=True)`` ergäbe in Django sonst nur ``x__isnull=True``.
    """
    if not condition:
        return Q()
    return condition | Q(**{f"{relation}__isnull": True})


def file_q(permissions: Collection[str], prefix: str = "") -> Q:
    """
    Anlagen nach der Regel von ``file_service.file_visible``.

    Das Elternobjekt bestimmt das Recht in derselben Reihenfolge wie ``file_service.file_parent``:
    Vorlage vor TOP vor Sitzung; Anlagen ohne Elternobjekt zählen wie Vorlagen-Anlagen.
    """

    def cond(**lookups: Any) -> Q:
        return Q(**{f"{prefix}{key}": value for key, value in lookups.items()})

    parts: list[Q] = []
    if "view_papers" in permissions:
        at_paper = cond(paper__isnull=False)
        orphan = cond(paper__isnull=True, agenda_item__isnull=True, meeting__isnull=True)
        if NON_PUBLIC_PAPERS not in permissions:
            at_paper &= cond(is_public=True, paper__is_public=True)
            orphan &= cond(is_public=True)
        parts += [at_paper, orphan]
    if "view_meetings" in permissions:
        at_item = cond(paper__isnull=True, agenda_item__isnull=False)
        at_meeting = cond(paper__isnull=True, agenda_item__isnull=True, meeting__isnull=False)
        if NON_PUBLIC_MEETINGS not in permissions:
            at_item &= cond(is_public=True, agenda_item__is_public=True, agenda_item__meeting__is_public=True)
            at_meeting &= cond(is_public=True, meeting__is_public=True)
        parts += [at_item, at_meeting]
    if not parts:
        return cond(pk__in=[])
    result = parts[0]
    for part in parts[1:]:
        result |= part
    return result


def meeting_visible(permissions: Collection[str], meeting: Any) -> bool:
    """Einzelne Sitzung nach :func:`meeting_q` (für bereits geladene Objekte)."""
    return NON_PUBLIC_MEETINGS in permissions or bool(meeting.is_public)


def agenda_item_visible(permissions: Collection[str], item: Any) -> bool:
    """Einzelner TOP nach :func:`agenda_item_q` (für bereits geladene Objekte)."""
    return NON_PUBLIC_MEETINGS in permissions or bool(item.is_public and item.meeting.is_public)


def paper_visible(permissions: Collection[str], paper: Any) -> bool:
    """Einzelne Vorlage nach :func:`paper_q` (für bereits geladene Objekte)."""
    return NON_PUBLIC_PAPERS in permissions or bool(paper.is_public)


class MeetingQuerySet(models.QuerySet["SessionMeeting"]):
    def visible_to(self, permissions: Collection[str]) -> MeetingQuerySet:
        return self.filter(meeting_q(permissions))


class AgendaItemQuerySet(models.QuerySet["SessionAgendaItem"]):
    def visible_to(self, permissions: Collection[str]) -> AgendaItemQuerySet:
        return self.filter(agenda_item_q(permissions))


class PaperQuerySet(models.QuerySet["SessionPaper"]):
    def visible_to(self, permissions: Collection[str]) -> PaperQuerySet:
        return self.filter(paper_q(permissions))


class FileQuerySet(models.QuerySet["SessionFile"]):
    def visible_to(self, permissions: Collection[str]) -> FileQuerySet:
        return self.filter(file_q(permissions))

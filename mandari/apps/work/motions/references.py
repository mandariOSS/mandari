# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bezug eines Dokuments: Bezugsantrag und Bezugssitzung (Issue #586).

- **Bezugsantrag** („Änderungsantrag zu …“): ein Dokument der Organisation (``parent_motion``) oder eine
  Vorlage aus dem RIS der eigenen Kommunen (``parent_paper``), höchstens eines von beiden. Wählbar sind
  nur Dokumente, die das Mitglied sehen darf (``Motion.visible_to``), nicht das Dokument selbst und
  keines, das schon (auch über mehrere Stufen) Änderungsantrag zu diesem ist.
- **Bezugssitzung**: eine Sitzung aus dem RIS der eigenen Kommunen (``related_meeting``).

RIS-Vorlagen und -Sitzungen stammen nur aus den Kommunen der Organisation (``get_all_bodies``). Wer ein
Dokument ansieht, erfährt den Titel eines Bezugsdokuments nur, wenn er dieses Dokument auch sehen darf.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import TYPE_CHECKING, Any, cast

from django.db.models import Q, QuerySet
from django.urls import reverse

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Organization

    from .models import Motion

#: Mindestlänge einer Suche und Treffer je Gruppe
MIN_QUERY_LENGTH = 2
RESULT_LIMIT = 8
#: Schutz gegen Endlosschleifen in (fehlerhaften) Altdaten beim Prüfen der Bezugskette
MAX_CHAIN_DEPTH = 50

#: Feste Meldungen für die Oberfläche
BOTH_GIVEN = "Bitte entweder ein Dokument oder eine RIS-Vorlage wählen."
DOCUMENT_NOT_FOUND = "Dokument nicht gefunden."
CYCLE = "Ein Dokument kann sich nicht auf sich selbst oder einen eigenen Änderungsantrag beziehen."
PAPER_NOT_FOUND = "Vorlage nicht gefunden."
MEETING_NOT_FOUND = "Sitzung nicht gefunden."

_GERMAN_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")


def _bodies(organization: Organization) -> Any:
    return cast(Any, organization).get_all_bodies()


def _visible(membership: Membership) -> Any:
    from .models import Motion

    return cast(Any, Motion).visible_to(membership)


def _uuid(raw: str) -> uuid.UUID | None:
    """Formular-ID als UUID; ungültige Werte ergeben keinen Treffer statt eines Fehlers."""
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_german_date(query: str) -> date | None:
    match = _GERMAN_DATE.match(query)
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


# =============================================================================
# Suche
# =============================================================================


def search_documents(motion: Motion, membership: Membership, query: str) -> list[Motion]:
    """Dokumente als Bezugsantrag: sichtbar, nicht im Papierkorb, nicht das Dokument selbst oder seine Änderungsanträge."""
    query = query.strip()
    if len(query) < MIN_QUERY_LENGTH:
        return []
    candidates = (
        _visible(membership)
        .exclude(pk=motion.pk)
        .exclude(parent_motion=motion)
        .filter(title__icontains=query)
        .order_by("-updated_at")
    )
    return list(candidates[:RESULT_LIMIT])


def search_papers(organization: Organization, query: str) -> list[Any]:
    """Vorlagen aus dem RIS der eigenen Kommunen (Name oder Drucksachennummer)."""
    from insight_core.models import OParlPaper

    query = query.strip()
    if len(query) < MIN_QUERY_LENGTH:
        return []
    papers = (
        OParlPaper.objects.filter(body__in=_bodies(organization))
        .filter(Q(name__icontains=query) | Q(reference__icontains=query))
        .order_by("-date")
    )
    return list(papers[:RESULT_LIMIT])


def search_meetings(organization: Organization, query: str) -> list[Any]:
    """Sitzungen aus dem RIS der eigenen Kommunen (Name oder Datum TT.MM.JJJJ)."""
    from insight_core.models import OParlMeeting

    query = query.strip()
    if len(query) < MIN_QUERY_LENGTH:
        return []
    meetings = OParlMeeting.objects.filter(body__in=_bodies(organization))
    day = _parse_german_date(query)
    meetings = meetings.filter(start__date=day) if day else meetings.filter(name__icontains=query)
    return list(meetings.order_by("-start")[:RESULT_LIMIT])


# =============================================================================
# Setzen (liefert eine feste Fehlermeldung oder None)
# =============================================================================


def _is_self_or_amendment(candidate: Motion, motion: Motion) -> bool:
    """Ist ``candidate`` das Dokument selbst oder (auch über mehrere Stufen) Änderungsantrag dazu?"""
    node: Motion | None = candidate
    for _ in range(MAX_CHAIN_DEPTH):
        if node is None:
            return False
        if node.pk == motion.pk:
            return True
        node = node.parent_motion
    return True  # unplausibel tiefe Kette: lieber ablehnen


def set_parent(motion: Motion, membership: Membership, *, motion_id: str = "", paper_id: str = "") -> str | None:
    """Bezugsantrag setzen (Dokument oder RIS-Vorlage); ohne Angaben entfernen."""
    from insight_core.models import OParlPaper

    if motion_id and paper_id:
        return BOTH_GIVEN
    parent: Motion | None = None
    paper = None
    if motion_id:
        parent_pk = _uuid(motion_id)
        parent = _visible(membership).filter(pk=parent_pk).exclude(status="deleted").first() if parent_pk else None
        if parent is None:
            return DOCUMENT_NOT_FOUND
        if _is_self_or_amendment(parent, motion):
            return CYCLE
    elif paper_id:
        paper_pk = _uuid(paper_id)
        if paper_pk is not None:
            paper = OParlPaper.objects.filter(pk=paper_pk, body__in=_bodies(motion.organization)).first()
        if paper is None:
            return PAPER_NOT_FOUND
    motion.parent_motion = parent
    motion.parent_paper = paper
    motion.save(update_fields=["parent_motion", "parent_paper", "updated_at"])
    return None


def set_reference_meeting(motion: Motion, meeting_id: str = "") -> str | None:
    """Bezugssitzung setzen; ohne Angabe entfernen."""
    from insight_core.models import OParlMeeting

    meeting = None
    if meeting_id:
        meeting_pk = _uuid(meeting_id)
        if meeting_pk is not None:
            meeting = OParlMeeting.objects.filter(pk=meeting_pk, body__in=_bodies(motion.organization)).first()
        if meeting is None:
            return MEETING_NOT_FOUND
    motion.related_meeting = meeting
    motion.save(update_fields=["related_meeting", "updated_at"])
    return None


# =============================================================================
# Anzeige
# =============================================================================


def paper_label(paper: Any) -> str:
    """Anzeige einer RIS-Vorlage: Drucksachennummer – Name."""
    return " – ".join(part for part in (paper.reference, paper.name) if part) or "Vorlage"


def meeting_label(meeting: Any) -> str:
    """Anzeige einer RIS-Sitzung: Name, Datum."""
    name = meeting.name or "Sitzung"
    return f"{name}, {meeting.start:%d.%m.%Y}" if meeting.start else name


def visible_amendments(motion: Motion, membership: Membership) -> QuerySet[Motion]:
    """Änderungsanträge zu diesem Dokument, die die Person sehen darf (Rückverweis)."""
    return cast("QuerySet[Motion]", _visible(membership).filter(parent_motion=motion).order_by("-updated_at"))


def reference_context(motion: Motion, membership: Membership, organization: Organization) -> dict[str, Any]:
    """
    Bezug für die Anzeige: Bezugsantrag, Bezugssitzung und sichtbare Änderungsanträge.

    Den Titel eines Bezugsdokuments zeigt die Anzeige nur, wenn die Person es sehen darf, sonst
    „Dokument ohne Freigabe“ ohne Verweis.
    """
    slug = organization.slug
    parent = None
    if motion.parent_motion_id:
        target = motion.parent_motion
        if target is not None and target.can_access(membership):
            url = reverse("work:document_editor", kwargs={"org_slug": slug, "motion_id": target.pk})
            parent = {"label": target.title, "url": url, "kind": "Dokument"}
        else:
            parent = {"label": "Dokument ohne Freigabe", "url": "", "kind": "Dokument"}
    elif motion.parent_paper_id and motion.parent_paper is not None:
        url = reverse("work:ris_paper_detail", kwargs={"org_slug": slug, "paper_id": motion.parent_paper_id})
        parent = {"label": paper_label(motion.parent_paper), "url": url, "kind": "RIS-Vorlage"}
    meeting = None
    if motion.related_meeting_id and motion.related_meeting is not None:
        url = reverse("work:ris_meeting_detail", kwargs={"org_slug": slug, "meeting_id": motion.related_meeting_id})
        meeting = {"label": meeting_label(motion.related_meeting), "url": url}
    return {"parent": parent, "meeting": meeting, "amendments": list(visible_amendments(motion, membership))}

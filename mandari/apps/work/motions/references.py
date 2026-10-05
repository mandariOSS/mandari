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

**Sichtbarkeit eines Änderungsantrags** (Issue #735): Änderungsanträge behalten eigene Rechte. Beim Anlegen
(``?bezug=<id>``) und nach dem Setzen des Bezugsantrags wird nur die *Sichtbarkeit* des Bezugsantrags
vorgeschlagen – wenn die Person ihn sehen darf; RIS-Vorlagen ergeben keinen Vorschlag. Übernehmen kann ihn nur,
wer die Sichtbarkeit des Änderungsantrags ändern darf (``Motion.can_share``). Freigaben, Federführung und
Mitarbeit des Bezugsantrags werden nie übernommen.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING, Any, cast

from django.db.models import QuerySet
from django.urls import reverse

from hub.ris import selectors as ris

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

#: Sichtbarkeiten eines Dokuments mit den Beschriftungen des Teilen-Dialogs (Reihenfolge wie dort)
VISIBILITY_LABELS = {
    "private": "Privat",
    "shared": "Mit bestimmten Personen",
    "organization": "Gesamte Organisation",
}

_GERMAN_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$")


def _bodies(organization: Organization) -> Any:
    return cast(Any, organization).get_all_bodies()


def _visible(membership: Membership) -> Any:
    from .models import Motion

    return cast(Any, Motion).visible_to(membership)


def _selectable(membership: Membership) -> Any:
    """
    Wählbare Bezugsanträge: sichtbar, aber nie aus „Nichtöffentliche Vorgänge“ – deren Titel stünde sonst am
    Änderungsantrag bei Personen, die die Unterlage nicht öffnen dürfen (Issue #873).
    """
    from .models import exclude_sworn_in_only

    return exclude_sworn_in_only(_visible(membership))


def _visible_document(membership: Membership, raw_id: str) -> Motion | None:
    """Dokument der Organisation, das die Person sehen darf, nicht im Papierkorb und als Bezug wählbar."""
    pk = _uuid(raw_id) if raw_id else None
    if pk is None:
        return None
    return cast("Motion | None", _selectable(membership).filter(pk=pk).exclude(status="deleted").first())


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
        _selectable(membership)
        .exclude(pk=motion.pk)
        .exclude(parent_motion=motion)
        .filter(title__icontains=query)
        .order_by("-updated_at")
    )
    return list(candidates[:RESULT_LIMIT])


def search_papers(organization: Organization, query: str) -> list[Any]:
    """Vorlagen aus dem RIS der eigenen Kommunen (Name oder Drucksachennummer)."""
    query = query.strip()
    if len(query) < MIN_QUERY_LENGTH:
        return []
    return list(ris.search_papers(_bodies(organization), query)[:RESULT_LIMIT])


def search_meetings(organization: Organization, query: str) -> list[Any]:
    """Sitzungen aus dem RIS der eigenen Kommunen (Name oder Datum TT.MM.JJJJ)."""
    query = query.strip()
    if len(query) < MIN_QUERY_LENGTH:
        return []
    found = ris.search_meetings(_bodies(organization), query, on=_parse_german_date(query))
    return list(found[:RESULT_LIMIT])


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
    if motion_id and paper_id:
        return BOTH_GIVEN
    parent: Motion | None = None
    paper = None
    if motion_id:
        parent = _visible_document(membership, motion_id)
        if parent is None:
            return DOCUMENT_NOT_FOUND
        if _is_self_or_amendment(parent, motion):
            return CYCLE
    elif paper_id:
        paper = ris.paper(_bodies(motion.organization), paper_id)
        if paper is None:
            return PAPER_NOT_FOUND
    motion.parent_motion = parent
    motion.parent_paper = paper
    motion.save(update_fields=["parent_motion", "parent_paper", "updated_at"])
    return None


def set_reference_meeting(motion: Motion, meeting_id: str = "") -> str | None:
    """Bezugssitzung setzen; ohne Angabe entfernen."""
    meeting = None
    if meeting_id:
        meeting = ris.meeting(_bodies(motion.organization), meeting_id)
        if meeting is None:
            return MEETING_NOT_FOUND
    motion.related_meeting = meeting
    motion.save(update_fields=["related_meeting", "updated_at"])
    return None


# =============================================================================
# Sichtbarkeit eines Änderungsantrags (Issue #735)
# =============================================================================


def suggested_visibility(parent: Motion | None, membership: Membership) -> str | None:
    """
    Sichtbarkeit des Bezugsantrags als Vorschlag für einen Änderungsantrag oder ``None``.

    Nur ein Dokument der Organisation, das die Person sehen darf (``Motion.can_access``) und das nicht im
    Papierkorb liegt. Eine RIS-Vorlage als Bezug hat keine Sichtbarkeit in der Organisation: kein Vorschlag.
    """
    if parent is None or parent.status == "deleted" or not parent.can_access(membership):
        return None
    return parent.visibility if parent.visibility in VISIBILITY_LABELS else None


def visibility_suggestion(motion: Motion, membership: Membership) -> dict[str, str] | None:
    """
    Vorschlag in der Details-Seitenleiste: die Sichtbarkeit des Bezugsantrags, wenn sie abweicht.

    Nur für Personen, die die Sichtbarkeit dieses Dokuments ändern dürfen (``Motion.can_share``). Übernommen
    wird erst im Teilen-Dialog – die Person bestätigt oder wählt etwas anderes.
    """
    if not motion.parent_motion_id:
        return None
    suggested = suggested_visibility(motion.parent_motion, membership)
    if suggested is None or suggested == motion.visibility or not motion.can_share(membership):
        return None
    return {
        "value": suggested,
        "label": VISIBILITY_LABELS[suggested],
        "current": VISIBILITY_LABELS.get(motion.visibility, motion.visibility),
    }


def new_document_context(
    organization: Organization, membership: Membership, query: Mapping[str, Any]
) -> dict[str, Any]:
    """
    Anlegen eines Änderungsantrags (``?bezug=<id>`` aus „Änderungsantrag anlegen“): Bezugsantrag und Sichtbarkeit.

    Ein Bezugsantrag, den die Person nicht sehen darf, ergibt weder Bezug noch Hinweis (auch nicht seinen Titel).
    Die Sichtbarkeit des Bezugsantrags ist vorausgewählt; wählen darf sie nur, wer die Sichtbarkeit eines eigenen
    Dokuments ändern darf (``Motion.can_share`` am neuen Dokument) – sonst bleibt der Standard „privat“.
    """
    from .models import Motion

    parent = _visible_document(membership, str(query.get("bezug") or "").strip())
    own_new_document = cast(Any, Motion)(organization=organization, author=membership)
    return {
        "bezug_parent": parent,
        "visibility_choices": list(VISIBILITY_LABELS.items()),
        "suggested_visibility": suggested_visibility(parent, membership) or "private",
        "can_choose_visibility": parent is not None and own_new_document.can_share(membership),
    }


def apply_to_new_document(motion: Motion, membership: Membership, data: Mapping[str, Any]) -> str | None:
    """
    Bezugsantrag und Sichtbarkeit aus dem Anlegen-Formular übernehmen (vor dem ersten Speichern).

    - ``parent_motion``: nur ein Dokument, das die Person sehen darf; sonst eine feste Meldung und das Dokument
      wird nicht angelegt.
    - ``visibility``: nur mit dem Recht, die Sichtbarkeit des neuen Dokuments zu ändern (``Motion.can_share``);
      sonst bleibt der Standard „privat“. Freigaben, Federführung und Mitarbeit des Bezugsantrags werden nie
      übernommen – Änderungsanträge haben eigene Rechte.
    """
    raw_parent = str(data.get("parent_motion") or "").strip()
    if raw_parent:
        parent = _visible_document(membership, raw_parent)
        if parent is None:
            return DOCUMENT_NOT_FOUND
        motion.parent_motion = parent
    visibility = str(data.get("visibility") or "")
    if visibility in VISIBILITY_LABELS and motion.can_share(membership):
        motion.visibility = visibility
    return None


def _amendment_create_url(motion: Motion, membership: Membership, slug: str) -> str:
    """„Änderungsantrag anlegen“: für Mitglieder mit ``motions.create``, nicht für Dokumente im Papierkorb."""
    member: Any = membership
    if getattr(member, "is_guest", False) or motion.status == "deleted" or not member.has_permission("motions.create"):
        return ""
    return f"{reverse('work:document_create', kwargs={'org_slug': slug})}?bezug={motion.pk}"


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
    Bezug für die Anzeige: Bezugsantrag, Bezugssitzung, sichtbare Änderungsanträge, Vorschlag zur Sichtbarkeit
    (``visibility_suggestion``) und Verweis „Änderungsantrag anlegen“ (``amendment_create_url``).

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
    return {
        "parent": parent,
        "meeting": meeting,
        "amendments": list(visible_amendments(motion, membership)),
        "visibility_suggestion": visibility_suggestion(motion, membership),
        "amendment_create_url": _amendment_create_url(motion, membership, slug),
    }

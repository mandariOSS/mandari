# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesordnung einer Fraktionssitzung: Nummern, Reihenfolge und Standard-Tagesordnung (Issue #872).

Gezählt werden nur angenommene TOPs (``proposal_status="active"``): Offene oder abgelehnte Vorschläge
haben keine Nummer und verschieben die Nummerierung nicht. Der Genehmigungs-TOP („Tagesordnung festlegen
und letztes Protokoll genehmigen“) ist immer TOP 1 des öffentlichen Teils.

Die Standard-Tagesordnung je Organisation (:class:`FactionStandardAgendaItem`) übernimmt
:func:`apply_standard_agenda` in eine neu angelegte Sitzung. Aufgerufen wird sie von beiden Wegen, auf denen
Sitzungen entstehen: der Anlage von Hand (``FactionMeetingListView.post``) und der Sitzungsreihe
(:func:`apps.work.faction.generation.generate_meetings_for_schedule`). Bestehende Sitzungen ändert sie nie.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.models import Max, QuerySet

if TYPE_CHECKING:
    from apps.tenants.models import Organization

    from .models import FactionAgendaItem, FactionMeeting, FactionStandardAgendaItem

logger = logging.getLogger(__name__)

#: TOPs, die auf der Tagesordnung stehen (keine offenen oder abgelehnten Vorschläge)
ACTIVE = "active"
#: Sitzungen, deren Tagesordnung Berechtigte noch ergänzen dürfen (wie ``can_edit`` in der Oberfläche)
AGENDA_OPEN_STATUSES = ("draft", "planned", "invited", "ongoing")
#: Sitzungen, für die noch TOPs vorgeschlagen werden können (vor Sitzungsbeginn)
PROPOSAL_OPEN_STATUSES = ("draft", "planned", "invited")


def numbered_items(meeting: FactionMeeting, visibility: str) -> QuerySet[FactionAgendaItem]:
    """Nummerierte TOPs der obersten Ebene eines Teils, ohne Genehmigungs-TOP und ohne Vorschläge."""
    return meeting.agenda_items.filter(
        visibility=visibility, parent__isnull=True, is_approval_item=False, proposal_status=ACTIVE
    )


def _first_number(meeting: FactionMeeting, visibility: str) -> int:
    """Erste Nummer eines Teils: Im öffentlichen Teil ist TOP 1 der Genehmigungs-TOP, falls vorhanden."""
    if visibility == "public" and meeting.agenda_items.filter(is_approval_item=True).exists():
        return 2
    return 1


def format_number(visibility: str, number: int) -> str:
    """„3“ im öffentlichen, „NÖ 3“ im nicht-öffentlichen Teil."""
    return f"NÖ {number}" if visibility == "internal" else str(number)


def next_number(meeting: FactionMeeting, visibility: str) -> str:
    """Nummer für einen neuen TOP am Ende seines Teils."""
    return format_number(visibility, _first_number(meeting, visibility) + numbered_items(meeting, visibility).count())


def next_order(meeting: FactionMeeting) -> int:
    """Reihenfolge für einen neuen TOP hinter allen TOPs der Tagesordnung."""
    highest = meeting.agenda_items.filter(proposal_status=ACTIVE).aggregate(highest=Max("order"))["highest"]
    return (highest or 0) + 1


def renumber(meeting: FactionMeeting, visibility: str) -> None:
    """TOPs eines Teils nach ihrer Reihenfolge neu nummerieren (Unterpunkte als 2.1, 2.2 …)."""
    for position, item in enumerate(
        numbered_items(meeting, visibility).order_by("order"), start=_first_number(meeting, visibility)
    ):
        number = format_number(visibility, position)
        if item.number != number:
            item.number = number
            item.save(update_fields=["number"])
        for child_position, child in enumerate(item.children.order_by("order"), start=1):
            child_number = f"{number}.{child_position}"
            if child.number != child_number:
                child.number = child_number
                child.save(update_fields=["number"])


# =============================================================================
# Standard-Tagesordnung
# =============================================================================


def standard_items(organization: Organization) -> QuerySet[FactionStandardAgendaItem]:
    """Standard-Tagesordnung der Organisation in ihrer Reihenfolge."""
    from .models import FactionStandardAgendaItem

    return FactionStandardAgendaItem.objects.filter(organization=organization).order_by("order", "created_at")


def standard_agenda_preview(organization: Organization, *, may_view_internal: bool) -> dict[str, Any]:
    """
    Tagesordnung, die eine neue Sitzung erhält (Dialog „Neue Sitzung“): erster TOP und Standard-TOPs.

    Nicht-öffentliche Punkte nur für Vereidigte (NÖ strikt, Issue #64), sonst nur ihre Anzahl.
    """
    faction_settings = (organization.settings or {}).get("faction", {})
    items = list(standard_items(organization))
    title = faction_settings.get("first_agenda_title_with_previous") or ""
    if not title or "{" in title:
        # Platzhalter füllt erst die Sitzung; die Vorschau nennt den Standardtitel
        title = "Tagesordnung festlegen und letztes Protokoll genehmigen"
    internal = [item for item in items if item.visibility == "internal"]
    return {
        "approval_item": faction_settings.get("auto_create_approval_item", True),
        "approval_title": title,
        "public": [item for item in items if item.visibility == "public"],
        "internal": internal if may_view_internal else [],
        "locked_internal": 0 if may_view_internal else len(internal),
        "count": len(items),
    }


def apply_standard_agenda(meeting: FactionMeeting) -> int:
    """
    Standard-TOPs der Organisation in eine neue Sitzung übernehmen; liefert die Zahl angelegter TOPs.

    Die Punkte folgen auf den Genehmigungs-TOP (falls vorhanden) und auf schon vorhandene TOPs, öffentliche
    und nicht-öffentliche getrennt nummeriert. Abgesagte Sitzungen erhalten keine Tagesordnung. Ein zweiter
    Aufruf für dieselbe Sitzung legt nichts an, auch wenn dort inzwischen Standard-TOPs gelöscht wurden.
    """
    from .models import FactionAgendaItem

    if meeting.status == "cancelled":
        return 0
    templates = list(standard_items(meeting.organization))
    if not templates:
        return 0

    with transaction.atomic():
        if meeting.agenda_items.filter(standard_item__isnull=False).exists():
            return 0
        order = next_order(meeting)
        counts = {
            visibility: _first_number(meeting, visibility) + numbered_items(meeting, visibility).count()
            for visibility in ("public", "internal")
        }
        for offset, template in enumerate(templates):
            visibility = template.visibility if template.visibility in counts else "public"
            FactionAgendaItem.objects.create(
                meeting=meeting,
                title=template.title,
                visibility=visibility,
                number=format_number(visibility, counts[visibility]),
                order=order + offset,
                standard_item=template,
            )
            counts[visibility] += 1

    logger.info("Standard-Tagesordnung übernommen (meeting=%s, tops=%d)", meeting.id, len(templates))
    return len(templates)

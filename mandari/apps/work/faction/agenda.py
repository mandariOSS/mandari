# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesordnung einer Fraktionssitzung: Nummern, Reihenfolge und Standard-Tagesordnung (Issue #872).

Gezählt werden nur angenommene TOPs (``proposal_status="active"``): Offene oder abgelehnte Vorschläge
haben keine Nummer und verschieben die Nummerierung nicht. Der Genehmigungs-TOP („Tagesordnung festlegen
und letztes Protokoll genehmigen“) ist immer TOP 1 des öffentlichen Teils.

Die Standard-Tagesordnung je Sitzungsart (:class:`FactionStandardAgendaItem`, Issue #1240) übernimmt
:func:`apply_standard_agenda` in eine neu angelegte Sitzung. Aufgerufen wird sie von beiden Wegen, auf denen
Sitzungen entstehen: der Anlage von Hand (``FactionMeetingListView.post``) und der Sitzungsreihe
(:func:`apps.work.faction.generation.generate_meetings_for_schedule`). Bestehende Sitzungen ändert sie nie. Eine Art
„ohne feste Tagesordnung“ (``agenda_mode`` „free“) bekommt keine Standardpunkte, eine Art ohne nichtöffentlichen Teil
keine nichtöffentlichen.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.db.models import Max, QuerySet

if TYPE_CHECKING:
    from apps.tenants.models import Organization

    from .models import FactionAgendaItem, FactionMeeting, FactionMeetingType, FactionStandardAgendaItem

logger = logging.getLogger(__name__)

#: TOPs, die auf der Tagesordnung stehen (keine offenen oder abgelehnten Vorschläge)
ACTIVE = "active"
#: Sitzungen, deren Tagesordnung Berechtigte noch ergänzen dürfen (wie ``can_edit`` in der Oberfläche)
AGENDA_OPEN_STATUSES = ("draft", "planned", "invited", "ongoing")
#: Sitzungen, für die noch TOPs vorgeschlagen werden können (vor Sitzungsbeginn)
PROPOSAL_OPEN_STATUSES = ("draft", "planned", "invited")
#: Titel des Genehmigungs-TOPs ohne eigene Vorlage (wie ``FactionMeeting.create_approval_agenda_item``)
APPROVAL_TITLE_WITH_PREVIOUS = "Tagesordnung festlegen und letztes Protokoll genehmigen"
APPROVAL_TITLE_NO_PREVIOUS = "Tagesordnung festlegen"


def open_for_members(meeting: FactionMeeting) -> bool:
    """Dürfen Mitglieder mit ``agenda.create``, die die Sitzung nicht verwalten, noch TOPs eintragen?"""
    return meeting.status in AGENDA_OPEN_STATUSES and not meeting.protocol_approved


def open_for_proposals(meeting: FactionMeeting) -> bool:
    """Können für die Sitzung noch TOPs vorgeschlagen werden (vor Sitzungsbeginn)?"""
    return meeting.status in PROPOSAL_OPEN_STATUSES


def lock_meeting(meeting: FactionMeeting) -> None:
    """
    Sitzung bis zum Ende der laufenden Transaktion sperren.

    Nummer und Reihenfolge neuer TOPs werden so nacheinander vergeben: Zwei gleichzeitige Annahmen oder
    Einträge in derselben Sitzung erhalten nicht dieselbe Nummer. Nur innerhalb von ``transaction.atomic``.
    """
    from .models import FactionMeeting

    list(FactionMeeting.objects.select_for_update().filter(pk=meeting.pk).values_list("pk", flat=True))


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


def add_item(
    meeting: FactionMeeting,
    *,
    title: str,
    visibility: str,
    parent: FactionAgendaItem | None = None,
    description: str = "",
) -> FactionAgendaItem:
    """
    TOP ans Ende seines Teils bzw. als letzten Unterpunkt von ``parent`` eintragen (Vorbereitung #855, laufende
    Sitzung #1166). Rechte, Teil und Eingaben prüft der Aufrufer; Unterpunkte eines nicht-öffentlichen TOPs bleiben
    nicht-öffentlich. Die Sitzung ist bis zum Speichern gesperrt, damit gleichzeitige Einträge verschiedene Nummern
    erhalten. Den Änderungsverlauf schreibt das Speichern (``audit.audit_post_save``).
    """
    from .models import FactionAgendaItem
    from .regeln import pruefe_nichtoeffentlich

    if parent is not None and parent.visibility == "internal":
        visibility = "internal"
    # Art ohne nichtöffentlichen Teil (Issue #1240): keine neuen NÖ-TOPs, auch keine Unterpunkte bestehender
    pruefe_nichtoeffentlich(meeting, visibility)
    with transaction.atomic():
        lock_meeting(meeting)
        if parent is not None:
            number = f"{parent.number}.{parent.children.count() + 1}"
        else:
            number = next_number(meeting, visibility)
        item = FactionAgendaItem(
            meeting=meeting,
            title=title,
            number=number,
            visibility=visibility,
            order=next_order(meeting),
            parent=parent,
        )
        if description:
            cast(Any, item).set_description_encrypted(description)
        item.save()
        if parent is None:
            # Gleicht Altbestand aus, in dem offene oder abgelehnte Vorschläge noch mitgezählt wurden
            renumber(meeting, visibility)
            item.refresh_from_db(fields=["number"])
    return item


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


def move_to_part(meeting: FactionMeeting, item: FactionAgendaItem, visibility: str) -> None:
    """
    TOP in den anderen Teil der Tagesordnung legen und beide Teile neu nummerieren (Issue #1160). Rechte prüft der Aufrufer.

    In den nicht-öffentlichen Teil gehen die Unterpunkte mit: Sonst stünden sie weiter in den öffentlichen Listen
    (Einladung, PDF). Zurück im öffentlichen Teil bleiben sie nicht-öffentlich, bis jemand sie einzeln freigibt. Ein
    Unterpunkt eines nicht-öffentlichen TOPs wird aus demselben Grund nicht öffentlich.
    """
    from .regeln import pruefe_nichtoeffentlich

    previous = item.visibility
    if visibility == previous:
        return
    # Art ohne nichtöffentlichen Teil (Issue #1240): nichts in den NÖ-Teil; zurück in den öffentlichen Teil geht
    pruefe_nichtoeffentlich(meeting, visibility)
    parent = item.parent
    if visibility == "public" and parent is not None and parent.visibility == "internal":
        return
    with transaction.atomic():
        lock_meeting(meeting)
        item.visibility = visibility
        item.save(update_fields=["visibility", "updated_at"])
        if visibility == "internal":
            for child in item.children.filter(visibility="public"):
                child.visibility = visibility
                child.save(update_fields=["visibility", "updated_at"])
        renumber(meeting, previous)
        renumber(meeting, visibility)
        # renumber arbeitet auf eigenen Instanzen: Ein späteres save() überschriebe die neue Nummer sonst
        item.refresh_from_db(fields=["number"])


class VerschiebenError(ValueError):
    """Der TOP lässt sich nicht an die gewünschte Stelle legen; die Meldung richtet sich an die Person am Bildschirm."""


def part_of(item: FactionAgendaItem) -> str:
    """Teil der Tagesordnung, in dem ein TOP steht: Unterpunkte stehen im Teil ihres TOPs."""
    parent = item.parent
    return parent.visibility if parent is not None else item.visibility


def siblings(meeting: FactionMeeting, parent: FactionAgendaItem | None, part: str) -> QuerySet[FactionAgendaItem]:
    """Unterpunkte von ``parent`` bzw. die nummerierten TOPs der obersten Ebene eines Teils, in ihrer Reihenfolge."""
    if parent is not None:
        return parent.children.filter(proposal_status=ACTIVE).order_by("order", "number")
    return numbered_items(meeting, part).order_by("order", "number")


def _insert_index(others: Sequence[Any], position: int | None, visible: Callable[[Any], bool] | None) -> int:
    """
    Stelle in ``others`` für ``position`` (ab 0), gezählt unter den Geschwistern, die das Mitglied sieht: Für
    Nicht-Vereidigte fehlen nichtöffentliche Unterpunkte auf der Seite. ``None``: ans Ende.
    """
    if position is None:
        return len(others)
    shown = [i for i, s in enumerate(others) if visible is None or visible(s)]
    position = max(0, position)
    if position < len(shown):
        return shown[position]
    return shown[-1] + 1 if shown else len(others)


def place_item(
    meeting: FactionMeeting,
    item: FactionAgendaItem,
    *,
    parent: FactionAgendaItem | None,
    position: int | None,
    visibility: str | None = None,
    visible: Callable[[Any], bool] | None = None,
) -> None:
    """
    TOP an eine Stelle der Tagesordnung legen (Ziehen am Griff, Ein- und Ausrücken, Issue #1285): auf die oberste Ebene
    eines Teils oder als Unterpunkt von ``parent``, an die Stelle ``position`` unter den Geschwistern (siehe
    :func:`_insert_index`). ``visibility``: gewünschter Teil, ``None`` behält ihn. Rechte prüft der Aufrufer.

    Eine Ebene wie bisher: Ein TOP mit eigenen Unterpunkten wird kein Unterpunkt, unter den Genehmigungs-TOP kommt
    nichts. Unterpunkte eines nichtöffentlichen TOPs sind nichtöffentlich; geht ein TOP in den nichtöffentlichen Teil,
    gehen seine Unterpunkte mit (wie :func:`move_to_part`). Ein nichtöffentlicher Unterpunkt eines öffentlichen TOPs
    bleibt nichtöffentlich, solange niemand den öffentlichen Teil wählt.
    """
    from .regeln import pruefe_nichtoeffentlich
    from .visibility import is_item_internal

    if item.is_approval_item:
        raise VerschiebenError("Der erste TOP bleibt an seiner Stelle.")
    with transaction.atomic():
        lock_meeting(meeting)
        item.refresh_from_db(fields=["parent", "visibility", "order"])
        # Wirksamer Teil: Ein (alter) öffentlich gespeicherter Unterpunkt eines NÖ-TOPs ist nichtöffentlich und bleibt es
        current = "internal" if is_item_internal(item) else item.visibility
        if parent is not None:
            parent.refresh_from_db(fields=["parent", "visibility", "proposal_status", "is_approval_item"])
            if (
                parent.pk == item.pk
                or parent.meeting_id != meeting.pk
                or parent.parent_id is not None
                or parent.is_approval_item
                or parent.proposal_status != ACTIVE
            ):
                raise VerschiebenError("Unter diesen TOP lässt sich kein Unterpunkt legen.")
            if item.children.exists():
                raise VerschiebenError(
                    f"TOP {item.number} hat eigene Unterpunkte und kann deshalb kein Unterpunkt werden."
                )
            target = "internal" if parent.visibility == "internal" else (visibility or current)
            part = parent.visibility
        else:
            target = visibility or current
            part = target
        if target != current:
            # Art ohne nichtöffentlichen Teil (Issue #1240): nichts in den NÖ-Teil, zurück in den öffentlichen schon
            pruefe_nichtoeffentlich(meeting, target)
        previous_part = part_of(item)
        old_order = item.order
        changed: list[str] = []
        if item.parent_id != (parent.pk if parent is not None else None):
            item.parent = parent
            changed.append("parent")
        if item.visibility != target:
            item.visibility = target
            changed.append("visibility")

        others = list(siblings(meeting, parent, part).exclude(pk=item.pk))
        ordered = list(others)
        ordered.insert(_insert_index(others, position, visible), item)
        # Die Reihenfolge-Werte der Gruppe nur neu verteilen; doppelte (Altbestand) oder 0 auf der obersten Ebene, wo
        # der Genehmigungs-TOP mit 0 steht, werden fortlaufend
        values = sorted([s.order for s in others] + [old_order])
        if len(set(values)) < len(values) or (parent is None and values[0] < 1):
            start = max(values[0], 1)
            values = list(range(start, start + len(values)))
        for entry, value in zip(ordered, values, strict=True):
            if entry.pk == item.pk:
                if item.order != value:
                    item.order = value
                    changed.append("order")
            elif entry.order != value:
                entry.order = value
                entry.save(update_fields=["order", "updated_at"])
        if changed:
            item.save(update_fields=[*changed, "updated_at"])
        if "visibility" in changed and target == "internal":
            for child in item.children.filter(visibility="public"):
                child.visibility = target
                child.save(update_fields=["visibility", "updated_at"])
        for touched in dict.fromkeys((previous_part, part)):
            renumber(meeting, touched)
        # renumber arbeitet auf eigenen Instanzen: Ein späteres save() überschriebe die neue Nummer sonst
        item.refresh_from_db(fields=["number"])


# =============================================================================
# Standard-Tagesordnung
# =============================================================================


def standard_items(
    organization: Organization, meeting_type: FactionMeetingType | None = None
) -> QuerySet[FactionStandardAgendaItem]:
    """Standard-Tagesordnung einer Sitzungsart (ohne Angabe der Standardart) in ihrer Reihenfolge (Issue #1240)."""
    from .models import FactionStandardAgendaItem

    same_type = {"meeting_type__is_default": True} if meeting_type is None else {"meeting_type": meeting_type}
    return FactionStandardAgendaItem.objects.filter(organization=organization, **same_type).order_by(
        "order", "created_at"
    )


def standard_items_for_new_meetings(
    organization: Organization, meeting_type: FactionMeetingType | None = None
) -> list[FactionStandardAgendaItem]:
    """
    Standardpunkte, die eine neue Sitzung der Art (ohne Angabe der Standardart) erhält (Issue #1240): keine bei „ohne
    feste Tagesordnung“, ohne nichtöffentlichen Teil nur die öffentlichen.
    """
    from .models import FactionMeetingType
    from .regeln import nichtoeffentlich_erlaubt, tagesordnung_frei

    art = meeting_type if meeting_type is not None else FactionMeetingType.default_for(organization)
    if tagesordnung_frei(art):
        return []
    items = list(standard_items(organization, art))
    if not nichtoeffentlich_erlaubt(art):
        items = [item for item in items if item.visibility != "internal"]
    return items


def approval_title_preview(faction_settings: dict[str, Any], *, has_previous: bool) -> str:
    """
    Titel des Genehmigungs-TOPs für Vorschau und Einstellungen.

    Eine eigene Vorlage mit Platzhaltern (etwa ``{datum_letzte_sitzung}``) füllt erst die Sitzung; bis dahin
    steht der Standardtitel da statt der rohen Platzhalter.
    """
    if has_previous:
        key, default = "first_agenda_title_with_previous", APPROVAL_TITLE_WITH_PREVIOUS
    else:
        key, default = "first_agenda_title_no_previous", APPROVAL_TITLE_NO_PREVIOUS
    title = str(faction_settings.get(key) or "")
    if not title or "{" in title:
        return default
    return title


def new_meeting_has_previous(organization: Organization, meeting_type: FactionMeetingType | None = None) -> bool:
    """
    Erhielte eine neue Sitzung der Art (ohne Angabe der Standardart) eine Vorsitzung und damit „… und letztes
    Protokoll genehmigen“?

    Wie bei der Anlage (Protokollkette, ``kette.py``): Es gibt in derselben Art (Issue #1231) schon eine Sitzung,
    die stattfindet. Der Tag der neuen Sitzung steht hier noch nicht fest; liegt sie vor allen anderen, entfällt
    der Zusatz.
    """
    from .models import FactionMeeting

    return cast(Any, FactionMeeting).find_previous_meeting(organization, meeting_type=meeting_type) is not None


def standard_agenda_preview(
    organization: Organization, *, may_view_internal: bool, meeting_type: FactionMeetingType | None = None
) -> dict[str, Any]:
    """
    Tagesordnung, die eine neue Sitzung der Art (ohne Angabe der Standardart) erhält (Dialog „Neue Sitzung“): erster
    TOP nach den Regeln der Art und ihre Standard-TOPs (Issue #1240).

    Nicht-öffentliche Punkte nur für Vereidigte (NÖ strikt, Issue #64), sonst nur ihre Anzahl.
    """
    from .models import FactionMeetingType
    from .regeln import einstellungen

    art = meeting_type if meeting_type is not None else FactionMeetingType.default_for(organization)
    faction_settings = einstellungen(art)
    items = standard_items_for_new_meetings(organization, art)
    approval_item = faction_settings.get("auto_create_approval_item", True)
    title = (
        approval_title_preview(faction_settings, has_previous=new_meeting_has_previous(organization, art))
        if approval_item
        else ""
    )
    internal = [item for item in items if item.visibility == "internal"]
    return {
        "approval_item": approval_item,
        "approval_title": title,
        "public": [item for item in items if item.visibility == "public"],
        "internal": internal if may_view_internal else [],
        "locked_internal": 0 if may_view_internal else len(internal),
        "count": len(items),
    }


def apply_standard_agenda(meeting: FactionMeeting) -> int:
    """
    Standard-TOPs der Sitzungsart in eine neue Sitzung übernehmen; liefert die Zahl angelegter TOPs.

    Die Punkte folgen auf den Genehmigungs-TOP (falls vorhanden) und auf schon vorhandene TOPs, öffentliche
    und nicht-öffentliche getrennt nummeriert. Abgesagte Sitzungen erhalten keine Tagesordnung, Sitzungen einer Art
    „ohne feste Tagesordnung“ auch nicht (Issue #1240). Ein zweiter Aufruf für dieselbe Sitzung legt nichts an, auch
    wenn dort inzwischen Standard-TOPs gelöscht wurden.
    """
    from .models import FactionAgendaItem

    if meeting.status == "cancelled":
        return 0
    templates = standard_items_for_new_meetings(meeting.organization, meeting.meeting_type)
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

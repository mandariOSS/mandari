# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tagesordnungs-Service für das Session RIS (Issue #26).

Zentrale Logik für:
- Automatische Neu-Nummerierung (Ö-Teil 1..n, NÖ-Teil getrennt N1..Nm,
  Unterpunkte 5.1, 5.2, …)
- Ö/NÖ-Gruppierung für Tagesordnungs- und Einladungsansichten
- Umsortieren (Drag-and-drop-Reihenfolge, Auf/Ab)
"""

from django.utils import timezone

from apps.session.models import SessionAgendaItem, SessionMeeting


def visibility_errors(item: SessionAgendaItem) -> dict[str, str]:
    """
    Ö/NÖ-Regeln eines TOPs (Feld -> Meldung; leer = in Ordnung).

    - Unterpunkte teilen die Öffentlichkeit ihres TOPs: ein nicht-öffentlicher Unterpunkt unter
      einem öffentlichen TOP verriete sich über Nummer und Einladung, ein öffentlicher unter einem
      nicht-öffentlichen den ganzen NÖ-TOP.
    - Eine nicht-öffentliche Vorlage steht nie auf einem öffentlichen TOP (sonst erschiene ihr
      Betreff in Tagesordnung, Einladung und Bürgerportal).
    """
    errors: dict[str, str] = {}
    parent = item.parent
    if parent is not None and parent.is_public != item.is_public:
        teil = "öffentlich" if parent.is_public else "nicht-öffentlich"
        errors["is_public"] = f"Unterpunkte haben dieselbe Öffentlichkeit wie ihr TOP – TOP {parent.number} ist {teil}."
    paper = item.paper
    if item.is_public and paper is not None and not paper.is_public:
        errors["paper"] = (
            f"Die Vorlage {paper.display_reference} ist nicht-öffentlich und kann nur auf einem "
            "nicht-öffentlichen TOP beraten werden."
        )
    return errors


def cascade_visibility(item: SessionAgendaItem) -> int:
    """Unterpunkte folgen der Öffentlichkeit ihres TOPs (einzeln gespeichert: Audit + Rücknahme)."""
    anzahl = 0
    for child in item.sub_items.exclude(is_public=item.is_public):
        child.is_public = item.is_public
        child.save()
        anzahl += 1
    return anzahl


def renumber_agenda(meeting: SessionMeeting) -> None:
    """
    Nummeriert die Tagesordnung einer Sitzung neu.

    Regeln:
    - Öffentlicher Teil: 1..n
    - Nichtöffentlicher Teil: getrennt nummeriert als N1..Nm
    - Unterpunkte erhalten <Eltern-Nr>.<lfd> (z. B. 5.1, N2.1)
    - Abgesetzte TOPs behalten ihre Nummer (Absetzung wird dokumentiert,
      nicht wegnummeriert)

    Die Nummern-/Reihenfolge-Updates laufen als bulk_update und erzeugen
    bewusst keine Audit-Einträge — protokolliert wird die auslösende
    Aktion (Verschieben, Ö/NÖ-Wechsel, Anlegen, Löschen) selbst.
    """
    items = list(meeting.agenda_items.order_by("order", "created_at"))

    children: dict = {}
    for item in items:
        if item.parent_id:
            children.setdefault(item.parent_id, []).append(item)

    top_public = [i for i in items if i.parent_id is None and i.is_public]
    top_non_public = [i for i in items if i.parent_id is None and not i.is_public]

    changed = []
    order_counter = 0

    def assign(item, number):
        nonlocal order_counter
        order_counter += 1
        if item.number != number or item.order != order_counter:
            item.number = number
            item.order = order_counter
            changed.append(item)
        else:
            item.order = order_counter

    for section_items, prefix in [(top_public, ""), (top_non_public, "N")]:
        for idx, item in enumerate(section_items, start=1):
            number = f"{prefix}{idx}"
            assign(item, number)
            for sub_idx, sub_item in enumerate(children.get(item.id, []), start=1):
                assign(sub_item, f"{number}.{sub_idx}")

    if changed:
        # updated_at mitsetzen: bulk_update umgeht auto_now, der Spiegel im Bürgerportal zieht
        # neue Nummern sonst nie nach (inkrementeller Abgleich per modified_since)
        jetzt = timezone.now()
        for item in changed:
            item.updated_at = jetzt
        SessionAgendaItem.objects.bulk_update(changed, ["number", "order", "updated_at"])
        # Sofort im Bürgerportal nachziehen: Nach einem Ö/NÖ-Wechsel stünde dort sonst bis zum
        # nächsten Abgleich eine Lücke („1, 3“)
        if meeting.is_public:
            from django.db import transaction

            from apps.session.oparl_publication import sync_agenda_numbers_to_portal

            nummern = [(item.id, item.number) for item in changed if item.is_public]
            if nummern:
                transaction.on_commit(lambda: sync_agenda_numbers_to_portal(meeting.tenant_id, nummern))


def grouped_agenda(meeting: SessionMeeting, include_non_public: bool = True):
    """
    Tagesordnung Ö/NÖ-gruppiert für Anzeige/Einladung.

    Returns:
        dict mit "public" und "non_public": Listen von Top-Level-TOPs,
        jeweils mit vorgeladenen ``item.children_list``-Unterpunkten.
    """
    qs = meeting.agenda_items.select_related("paper").order_by("order", "number")
    items = list(qs)

    children: dict = {}
    for item in items:
        # Öffentliche Ansichten (Einladung an Gäste, Ö-Niederschrift, Nutzer ohne NÖ-Recht):
        # nie nicht-öffentliche Unterpunkte, auch nicht unter einem öffentlichen TOP
        if item.parent_id and (include_non_public or item.is_public):
            children.setdefault(item.parent_id, []).append(item)
    for item in items:
        item.children_list = children.get(item.id, [])

    result = {
        "public": [i for i in items if i.parent_id is None and i.is_public],
        "non_public": [],
    }
    if include_non_public:
        result["non_public"] = [i for i in items if i.parent_id is None and not i.is_public]
    return result


def apply_order(meeting: SessionMeeting, ordered_ids: list) -> None:
    """
    Neue Reihenfolge (z. B. aus Drag-and-drop) übernehmen und neu nummerieren.

    Unbekannte IDs werden ignoriert; nicht genannte TOPs behalten ihre
    relative Position hinter den genannten.
    """
    items = {str(i.id): i for i in meeting.agenda_items.all()}
    order_counter = 0
    changed = []
    seen = set()
    for item_id in ordered_ids:
        item = items.get(str(item_id))
        if item is None or str(item_id) in seen:
            continue
        seen.add(str(item_id))
        order_counter += 1
        if item.order != order_counter:
            item.order = order_counter
            changed.append(item)
    # Nicht genannte Items hinten anstellen (stabil)
    remaining = [i for key, i in items.items() if key not in seen]
    remaining.sort(key=lambda i: i.order)
    for item in remaining:
        order_counter += 1
        if item.order != order_counter:
            item.order = order_counter
            changed.append(item)
    if changed:
        SessionAgendaItem.objects.bulk_update(changed, ["order"])
    renumber_agenda(meeting)


def move_item(item: SessionAgendaItem, direction: str) -> bool:
    """
    TOP innerhalb seiner Gruppe (gleicher Teil, gleiche Ebene) verschieben.

    Args:
        item: der zu verschiebende TOP
        direction: "up" oder "down"

    Returns:
        True, wenn verschoben wurde.
    """
    siblings = list(
        SessionAgendaItem.objects.filter(
            meeting=item.meeting,
            parent=item.parent,
            is_public=item.is_public,
        ).order_by("order", "created_at")
    )
    try:
        idx = [s.id for s in siblings].index(item.id)
    except ValueError:
        return False

    target_idx = idx - 1 if direction == "up" else idx + 1
    if target_idx < 0 or target_idx >= len(siblings):
        return False

    other = siblings[target_idx]
    item.order, other.order = other.order, item.order
    SessionAgendaItem.objects.bulk_update([item, other], ["order"])
    renumber_agenda(item.meeting)
    return True

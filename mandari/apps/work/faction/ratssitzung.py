# SPDX-License-Identifier: AGPL-3.0-or-later
"""
TOPs aus einer Ratssitzung übernehmen (Issue #1071, Muster „Aus Ratssitzung übernehmen …“).

Eine Fraktionssitzung bereitet meist eine Sitzung des Rats oder eines Ausschusses vor. Hier wählt die Verwaltung der
Tagesordnung eine anstehende Sitzung aus dem RIS der eigenen Kommunen und übernimmt deren Punkte als Fraktions-TOPs:
mit Bezug zum RIS-TOP (``related_agenda_item``) und der beratenen Vorlage als Unterlage (``related_papers``), wahlweise
als Unterpunkte eines vorhandenen TOPs (Muster: unter „Beschlüsse“).

Grenzen:

- Mandantentrennung: nur Sitzungen der Kommunen, mit denen die Organisation verknüpft ist (``get_all_bodies``), und
  nur Punkte genau dieser Sitzung.
- Nichtöffentliche Punkte des RIS sehen und übernehmen nur Vereidigte; sie werden nichtöffentliche TOPs.
- Keine Dubletten: Ein RIS-Punkt, der schon als TOP dieser Sitzung übernommen ist (auch als offener Vorschlag), wird
  nicht noch einmal angelegt, auch nicht bei gleichzeitigem Klick (Sperre der Sitzung wie beim Anlegen).

Lesen über die RIS-Lesefassade :mod:`hub.ris.selectors`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.utils import timezone
from django.utils.formats import date_format

from . import agenda
from .visibility import can_view_internal

if TYPE_CHECKING:
    from apps.tenants.models import Membership

    from .models import FactionAgendaItem, FactionMeeting

#: Wie viele anstehende Sitzungen die Auswahl anbietet
SITZUNGEN_HOECHSTENS = 40
#: Länge eines TOP-Titels (wie ``FactionAgendaItem.title``)
TITEL_LAENGE = 500


def _bodies(meeting: FactionMeeting) -> Any:
    from apps.work.meetings.selectors import organization_bodies

    return organization_bodies(meeting.organization)


def moeglich(meeting: FactionMeeting) -> bool:
    """Gibt es eine Kommune, aus deren RIS sich TOPs übernehmen lassen?"""
    return _bodies(meeting) is not None


@dataclass
class Punkt:
    """Ein Punkt der Ratssitzung in der Auswahl."""

    item: Any
    nummer: str
    titel: str
    vorlage: str
    intern: bool
    #: Nummer des TOPs, als der der Punkt schon übernommen ist (dann nicht wählbar)
    schon_als: str = ""


@dataclass
class Angebot:
    """Inhalt des Dialogs: Sitzungen zur Wahl, gewählte Sitzung, ihre Punkte und mögliche Eltern-TOPs."""

    sitzungen: list[dict[str, Any]] = field(default_factory=list)
    gewaehlt: Any = None
    punkte: list[Punkt] = field(default_factory=list)
    #: Nichtöffentliche Punkte, die die Person nicht sehen darf (nur die Zahl)
    gesperrt: int = 0
    eltern: list[Any] = field(default_factory=list)
    eltern_vorschlag: str = ""

    @property
    def waehlbar(self) -> int:
        return sum(1 for p in self.punkte if not p.schon_als)


def _sitzung_name(ris: Any) -> str:
    datum = date_format(timezone.localtime(ris.start), "D d.m.Y, H:i") if ris.start else ""
    return f"{ris.name or 'Sitzung'}, {datum}" if datum else (ris.name or "Sitzung")


def _verknuepfte_sitzung_id(meeting: FactionMeeting) -> Any:
    """Schon vorbereitete Ratssitzung: Feld der Sitzung oder die Sitzung eines verknüpften TOPs."""
    if meeting.related_meeting_id:
        return meeting.related_meeting_id
    from .models import FactionAgendaItem

    return (
        FactionAgendaItem.objects.filter(meeting=meeting, related_agenda_item__isnull=False)
        .values_list("related_agenda_item__meeting_id", flat=True)
        .first()
    )


def sitzungen(meeting: FactionMeeting) -> tuple[list[Any], Any]:
    """Anstehende Sitzungen der eigenen Kommunen (ab heute) und der Vorschlag für die Auswahl."""
    from hub.ris import selectors as ris

    bodies = _bodies(meeting)
    if bodies is None:
        return [], None
    heute = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    liste = list(ris.upcoming_meetings(bodies, since=heute)[:SITZUNGEN_HOECHSTENS])
    verknuepft = _verknuepfte_sitzung_id(meeting)
    if verknuepft and all(s.pk != verknuepft for s in liste):
        alt = ris.meeting(bodies, verknuepft)
        if alt is not None:
            liste.insert(0, alt)
    vorschlag = next((s for s in liste if s.pk == verknuepft), None)
    if vorschlag is None:
        # Die Fraktion bereitet meist die nächste Sitzung nach ihrem eigenen Termin vor
        vorschlag = next((s for s in liste if s.start and s.start >= meeting.start - timedelta(hours=1)), None)
    return liste, vorschlag or (liste[0] if liste else None)


def _schon_uebernommen(meeting: FactionMeeting) -> dict[Any, str]:
    """RIS-Punkt → Nummer des TOPs, als der er in dieser Sitzung steht (offene Vorschläge ohne Nummer)."""
    from .models import FactionAgendaItem

    zeilen = FactionAgendaItem.objects.filter(meeting=meeting, related_agenda_item__isnull=False).exclude(
        proposal_status="rejected"
    )
    return {rid: (nummer or "Vorschlag") for rid, nummer in zeilen.values_list("related_agenda_item_id", "number")}


def angebot(meeting: FactionMeeting, membership: Membership, ris_meeting_id: Any = None) -> Angebot:
    """Auswahl für den Dialog; ``ris_meeting_id`` wählt eine andere Sitzung der eigenen Kommunen."""
    from hub.ris import selectors as ris

    ergebnis = Angebot()
    liste, vorschlag = sitzungen(meeting)
    bodies = _bodies(meeting)
    gewaehlt = vorschlag
    if ris_meeting_id and bodies is not None:
        gewaehlt = ris.meeting(bodies, ris_meeting_id) or vorschlag
        if gewaehlt is not None and all(s.pk != gewaehlt.pk for s in liste):
            liste.insert(0, gewaehlt)
    ergebnis.sitzungen = [{"id": str(s.pk), "name": _sitzung_name(s)} for s in liste]
    ergebnis.gewaehlt = gewaehlt
    if gewaehlt is None:
        return ergebnis

    intern = can_view_internal(membership)
    punkte = ris.visible_agenda_items(gewaehlt)
    vorlagen = ris.papers_of_agenda_items(punkte)
    schon = _schon_uebernommen(meeting)
    for item in punkte:
        if not item.public and not intern:
            ergebnis.gesperrt += 1
            continue
        papiere = vorlagen.get(item.pk, [])
        vorlage = ", ".join(p.reference or p.name or "Vorlage" for p in papiere)
        ergebnis.punkte.append(
            Punkt(
                item=item,
                nummer=item.number or "",
                titel=item.name or f"TOP {item.number or ''}".strip(),
                vorlage=vorlage,
                intern=not item.public,
                schon_als=schon.get(item.pk, ""),
            )
        )

    ergebnis.eltern = list(agenda.numbered_items(meeting, "public").order_by("order"))
    beschluesse = next((e for e in ergebnis.eltern if "beschl" in (e.title or "").lower()), None)
    ergebnis.eltern_vorschlag = str(beschluesse.pk) if beschluesse else ""
    return ergebnis


class UebernahmeError(Exception):
    """Übernahme nicht möglich; die Meldung ist ein Satz für die Oberfläche."""


def uebernehmen(
    meeting: FactionMeeting, membership: Membership, ris_meeting_id: Any, punkt_ids: list[str], parent_id: str = ""
) -> list[FactionAgendaItem]:
    """
    Gewählte Punkte als TOPs anlegen und die angelegten TOPs liefern. Öffentliche Punkte kommen in den öffentlichen
    Teil (mit ``parent_id`` als Unterpunkte dieses TOPs), nichtöffentliche nur für Vereidigte in den
    nichtöffentlichen Teil. Schon übernommene Punkte werden übersprungen.
    """
    from hub.ris import selectors as ris

    from .models import FactionAgendaItem

    bodies = _bodies(meeting)
    if bodies is None:
        raise UebernahmeError("Ihre Organisation ist mit keiner Kommune verknüpft.")
    ris_meeting = ris.meeting(bodies, ris_meeting_id)
    if ris_meeting is None:
        raise UebernahmeError("Diese Ratssitzung gibt es im RIS Ihrer Kommune nicht.")
    gewuenscht = {str(pid) for pid in punkt_ids if pid}
    punkte = [p for p in ris.visible_agenda_items(ris_meeting) if str(p.pk) in gewuenscht]
    intern = can_view_internal(membership)
    if any(not p.public for p in punkte) and not intern:
        raise UebernahmeError("Nichtöffentliche Punkte können nur vereidigte Mitglieder übernehmen.")
    if not punkte:
        raise UebernahmeError("Wählen Sie mindestens einen Punkt aus.")
    vorlagen = ris.papers_of_agenda_items(punkte)

    angelegt: list[FactionAgendaItem] = []
    with transaction.atomic():
        agenda.lock_meeting(meeting)
        eltern = None
        if parent_id:
            eltern = FactionAgendaItem.objects.filter(
                pk=parent_id if _ist_uuid(parent_id) else None,
                meeting=meeting,
                parent__isnull=True,
                is_approval_item=False,
                proposal_status=agenda.ACTIVE,
                visibility="public",
            ).first()
            if eltern is None:
                raise UebernahmeError("Den gewählten TOP gibt es nicht mehr. Bitte laden Sie die Seite neu.")
        schon = _schon_uebernommen(meeting)
        for punkt in punkte:
            if punkt.pk in schon:
                continue
            sichtbarkeit = "public" if punkt.public else "internal"
            unter = eltern if sichtbarkeit == "public" else None
            if unter is not None:
                nummer = f"{unter.number}.{unter.children.count() + 1}"
            else:
                nummer = agenda.next_number(meeting, sichtbarkeit)
            titel = (punkt.name or f"TOP {punkt.number or ''}".strip())[:TITEL_LAENGE]
            item = FactionAgendaItem.objects.create(
                meeting=meeting,
                title=titel,
                number=nummer,
                visibility=sichtbarkeit,
                order=agenda.next_order(meeting),
                parent=unter,
                related_agenda_item=punkt,
            )
            papiere = vorlagen.get(punkt.pk, [])
            if papiere:
                item.related_papers.add(*papiere)
            angelegt.append(item)
            schon[punkt.pk] = nummer
        for sichtbarkeit in {i.visibility for i in angelegt}:
            agenda.renumber(meeting, sichtbarkeit)
        if angelegt and not meeting.related_meeting_id:
            meeting.related_meeting = ris_meeting
            meeting.save(update_fields=["related_meeting", "updated_at"])
    return angelegt


def _ist_uuid(wert: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(wert))
    except (TypeError, ValueError):
        return False
    return True

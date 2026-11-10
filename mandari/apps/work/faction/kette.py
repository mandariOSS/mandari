# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Protokollkette der Fraktionssitzungen (Issue #1295): Welche Sitzung genehmigt welches Protokoll?

Jede Sitzung kennt ihre vorherige Sitzung (``FactionMeeting.previous_meeting``); ihr automatischer erster TOP genehmigt
deren Protokoll. Die vorherige Sitzung ist der zeitlich direkt davor liegende Termin derselben Sitzungsart, der
stattfindet:

- abgesagte Termine zählen nicht, sie werden übersprungen und genehmigen selbst nichts;
- der Status spielt sonst keine Rolle. Die Reihe legt ihre Termine Wochen im Voraus an; die Sitzung davor ist dann
  noch nicht abgeschlossen. (Bis #1295 suchte Work beim Anlegen die zuletzt abgeschlossene Sitzung: Reihentermine
  blieben unverkettet oder genehmigten ein Protokoll erst Monate später.)

:func:`ordnen` zieht die Kette einer Sitzungsart nach. Geändert werden nur Sitzungen, die noch nicht begonnen haben;
begonnene und abgeschlossene behalten ihre vorherige Sitzung, denn ihr Genehmigungs-TOP ist so beraten worden. Der
Genehmigungs-TOP folgt der Kette: die genehmigte Sitzung immer, Titel und Beschreibung nur, solange niemand ihn
bearbeitet hat (wie nach dem Verschieben, Issue #1071). Ist die vorherige Sitzung abgeschlossen, steht ihr Protokoll
ab dann „zur Genehmigung“.

Ausgelöst wird das Ordnen durch das Speichern und Löschen einer Sitzung (``post_save`` bei neuer Sitzung, geändertem
Status, Beginn oder geänderter Art; ``post_delete``), damit Absagen, Verschieben, Wiederansetzen und Löschen die Kette
an keiner Stelle vergessen. Der stündliche Erzeugungslauf ordnet zusätzlich alle Arten mit kommenden Sitzungen
(:func:`alle_ordnen`): So kommt auch der Bestand von vor #1295 in Ordnung.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction

if TYPE_CHECKING:
    from .models import FactionMeeting

logger = logging.getLogger(__name__)

#: Begonnen oder abgeschlossen: Die vorherige Sitzung steht fest
FEST = ("ongoing", "completed")
#: Entfällt: nicht Teil der Kette
ENTFAELLT = "cancelled"
#: Felder einer Sitzung, die ihre Stelle in der Kette bestimmen
_KETTENFELDER = ("status", "start", "meeting_type_id")


def _soll(sitzungen: list[FactionMeeting], vergeben: set[Any]) -> dict[Any, Any]:
    """
    Vorherige Sitzung je Sitzung (nach Beginn geordnet). ``vergeben``: Sitzungen, die schon einer feststehenden oder
    fremden Sitzung als vorherige zugeordnet sind (die Zuordnung ist eindeutig, OneToOne).
    """
    soll: dict[Any, Any] = {}
    davor: FactionMeeting | None = None
    for sitzung in sitzungen:
        if sitzung.status == ENTFAELLT:
            soll[sitzung.pk] = None
            continue
        if sitzung.status in FEST:
            soll[sitzung.pk] = sitzung.previous_meeting_id
        else:
            ziel = davor.pk if davor is not None else None
            soll[sitzung.pk] = None if ziel in vergeben else ziel
        davor = sitzung
    return soll


def ordnen(organization_id: Any, meeting_type_id: Any) -> dict[Any, Any]:
    """
    Kette einer Sitzungsart nach dem Beginn ordnen. Liefert die geänderten Zuordnungen (Sitzung → vorherige Sitzung);
    leer, wenn alles stimmte.
    """
    from .models import FactionMeeting

    if organization_id is None or meeting_type_id is None:
        return {}
    with transaction.atomic():
        sitzungen = list(
            FactionMeeting.objects.select_for_update()
            .filter(organization_id=organization_id, meeting_type_id=meeting_type_id)
            .only("id", "start", "created_at", "status", "protocol_status", "previous_meeting_id")
            .order_by("start", "created_at", "pk")
        )
        soll = _soll(sitzungen, set())
        geaendert = [s for s in sitzungen if soll[s.pk] != s.previous_meeting_id]
        if geaendert:
            # Erst jetzt nachsehen, was schon vergeben ist: an feststehende Sitzungen und an Sitzungen anderer Arten
            beweglich = [s.pk for s in sitzungen if s.status not in FEST]
            vergeben = set(
                FactionMeeting.objects.filter(organization_id=organization_id, previous_meeting__isnull=False)
                .exclude(pk__in=beweglich)
                .values_list("previous_meeting_id", flat=True)
            )
            soll = _soll(sitzungen, vergeben)
            geaendert = [s for s in sitzungen if soll[s.pk] != s.previous_meeting_id]
        aenderungen = {s.pk: soll[s.pk] for s in geaendert}
        if aenderungen:
            _umhaengen(aenderungen)
        _zur_genehmigung(sitzungen, soll)
    return aenderungen


def _umhaengen(aenderungen: dict[Any, Any]) -> None:
    """Zuordnungen schreiben und die Genehmigungs-TOPs nachziehen."""
    from . import generation
    from .models import FactionMeeting

    sitzungen = list(
        FactionMeeting.objects.filter(pk__in=aenderungen).select_related(
            "organization", "meeting_type", "previous_meeting"
        )
    )
    # Vor dem Umhängen festhalten, ob der Genehmigungs-TOP noch so dasteht, wie Work ihn angelegt hat
    unbearbeitet = {s.pk: cast(Any, generation).approval_item_unchanged(s) for s in sitzungen}
    # Die Zuordnung ist eindeutig: erst alle lösen, dann neu setzen. Ohne save(): Die Kette ist abgeleitet und
    # gehört nicht in die Änderungshistorie der Sitzung; der geänderte TOP steht dort.
    FactionMeeting.objects.filter(pk__in=aenderungen).update(previous_meeting=None)
    for pk, ziel in aenderungen.items():
        if ziel is not None:
            FactionMeeting.objects.filter(pk=pk).update(previous_meeting_id=ziel)
    for sitzung in sitzungen:
        sitzung.previous_meeting_id = aenderungen[sitzung.pk]
        top = sitzung.agenda_items.filter(is_approval_item=True).first()
        if top is None:
            continue
        if unbearbeitet[sitzung.pk]:
            sitzung.create_approval_agenda_item()
        elif top.approves_meeting_id != sitzung.previous_meeting_id:
            top.approves_meeting_id = sitzung.previous_meeting_id
            top.save(update_fields=["approves_meeting", "updated_at"])


def _zur_genehmigung(sitzungen: list[FactionMeeting], soll: dict[Any, Any]) -> None:
    """Abgeschlossene Sitzung, deren Protokoll die folgende Sitzung genehmigt: Protokoll steht „zur Genehmigung“."""
    from .models import FactionAgendaItem, FactionMeeting

    nach_pk = {s.pk: s for s in sitzungen}
    wartend = [
        ziel
        for sitzung in sitzungen
        if sitzung.status not in FEST
        and (ziel := soll[sitzung.pk]) in nach_pk
        and nach_pk[ziel].status == "completed"
        and nach_pk[ziel].protocol_status == "draft"
    ]
    if not wartend:
        return
    mit_top = set(
        FactionAgendaItem.objects.filter(is_approval_item=True, approves_meeting__in=wartend).values_list(
            "approves_meeting_id", flat=True
        )
    )
    for vorherige in FactionMeeting.objects.filter(pk__in=mit_top, protocol_status="draft"):
        cast(Any, vorherige).submit_protocol_for_approval()


def alle_ordnen() -> int:
    """
    Alle Sitzungsarten mit Sitzungen ordnen, die noch nicht begonnen haben (stündlicher Erzeugungslauf). Liefert die
    Zahl der umgehängten Sitzungen.
    """
    from .models import FactionMeeting

    arten = (
        FactionMeeting.objects.filter(organization__is_active=True, meeting_type__isnull=False)
        .exclude(status__in=(*FEST, ENTFAELLT))
        .values_list("organization_id", "meeting_type_id")
        .distinct()
        .order_by()
    )
    geaendert = 0
    for organization_id, meeting_type_id in arten:
        try:
            geaendert += len(ordnen(organization_id, meeting_type_id))
        except Exception:
            logger.exception("Protokollkette konnte nicht geordnet werden (meeting_type=%s)", meeting_type_id)
    return geaendert


# =============================================================================
# Auslöser
# =============================================================================


def _ordnen_fuer(sitzung: FactionMeeting) -> None:
    """Kette der Art dieser Sitzung ordnen und die übergebene Sitzung im Speicher nachführen."""
    from .models import FactionMeeting

    # Nach einem Wechsel der Art hängt die Sitzung noch in der Kette der alten Art
    fremd = (
        FactionMeeting.objects.filter(previous_meeting_id=sitzung.pk)
        .exclude(meeting_type_id=sitzung.meeting_type_id)
        .values_list("organization_id", "meeting_type_id")
    )
    for organization_id, meeting_type_id in list(fremd):
        ordnen(organization_id, meeting_type_id)
    aenderungen = ordnen(sitzung.organization_id, sitzung.meeting_type_id)
    # Wer die Sitzung danach noch einmal speichert, schriebe sonst den alten Stand zurück
    if sitzung.pk in aenderungen:
        sitzung.previous_meeting_id = aenderungen[sitzung.pk]
    if sitzung.status == "completed" and sitzung.protocol_status == "draft":
        sitzung.protocol_status = FactionMeeting.objects.values_list("protocol_status", flat=True).get(pk=sitzung.pk)


def _gespeichert(
    sender: Any, instance: FactionMeeting, created: bool, update_fields: Any = None, raw: bool = False, **kwargs: Any
) -> None:
    if raw:
        return
    if not created:
        if update_fields is not None and not {"status", "start", "meeting_type", "meeting_type_id"} & set(
            update_fields
        ):
            return
        # Alter Stand aus der Änderungshistorie (``audit.audit_pre_save``): nur ordnen, wenn sich die Stelle ändert
        alt = getattr(instance, "_audit_old", None)
        if alt is not None and all(getattr(alt, feld) == getattr(instance, feld) for feld in _KETTENFELDER):
            return
    _ordnen_fuer(instance)


def _geloescht(sender: Any, instance: FactionMeeting, **kwargs: Any) -> None:
    ordnen(instance.organization_id, instance.meeting_type_id)


def register() -> None:
    from django.db.models.signals import post_delete, post_save

    from .models import FactionMeeting

    post_save.connect(_gespeichert, sender=FactionMeeting, dispatch_uid="faction_kette_gespeichert")
    post_delete.connect(_geloescht, sender=FactionMeeting, dispatch_uid="faction_kette_geloescht")

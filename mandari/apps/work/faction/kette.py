# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Protokollkette der Fraktionssitzungen (Issue #1295): Welche Sitzung genehmigt welches Protokoll?

Jede Sitzung kennt ihre vorherige Sitzung (``FactionMeeting.previous_meeting``); ihr automatischer erster TOP genehmigt
deren Protokoll. Die vorherige Sitzung ist der zeitlich direkt davor liegende Termin derselben Sitzungsart, der
stattfindet:

- Abgesagte Termine zählen nicht: Sie werden übersprungen und genehmigen selbst nichts.
- Ebenso Termine, die nie begonnen wurden, obwohl eine spätere Sitzung schon begonnen hat: Sie haben nicht
  stattgefunden, ein Protokoll gibt es nicht.
- Sonst spielt der Status keine Rolle. Die Reihe legt ihre Termine Wochen im Voraus an; die Sitzung davor ist dann
  noch nicht abgeschlossen. (Bis #1295 suchte Work beim Anlegen die zuletzt abgeschlossene Sitzung: Reihentermine
  blieben unverkettet oder genehmigten ein Protokoll erst Monate später.)

:func:`ordnen` zieht die Kette einer Sitzungsart nach. Abgeschlossene Sitzungen behalten ihre vorherige Sitzung, denn
ihr Genehmigungs-TOP ist so beraten worden. Der Genehmigungs-TOP folgt der Kette: die genehmigte Sitzung immer, Titel
und Beschreibung nur, solange niemand ihn bearbeitet hat (wie nach dem Verschieben, Issue #1071). Vor der Einladung
ist das abgeleiteter Stand ohne Eintrag in der Änderungshistorie (die Reihe hielte den Termin sonst für bearbeitet);
kennen die Eingeladenen den alten Titel schon, wird der TOP gespeichert wie von Hand geändert, und die Infoleiste
bietet die Aktualisierung an. Ist die vorherige Sitzung abgeschlossen, steht ihr Protokoll „zur Genehmigung“.

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

#: Begonnen oder abgeschlossen: Die Sitzung hat stattgefunden
BEGONNEN = ("ongoing", "completed")
#: Abgeschlossen: Die vorherige Sitzung steht fest
FEST = "completed"
#: Entfällt: nicht Teil der Kette
ENTFAELLT = "cancelled"
#: Felder einer Sitzung, die ihre Stelle in der Kette bestimmen
_KETTENFELDER = ("status", "start", "meeting_type_id")


def hat_stattgefunden(sitzung: FactionMeeting) -> bool:
    """Begonnen oder abgeschlossen: Nur dann gibt es ein Protokoll, das vorliegen oder offen sein kann."""
    return sitzung.status in BEGONNEN


def _soll(sitzungen: list[FactionMeeting], vergeben: set[Any]) -> dict[Any, Any]:
    """
    Vorherige Sitzung je Sitzung (nach Beginn geordnet). ``vergeben``: Sitzungen, die schon einer abgeschlossenen oder
    fremden Sitzung als vorherige zugeordnet sind (die Zuordnung ist eindeutig, OneToOne).
    """
    begonnen = [s.start for s in sitzungen if s.status in BEGONNEN]
    zuletzt_begonnen = max(begonnen) if begonnen else None
    soll: dict[Any, Any] = {}
    davor: FactionMeeting | None = None
    for sitzung in sitzungen:
        nie_begonnen = (
            sitzung.status not in BEGONNEN and zuletzt_begonnen is not None and sitzung.start < zuletzt_begonnen
        )
        if sitzung.status == ENTFAELLT or nie_begonnen:
            soll[sitzung.pk] = None
            continue
        if sitzung.status == FEST:
            soll[sitzung.pk] = sitzung.previous_meeting_id
        else:
            ziel = davor.pk if davor is not None else None
            soll[sitzung.pk] = None if ziel in vergeben else ziel
        davor = sitzung
    return soll


def _stand(organization_id: Any, meeting_type_id: Any) -> tuple[list[FactionMeeting], dict[Any, Any]]:
    """Sitzungen der Art nach Beginn und ihre vorherige Sitzung, wie sie sein soll."""
    from .models import FactionMeeting

    sitzungen = list(
        FactionMeeting.objects.filter(organization_id=organization_id, meeting_type_id=meeting_type_id)
        .only("id", "start", "created_at", "status", "protocol_status", "previous_meeting_id")
        .order_by("start", "created_at", "pk")
    )
    soll = _soll(sitzungen, set())
    if any(soll[s.pk] != s.previous_meeting_id for s in sitzungen):
        # Erst jetzt nachsehen, was vergeben ist: an abgeschlossene Sitzungen und an Sitzungen anderer Arten
        beweglich = [s.pk for s in sitzungen if s.status != FEST]
        vergeben = set(
            FactionMeeting.objects.filter(organization_id=organization_id, previous_meeting__isnull=False)
            .exclude(pk__in=beweglich)
            .values_list("previous_meeting_id", flat=True)
        )
        soll = _soll(sitzungen, vergeben)
    return sitzungen, soll


def ordnen(
    organization_id: Any, meeting_type_id: Any, *, unbearbeitet: dict[Any, bool] | None = None
) -> dict[Any, Any]:
    """
    Kette einer Sitzungsart nach dem Beginn ordnen. Liefert die geänderten Zuordnungen (Sitzung → vorherige Sitzung);
    leer, wenn alles stimmte. ``unbearbeitet``: vorab festgehaltener Stand des Genehmigungs-TOPs je Sitzung (beim
    Löschen ist die Zuordnung schon gelöst, der Vergleich mit den Einstellungen ginge sonst fehl).
    """
    from .models import FactionMeetingType

    if organization_id is None or meeting_type_id is None:
        return {}
    sitzungen, soll = _stand(organization_id, meeting_type_id)
    aenderungen: dict[Any, Any] = {}
    if any(soll[s.pk] != s.previous_meeting_id for s in sitzungen):
        with transaction.atomic():
            # Eine Neuordnung je Art zur Zeit (Sperre auf der Art, nicht auf allen Sitzungen); danach frisch lesen
            list(FactionMeetingType.objects.select_for_update().filter(pk=meeting_type_id).values_list("pk"))
            sitzungen, soll = _stand(organization_id, meeting_type_id)
            aenderungen = {s.pk: soll[s.pk] for s in sitzungen if soll[s.pk] != s.previous_meeting_id}
            if aenderungen:
                _umhaengen(aenderungen, unbearbeitet or {})
    _zur_genehmigung(sitzungen, soll)
    return aenderungen


def _umhaengen(aenderungen: dict[Any, Any], vorab: dict[Any, bool]) -> None:
    """Zuordnungen schreiben und die Genehmigungs-TOPs nachziehen."""
    from . import generation
    from .models import FactionMeeting

    sitzungen = list(
        FactionMeeting.objects.filter(pk__in=aenderungen).select_related(
            "organization", "meeting_type", "previous_meeting"
        )
    )
    # Vor dem Umhängen festhalten, ob der Genehmigungs-TOP noch so dasteht, wie Work ihn angelegt hat
    unbearbeitet = {
        s.pk: vorab[s.pk] if s.pk in vorab else cast(Any, generation).approval_item_unchanged(s) for s in sitzungen
    }
    # Die Zuordnung ist eindeutig: erst alle lösen, dann neu setzen. Ohne save(): Die Kette ist abgeleitet und
    # gehört nicht in die Änderungshistorie der Sitzung.
    FactionMeeting.objects.filter(pk__in=aenderungen).update(previous_meeting=None)
    for pk, ziel in aenderungen.items():
        if ziel is not None:
            FactionMeeting.objects.filter(pk=pk).update(previous_meeting_id=ziel)
    for sitzung in sitzungen:
        sitzung.previous_meeting_id = aenderungen[sitzung.pk]
        top_nachziehen(sitzung, unbearbeitet=unbearbeitet[sitzung.pk])


def top_nachziehen(sitzung: FactionMeeting, *, unbearbeitet: bool) -> None:
    """
    Genehmigungs-TOP an die vorherige Sitzung anpassen: die genehmigte Sitzung immer, Titel und Beschreibung nur beim
    unbearbeiteten TOP.
    """
    from .models import FactionAgendaItem

    top = sitzung.agenda_items.filter(is_approval_item=True).first()
    if top is None:
        return
    felder: dict[str, Any] = {}
    if top.approves_meeting_id != sitzung.previous_meeting_id:
        felder["approves_meeting_id"] = sitzung.previous_meeting_id
    if unbearbeitet:
        titel, beschreibung = sitzung.approval_item_texts()
        if top.title != titel or (cast(Any, top).get_description_decrypted() or "") != beschreibung:
            if sitzung.invitation_sent or sitzung.status == "invited":
                # Die Eingeladenen kennen den alten Titel: speichern wie von Hand geändert (Historie, „Tagesordnung
                # geändert“ in der Infoleiste)
                sitzung.create_approval_agenda_item()
                return
            top.title = titel
            cast(Any, top).set_description_encrypted(beschreibung)
            felder.update(title=top.title, description_encrypted=top.description_encrypted)
    if felder:
        FactionAgendaItem.objects.filter(pk=top.pk).update(**felder)


def _zur_genehmigung(sitzungen: list[FactionMeeting], soll: dict[Any, Any]) -> None:
    """Abgeschlossene Sitzung, deren Protokoll die folgende Sitzung genehmigt: Protokoll steht „zur Genehmigung“."""
    from .models import FactionAgendaItem, FactionMeeting

    nach_pk = {s.pk: s for s in sitzungen}
    wartend = [
        ziel
        for sitzung in sitzungen
        if sitzung.status != FEST
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
    Alle Sitzungsarten mit Sitzungen ordnen, die noch nicht abgeschlossen sind (stündlicher Erzeugungslauf). Liefert
    die Zahl der umgehängten Sitzungen.
    """
    from .models import FactionMeeting

    arten = (
        FactionMeeting.objects.filter(organization__is_active=True, meeting_type__isnull=False)
        .exclude(status__in=(FEST, ENTFAELLT))
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

    aenderungen = ordnen(sitzung.organization_id, sitzung.meeting_type_id)
    # Nach einem Wechsel der Art hängt die Sitzung noch in der Kette der alten Art; ihre alte vorherige Sitzung ist
    # jetzt frei
    fremd = (
        FactionMeeting.objects.filter(previous_meeting_id=sitzung.pk)
        .exclude(meeting_type_id=sitzung.meeting_type_id)
        .values_list("organization_id", "meeting_type_id")
    )
    for organization_id, meeting_type_id in list(fremd):
        ordnen(organization_id, meeting_type_id)
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


def _vor_dem_loeschen(sender: Any, instance: FactionMeeting, **kwargs: Any) -> None:
    """
    Stand des Genehmigungs-TOPs der folgenden Sitzung festhalten: Beim Löschen löst die Datenbank ihre Zuordnung,
    danach sähe jeder TOP bearbeitet aus und behielte „… und letztes Protokoll genehmigen“.
    """
    from . import generation
    from .audit import is_organization_deleting
    from .models import FactionMeeting

    cast(Any, instance)._kette_folgende = None
    if is_organization_deleting(instance.organization_id):
        return
    folgende = FactionMeeting.objects.filter(previous_meeting_id=instance.pk).exclude(status=FEST).first()
    if folgende is not None:
        cast(Any, instance)._kette_folgende = (folgende.pk, cast(Any, generation).approval_item_unchanged(folgende))


def _geloescht(sender: Any, instance: FactionMeeting, **kwargs: Any) -> None:
    from .models import FactionMeeting

    gemerkt = getattr(instance, "_kette_folgende", None)
    aenderungen = ordnen(
        instance.organization_id, instance.meeting_type_id, unbearbeitet=dict([gemerkt]) if gemerkt else None
    )
    if gemerkt and gemerkt[0] not in aenderungen:
        # Keine Sitzung davor: Die Zuordnung bleibt leer, der TOP verliert trotzdem seinen Zusatz
        folgende = FactionMeeting.objects.filter(pk=gemerkt[0]).select_related("organization", "meeting_type").first()
        if folgende is not None:
            top_nachziehen(folgende, unbearbeitet=gemerkt[1])


def register() -> None:
    from django.db.models.signals import post_delete, post_save, pre_delete

    from .models import FactionMeeting

    post_save.connect(_gespeichert, sender=FactionMeeting, dispatch_uid="faction_kette_gespeichert")
    pre_delete.connect(_vor_dem_loeschen, sender=FactionMeeting, dispatch_uid="faction_kette_vor_dem_loeschen")
    post_delete.connect(_geloescht, sender=FactionMeeting, dispatch_uid="faction_kette_geloescht")

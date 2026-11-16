# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsansicht der laufenden Fraktionssitzung (Issue #874).

Fachlogik der neuen Ansicht: Schalter je Organisation, Rechte (Schriftführung), Tagesordnung mit Nachbarn,
Unterlagen eines TOPs, „Im Beratungsverlauf“ aus der Sitzungsvorbereitung, Notizen je TOP und Aufgaben für
Personen und Personengruppen. Views und Templates rufen nur hier hinein.

Grenzen wie in der bisherigen Ansicht:

- Nichtöffentliche TOPs (auch als Unterpunkt) sehen nur Vereidigte (:mod:`apps.work.faction.visibility`).
  Für alle anderen enthält die Tagesordnung keine nichtöffentlichen Objekte, nur deren Anzahl.
- Bestehende Protokolleinträge (Notiz, Wortbeitrag, Beschluss, Aufgabe, Abstimmung, Nachtrag) bleiben unverändert
  und lesbar; neue Wortbeiträge legt die neue Ansicht nicht an (Entscheidung vom 05.10.2026).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.work.rahmen import neues_design
from apps.work.sanitize import sanitize_editor_html

from .visibility import can_view_internal, can_view_item_with_parents, is_item_internal, is_sworn_member

if TYPE_CHECKING:
    from apps.tenants.models import Membership, Organization
    from apps.work.tasks.models import Task

    from .models import FactionAgendaItem, FactionMeeting

logger = logging.getLogger(__name__)

#: Notizen höchstens so lang (Zeichen HTML) – schützt vor versehentlich eingefügten Riesentexten
NOTIZEN_MAX_ZEICHEN = 200_000
#: Änderungshistorie: höchstens ein Eintrag je TOP und Zeitraum für die Notizen (kein Eintrag je Tastendruck)
NOTIZEN_HISTORIE_ABSTAND = timedelta(minutes=10)
#: Aufgabentitel wie ``Task.title``
AUFGABE_TITEL_MAX = 200

#: Punktfarbe je Position aus der Sitzungsvorbereitung (AgendaItemPosition.POSITION_CHOICES)
POSITION_PUNKT = {
    "for": "zustimmung",
    "against": "ablehnung",
    "abstain": "enthaltung",
    "open": "offen",
}


# =============================================================================
# Schalter und Rechte
# =============================================================================


def zeigt_sitzungsansicht(meeting: FactionMeeting, ansicht: str | None) -> bool:
    """
    Neue Sitzungsansicht statt der bisherigen? Nur für laufende Sitzungen und nur, wenn die Organisation das neue
    Erscheinungsbild eingeschaltet hat (Schalter der Spur „Rahmen“, Issue #852); ``?ansicht=bisher`` führt zurück.
    """
    return meeting.status == "ongoing" and ansicht != "bisher" and neues_design(meeting.organization)


def ist_schriftfuehrung(membership: Membership, meeting: FactionMeeting) -> bool:
    return meeting.minute_taker_id is not None and meeting.minute_taker_id == membership.pk


def darf_verwalten(membership: Membership, meeting: FactionMeeting) -> bool:
    """Ersteller der Sitzung oder ``faction.manage`` (wie ``can_edit`` der bisherigen Ansicht, ohne Status)."""
    return meeting.created_by_id == membership.pk or membership.has_permission("faction.manage")


def darf_protokollieren(membership: Membership, meeting: FactionMeeting) -> bool:
    """
    Notizen, Aufgaben und Beschluss erfassen: wer bisher protokollieren durfte, dazu die Schriftführung.

    Nur während und nach der Sitzung, solange das Protokoll nicht genehmigt ist (wie ``can_protocol``).
    """
    if meeting.protocol_approved or meeting.status not in ("ongoing", "completed"):
        return False
    return (
        darf_verwalten(membership, meeting)
        or membership.has_permission("protocols.create")
        or membership.has_permission("protocols.edit")
        or ist_schriftfuehrung(membership, meeting)
    )


def darf_anwesenheit_pflegen(membership: Membership, meeting: FactionMeeting) -> bool:
    """Anwesenheit (vor Ort, online, entschuldigt): ``faction.manage`` wie bisher, dazu die Schriftführung."""
    if meeting.attendance_confirmed_at is not None or meeting.status != "ongoing":
        return False
    return membership.has_permission("faction.manage") or ist_schriftfuehrung(membership, meeting)


def darf_teilnahme_nachtragen(membership: Membership, meeting: FactionMeeting) -> bool:
    """
    Mitglieder ohne Einladung in die Anwesenheit aufnehmen (Issue #1436): solange die Anwesenheit gepflegt wird, mit
    ``faction.manage`` wie in der bisherigen Ansicht. Eine Teilnahme macht die Sitzung für das Mitglied sichtbar
    (:func:`apps.work.faction.sitzungsarten.darf_sehen`); die Schriftführung allein vergibt das nicht.
    """
    return darf_anwesenheit_pflegen(membership, meeting) and membership.has_permission("faction.manage")


def darf_rollen_setzen(membership: Membership, meeting: FactionMeeting) -> bool:
    """Sitzungsleitung und Schriftführung festlegen: wer die Sitzung verwaltet."""
    return meeting.status in ("planned", "invited", "ongoing") and darf_verwalten(membership, meeting)


def darf_beratungsverlauf_sehen(membership: Membership) -> bool:
    """„Im Beratungsverlauf“ zeigt Positionen aus der Sitzungsvorbereitung: nur mit deren Recht (``meetings.prepare``)."""
    return membership.has_permission("meetings.prepare")


def darf_unterlagen_anhaengen(membership: Membership, meeting: FactionMeeting) -> bool:
    """Unterlagen in der laufenden Sitzung anhängen (Tischvorlage): wie bisher ``can_edit``, dazu die Schriftführung."""
    if meeting.status not in ("draft", "planned", "invited", "ongoing"):
        return False
    return darf_verwalten(membership, meeting) or (
        meeting.status == "ongoing" and ist_schriftfuehrung(membership, meeting)
    )


def darf_top_hinzufuegen(membership: Membership, meeting: FactionMeeting) -> bool:
    """
    TOP in der laufenden Sitzung ergänzen (Issue #1166), etwa wenn unter TOP 1 eine geänderte Tagesordnung beschlossen
    wird: dieselben Rechte wie „TOP eintragen“ in der Vorbereitung (``can_add_items``), also wer die Sitzung verwaltet
    oder ``agenda.create`` hat, dazu die Schriftführung der Sitzung, die eine geänderte Tagesordnung allein nachtragen
    kann. Nichtöffentliches nur für Vereidigte (prüft :func:`top_hinzufuegen`). Nur solange die Sitzung läuft: Nach
    dem Beenden bietet auch die Vorbereitung das Eintragen nicht mehr an, die Tagesordnung gehört dann zum Protokoll.
    """
    if meeting.status != "ongoing" or meeting.protocol_approved:
        return False
    return (
        darf_verwalten(membership, meeting)
        or membership.has_permission("agenda.create")
        or ist_schriftfuehrung(membership, meeting)
    )


def darf_top_verschieben(membership: Membership, meeting: FactionMeeting) -> bool:
    """
    TOP in der laufenden Sitzung verschieben (Issue #1436), etwa wenn sich zeigt, dass ein Punkt in den
    nichtöffentlichen Teil gehört: wer die Tagesordnung verwaltet (wie „Verschieben“ in der Vorbereitung,
    ``agenda.manages``), dazu die Schriftführung der Sitzung, die eine geänderte Tagesordnung nachträgt. Wer nur
    ``agenda.create`` hat, trägt TOPs ein, ordnet sie aber nicht um. Nur solange die Sitzung läuft und das Protokoll
    nicht genehmigt ist.
    """
    from . import agenda

    if meeting.status != "ongoing" or meeting.protocol_approved:
        return False
    return agenda.manages(meeting, membership) or ist_schriftfuehrung(membership, meeting)


# =============================================================================
# Tagesordnung
# =============================================================================


@dataclass
class TopZeile:
    """Ein TOP der Tagesordnung in der Sitzungsansicht."""

    item: FactionAgendaItem
    unter: bool
    intern: bool
    besprochen: bool


@dataclass
class Tagesordnung:
    oeffentlich: list[TopZeile]
    intern: list[TopZeile]
    #: nichtöffentliche TOPs, die das Mitglied nicht sehen darf (nur die Zahl)
    gesperrt: int

    @property
    def zeilen(self) -> list[TopZeile]:
        return [*self.oeffentlich, *self.intern]

    @property
    def eltern_oeffentlich(self) -> list[TopZeile]:
        """TOPs des öffentlichen Teils, unter die ein neuer Unterpunkt kann (wie in der Vorbereitung, Issue #1166)."""
        return _eltern(self.oeffentlich)

    @property
    def eltern_intern(self) -> list[TopZeile]:
        return _eltern(self.intern)

    def finde(self, item_id: Any) -> TopZeile | None:
        key = str(item_id)
        return next((z for z in self.zeilen if str(z.item.pk) == key), None)

    def nachbarn(self, item_id: Any) -> tuple[TopZeile | None, TopZeile | None]:
        zeilen = self.zeilen
        ids = [str(z.item.pk) for z in zeilen]
        if str(item_id) not in ids:
            return None, None
        i = ids.index(str(item_id))
        return (zeilen[i - 1] if i > 0 else None), (zeilen[i + 1] if i + 1 < len(zeilen) else None)


def _eltern(zeilen: list[TopZeile]) -> list[TopZeile]:
    """TOPs der obersten Ebene ohne den Genehmigungs-TOP (Auswahl „Als Unterpunkt von“)."""
    return [z for z in zeilen if not z.unter and not z.item.is_approval_item]


def _besprochen(item: FactionAgendaItem) -> bool:
    decision_present = item.recorded_decision is not None
    return bool(decision_present or item.notes_encrypted or getattr(item, "eintraege", 0))


def tagesordnung(meeting: FactionMeeting, membership: Membership) -> Tagesordnung:
    """Angenommene TOPs: öffentlicher Teil, dann nichtöffentlicher Teil; Unterpunkte direkt unter ihrem TOP."""
    from .models import FactionAgendaItem

    darf_intern = can_view_internal(membership)
    alle = list(
        FactionAgendaItem.objects.filter(meeting=meeting, proposal_status="active")
        .select_related("decision", "parent", "meeting__organization", "approves_meeting", "proposed_by__user")
        .annotate(eintraege=Count("protocol_entries"))
        .order_by("order", "number")
    )
    kinder: dict[Any, list[FactionAgendaItem]] = {}
    for item in alle:
        if item.parent_id is not None:
            kinder.setdefault(item.parent_id, []).append(item)

    oeffentlich: list[TopZeile] = []
    intern: list[TopZeile] = []
    gesperrt = 0
    for item in alle:
        if item.parent_id is not None:
            continue
        ziel = intern if item.visibility == "internal" else oeffentlich
        for eintrag in [item, *kinder.get(item.pk, [])]:
            nicht_oeffentlich = is_item_internal(eintrag)
            if nicht_oeffentlich and not darf_intern:
                gesperrt += 1
                continue
            ziel.append(
                TopZeile(
                    item=eintrag,
                    unter=eintrag.parent_id is not None,
                    intern=nicht_oeffentlich,
                    besprochen=_besprochen(eintrag),
                )
            )
    return Tagesordnung(oeffentlich=oeffentlich, intern=intern, gesperrt=gesperrt)


def aktueller_top(ordnung: Tagesordnung, gewuenscht: Any = None) -> TopZeile | None:
    """Gewünschter TOP (aus der Adresse), sonst der erste noch nicht besprochene, sonst der erste."""
    if gewuenscht:
        zeile = ordnung.finde(gewuenscht)
        if zeile is not None:
            return zeile
    zeilen = ordnung.zeilen
    return next((z for z in zeilen if not z.besprochen), zeilen[0] if zeilen else None)


# =============================================================================
# Unterlagen
# =============================================================================


@dataclass
class Unterlage:
    """
    Ein Reiter unter „Unterlagen“: Protokoll zur Genehmigung, RIS-Datei, RIS-Vorlage ohne Datei, eigenes Dokument,
    Anlage oder Links.
    """

    schluessel: str
    art: str
    titel: str
    objekt: Any = None
    vorlage: Any = None
    #: Protokoll zur Genehmigung: Fassung der Niederschrift („intern“ oder „oeffentlich“), die das Mitglied sehen darf
    fassung: str = ""
    #: Protokoll zur Genehmigung: darf das Mitglied die öffentliche Fassung lesen (``protocols.view_public``)? Ohne
    #: dieses Recht bleibt die Vorschau bei verdecktem Sichtschutz leer, statt die vollständige Fassung zu zeigen.
    oeffentlich_lesbar: bool = True


def vorige_mit_protokoll(item: FactionAgendaItem, membership: Membership | None = None) -> FactionMeeting | None:
    """
    Sitzung, deren Protokoll dieser Genehmigungs-TOP vorliegen hat. Nur wenn sie stattgefunden hat: Die Protokollkette
    reicht in die Zukunft (Issue #1295), vor der Sitzung davor gibt es kein Protokoll zu zeigen.

    Mit ``membership`` nur, wenn das Mitglied die vorige Sitzung sehen darf (Sitzungsarten, Issue #1239): Wer einzeln
    zu dieser Sitzung eingeladen ist, gehört nicht zwingend zu den Teilnehmenden der Art.
    """
    from .kette import hat_stattgefunden
    from .sitzungsarten import darf_sehen

    if not item.is_approval_item or not item.approves_meeting_id:
        return None
    vorige = item.approves_meeting
    if vorige is None or not hat_stattgefunden(vorige):
        return None
    if membership is not None and not darf_sehen(membership, vorige):
        return None
    return vorige


def protokoll_fassung(membership: Membership) -> str:
    """Fassung der Niederschrift, die das Mitglied lesen darf (wie der PDF-Export); leer ohne Recht."""
    if can_view_internal(membership) and membership.has_permission("protocols.view_full"):
        return "intern"
    if membership.has_permission("protocols.view_public"):
        return "oeffentlich"
    return ""


def fuehrt_protokoll(membership: Membership, meeting: FactionMeeting) -> bool:
    """Wer das Protokoll der Sitzung führt, unabhängig von ihrem Stand: wie :func:`darf_protokollieren` ohne Phase."""
    return (
        darf_verwalten(membership, meeting)
        or membership.has_permission("protocols.create")
        or membership.has_permission("protocols.edit")
        or ist_schriftfuehrung(membership, meeting)
    )


def darf_eintraege_lesen(membership: Membership, meeting: FactionMeeting, *, intern: bool) -> bool:
    """
    Protokolleinträge eines TOPs lesen: mit denselben Rechten wie die Niederschrift als PDF und der Protokollversand
    (:func:`protokoll_fassung`), also ``protocols.view_public`` für öffentliche TOPs und Vereidigung mit
    ``protocols.view_full`` für nichtöffentliche (``intern``). Ein in der Rollenverwaltung entzogenes Recht schließt
    so auch die Seiten der Sitzung, nicht nur das PDF. Wer das Protokoll führt, liest, was er schreibt. Ob das
    Mitglied den TOP überhaupt sieht, prüft der Aufrufer wie bisher.
    """
    if fuehrt_protokoll(membership, meeting):
        return True
    fassung = protokoll_fassung(membership)
    return fassung == "intern" if intern else bool(fassung)


def lesbare_eintraege(membership: Membership, meeting: FactionMeeting) -> Any:
    """
    Protokolleinträge der Sitzung, die das Mitglied lesen darf (:func:`darf_eintraege_lesen`), als Abfrage für Listen,
    die die Einträge je TOP vorab laden. Einträge ohne TOP gelten wie überall als intern.
    """
    from django.db.models import Q

    from .models import FactionProtocolEntry

    qs = FactionProtocolEntry.objects.filter(meeting=meeting).select_related("speaker__user", "action_assignee__user")
    if darf_eintraege_lesen(membership, meeting, intern=True):
        return qs
    if not darf_eintraege_lesen(membership, meeting, intern=False):
        return qs.none()
    return qs.filter(agenda_item__isnull=False).exclude(
        Q(agenda_item__visibility="internal") | Q(agenda_item__parent__visibility="internal")
    )


def _haupt_datei_kennung(paper: Any) -> str:
    raw = paper.raw_json if isinstance(getattr(paper, "raw_json", None), dict) else {}
    main = raw.get("mainFile")
    if isinstance(main, dict):
        return str(main.get("id") or "")
    return str(main or "")


def unterlagen(item: FactionAgendaItem, membership: Membership) -> list[Unterlage]:
    """
    Unterlagen eines TOPs in der Reihenfolge der Reiter: beim Genehmigungs-TOP zuerst das Protokoll der vorigen
    Sitzung (Niederschrift in der Fassung, die das Mitglied lesen darf), dann Vorlagen, eigene Dokumente, Anlagen, Links.
    """
    from hub.ris import selectors as ris

    from . import services as faction_services

    liste: list[Unterlage] = []
    vorige = vorige_mit_protokoll(item, membership)
    fassung = protokoll_fassung(membership) if vorige is not None else ""
    if vorige is not None and fassung:
        protokoll = f"Protokoll vom {timezone.localtime(vorige.start):%d.%m.%Y}" if vorige.start else "Protokoll"
        oeffentlich = fassung == "oeffentlich" or membership.has_permission("protocols.view_public")
        liste.append(
            Unterlage(f"p-{vorige.pk}", "protokoll", protokoll, vorige, fassung=fassung, oeffentlich_lesbar=oeffentlich)
        )
    for paper in item.related_papers.all().order_by("-date"):
        dateien = list(ris.files_of_paper(paper).defer("text_content", "raw_json"))
        haupt = _haupt_datei_kennung(paper)
        dateien.sort(key=lambda f: (0 if haupt and f.external_id == haupt else 1, (f.name or f.file_name or "")))
        if not dateien:
            liste.append(Unterlage(f"v-{paper.pk}", "ris_vorlage", paper.reference or paper.name or "Vorlage", paper))
            continue
        for datei in dateien:
            # Die Hauptdatei (oder die einzige Datei) trägt den Namen der Vorlage, alle anderen ihren Dateinamen
            ist_haupt = len(dateien) == 1 or bool(haupt and datei.external_id == haupt)
            titel = (paper.reference or paper.name or "Vorlage") if ist_haupt else (datei.name or datei.file_name)
            liste.append(Unterlage(f"r-{datei.pk}", "ris_datei", titel or "Datei", datei, paper))
    for motion in cast(Any, faction_services).visible_linked_motions(item, membership).order_by("title"):
        liste.append(Unterlage(f"d-{motion.pk}", "dokument", motion.title or "Dokument", motion))
    for anlage in item.attachments.order_by("created_at"):
        liste.append(Unterlage(f"a-{anlage.pk}", "anlage", anlage.filename or "Anlage", anlage))
    links = [
        link
        for link in (item.reference_links or [])
        if isinstance(link, dict) and cast(Any, faction_services).safe_link_url(str(link.get("url") or ""))
    ]
    if links:
        liste.append(Unterlage("links", "links", "Links", links))
    return liste


def unterlage_finden(item: FactionAgendaItem, membership: Membership, schluessel: str) -> Unterlage | None:
    return next((u for u in unterlagen(item, membership) if u.schluessel == schluessel), None)


# =============================================================================
# Im Beratungsverlauf (Übergabe aus der Sitzungsvorbereitung)
# =============================================================================


@dataclass
class VerlaufEintrag:
    wo: str
    position: str
    punkt: str
    ergebnis: str = ""
    endgueltig: bool = False
    begruendung: str = ""
    meeting_id: str | None = None
    ziel: bool = False


def _gremium_und_datum(agenda_item: Any) -> tuple[str, str, str | None]:
    meeting = getattr(agenda_item, "meeting", None)
    if meeting is None:
        return "", "", None
    namen = ", ".join(o.short_name or o.name or "" for o in meeting.organizations.all() if (o.short_name or o.name))
    datum = timezone.localtime(meeting.start).strftime("%d.%m.%Y") if meeting.start else ""
    return namen or (meeting.name or ""), datum, str(meeting.pk)


def herkunft(item: FactionAgendaItem, membership: Membership | None = None) -> str:
    """
    Zweite Zeile im Kopf des TOPs: übergeordneter TOP, vorbereiteter Gremien-TOP, Standard-TOP. Beim Genehmigungs-TOP
    nennt sie die vorige Sitzung; mit ``membership`` Titel und Datum nur, wenn das Mitglied sie sehen darf.
    """
    teile: list[str] = []
    if item.parent_id and item.parent is not None:
        teile.append(f"Unter {item.parent.title}")
    ziel = item.related_agenda_item if item.related_agenda_item_id else None
    if ziel is not None:
        gremium, datum, _meeting_id = _gremium_und_datum(ziel)
        nummer = f" TOP {ziel.number}" if getattr(ziel, "number", None) else ""
        tag = f" am {datum[:6]}" if datum else ""
        text = f"{gremium or 'Gremium'}{nummer}{tag}"
        teile.append(("im " if teile else "Im ") + text)
    if item.standard_item_id:
        teile.append("Standard-TOP")
    text = ", ".join(teile)
    # Das Datum endet schon mit einem Punkt („am 19.10.“): kein zweiter Punkt am Satzende
    text = text if not text or text.endswith(".") else f"{text}."
    # Genehmigungs-TOP: welches Protokoll vorliegt (wie „Genehmigt: …“ in der bisherigen Ansicht)
    vorige = vorige_mit_protokoll(item)
    if vorige is not None:
        if membership is not None and vorige_mit_protokoll(item, membership) is None:
            satz = "Das Protokoll der vorigen Sitzung liegt zur Genehmigung vor."
        else:
            datum = f" vom {timezone.localtime(vorige.start):%d.%m.%Y}" if vorige.start else ""
            satz = f"Das Protokoll von „{vorige.title}“{datum} liegt zur Genehmigung vor."
        text = f"{text} {satz}" if text else satz
    return text


def _ziel_tagesordnungspunkt(item: FactionAgendaItem) -> Any:
    """Rats-TOP, den der Fraktions-TOP vorbereitet: verknüpfter TOP, sonst die maßgebliche Beratung der Vorlage."""
    if item.related_agenda_item_id:
        return item.related_agenda_item
    from hub.ris import selectors as ris

    for paper in item.related_papers.all().order_by("-date")[:3]:
        beratungen = list(
            ris.consultations_of_paper(paper)
            .exclude(agenda_item_external_id__isnull=True)
            .filter(ris.consultation_agenda_item_in_source())
        )
        beratungen = [b for b in beratungen if b.agenda_item_external_id]
        if not beratungen:
            continue
        massgeblich = next((b for b in beratungen if b.authoritative), beratungen[-1])
        treffer = ris.agenda_items_by_external_id([str(massgeblich.agenda_item_external_id)]).first()
        if treffer is not None:
            return treffer
    return None


def beratungsverlauf(item: FactionAgendaItem, organization: Organization) -> list[VerlaufEintrag]:
    """
    Positionen der Fraktion aus anderen Gremien zur selben Vorlage, dazu die Position für das Zielgremium.

    Quelle sind die Daten der Sitzungsvorbereitung (``AgendaItemPosition.get_cross_positions_for_items``); die
    Organisation bleibt die Grenze. Leer, wenn der TOP keine Vorlage betrifft.
    """
    from apps.work.meetings import selectors as vorbereitung

    ziel = _ziel_tagesordnungspunkt(item)
    if ziel is None:
        return []
    eintraege: list[VerlaufEintrag] = []
    for cp in vorbereitung.cross_positions(organization, [ziel]).get(ziel.pk, []):
        wo = ", ".join(x for x in (cp.get("gremium") or cp.get("sitzung") or "", cp.get("datum_display") or "") if x)
        eintraege.append(
            VerlaufEintrag(
                wo=wo,
                position=cp.get("position_display") or "",
                punkt=POSITION_PUNKT.get(cp.get("position") or "", "andere"),
                ergebnis=cp.get("outcome_display") or "",
                endgueltig=bool(cp.get("is_final")),
                begruendung=cp.get("reasoning") or "",
                meeting_id=cp.get("meeting_id"),
            )
        )
    eigene = vorbereitung.get_position(organization, ziel)
    if eigene is not None and (eigene.position != "open" or eigene.outcome or eigene.reasoning_encrypted):
        gremium, datum, meeting_id = _gremium_und_datum(ziel)
        wo = f"Für {gremium or 'das Gremium'}{' am ' + datum if datum else ''}, aus der Vorbereitung"
        eintraege.append(
            VerlaufEintrag(
                wo=wo,
                position=eigene.get_position_display(),
                punkt=POSITION_PUNKT.get(eigene.position, "andere"),
                ergebnis=eigene.get_outcome_display() if eigene.outcome else "",
                endgueltig=eigene.is_final,
                begruendung=cast(Any, eigene).get_reasoning_decrypted() or "",
                meeting_id=meeting_id,
                ziel=True,
            )
        )
    return eintraege


# =============================================================================
# Notizen je TOP
# =============================================================================


def fingerabdruck(html: str | None) -> str:
    """Kurzer Fingerabdruck des gespeicherten Stands für die Konflikterkennung."""
    return hashlib.sha256((html or "").encode("utf-8")).hexdigest()[:16]


def notizen(item: FactionAgendaItem) -> str:
    return str(cast(Any, item).get_notes_decrypted() or "")


@dataclass
class NotizErgebnis:
    gespeichert: bool
    html: str
    stand: str
    geaendert_um: Any = None


def _ist_leer(html: str) -> bool:
    """
    Kein Text außerhalb von Marken und keine Liste.

    Ein Durchlauf von vorn nach hinten statt eines regulären Ausdrucks: Der Text kommt ungeprüft vom Browser, und
    ein Muster, das von jeder öffnenden Klammer bis zum Ende sucht, braucht bei vielen Klammern quadratische Zeit.
    Eine Klammer ohne Abschluss und ein leeres Klammerpaar zählen wie bisher als Text.
    """
    html = html or ""
    if "<li" in html:
        return False
    position = 0
    while True:
        start = html.find("<", position)
        if start == -1:
            return not html[position:].strip()
        if html[position:start].strip():
            return False
        ende = html.find(">", start + 1)
        if ende == -1 or ende == start + 1:
            return False
        position = ende + 1


def notizen_leer(html: str) -> bool:
    """Notizen ohne Text und ohne Liste (der Editor legt leer ``<p></p>`` an)."""
    return _ist_leer(html)


def notizen_speichern(
    item: FactionAgendaItem,
    html: str,
    *,
    basis: str | None,
    membership: Membership,
    yjs: bytes | None = None,
) -> NotizErgebnis:
    """
    Notizen eines TOPs speichern, nur wenn niemand inzwischen gespeichert hat (``basis`` = Stand beim Laden).

    Weicht der gespeicherte Stand ab, bleibt er unverändert; zurück kommt der aktuelle Stand, damit der Browser
    beide Fassungen zusammenführen kann (keine stillen Verluste). Gleicht der Text dem gespeicherten, gilt er als
    gespeichert, egal welcher Stand mitkam (Issue #1166). Gespeichert wird nur die Positivliste des Editors.
    Die Änderungshistorie erhält höchstens alle zehn Minuten je TOP einen Eintrag (ohne Inhalt).

    Live-Mitschreiben (``yjs`` = Zustand des gemeinsamen Editors, :mod:`apps.work.faction.live`): Maßgeblich ist der
    Stand, zu dem der gespeicherte Zustand gehört; gibt es keinen, der Stand beim Verbinden (``basis``). Der Zustand
    wird mit den Notizen gespeichert. Speichert jemand ohne Verbindung, entfällt der Zustand; verbundene Editoren
    bauen ihn danach aus den neuen Notizen auf (``live.neu_aufbauen``).

    Neu erwähnte Personen bekommen danach eine Benachrichtigung (:mod:`apps.work.faction.erwaehnungen`).
    """
    from . import audit, erwaehnungen, live
    from .models import FactionAgendaItem, FactionNotesState

    sauber = "" if _ist_leer(html) else sanitize_editor_html(html)
    zustand_verworfen = False
    with transaction.atomic():
        gesperrt = FactionAgendaItem.objects.select_for_update().select_related("meeting__organization").get(pk=item.pk)
        zustand = FactionNotesState.objects.select_for_update().filter(item=gesperrt).first()
        aktuell = str(cast(Any, gesperrt).get_notes_decrypted() or "")
        gleich = sauber == aktuell
        erwartet = zustand.stand if yjs is not None and zustand is not None else basis
        # Gleicher Text ist nie ein Konflikt, auch mit veraltetem Stand (zwei Speicherungen kurz hintereinander,
        # Rückfall vom WebSocket auf das Formular): Sonst stünde derselbe Text doppelt in den Notizen (Issue #1166)
        if not gleich and erwartet is not None and erwartet != fingerabdruck(aktuell):
            return NotizErgebnis(False, aktuell, fingerabdruck(aktuell), gesperrt.notes_updated_at)
        if yjs is not None:
            if zustand is None:
                zustand = FactionNotesState(item=gesperrt)
            zustand.item = gesperrt
            zustand.set_state(yjs)
            zustand.stand = fingerabdruck(sauber)
            zustand.save()
        elif zustand is not None and not gleich:
            # Ohne Verbindung gespeichert: Der Zustand passt nicht mehr zu den Notizen
            zustand.delete()
            zustand_verworfen = True
        if gleich:
            return NotizErgebnis(True, aktuell, fingerabdruck(aktuell), gesperrt.notes_updated_at)
        cast(Any, gesperrt).set_notes_encrypted(sauber)
        jetzt = timezone.now()
        # Ohne save(): sonst entstünde je automatischer Speicherung ein Eintrag in der Änderungshistorie
        FactionAgendaItem.objects.filter(pk=item.pk).update(
            notes_encrypted=gesperrt.notes_encrypted, notes_updated_at=jetzt, updated_at=jetzt
        )
        if zustand_verworfen:
            transaction.on_commit(lambda: live.neu_aufbauen(gesperrt.pk, sauber, vorher=aktuell))
    if _historie_faellig(item):
        audit.log_event(
            "update",
            gesperrt,
            membership=membership,
            changes={"notes_encrypted": {"alt": "[verschlüsselt geändert]", "neu": "[verschlüsselt geändert]"}},
        )
    erwaehnungen.benachrichtigen(gesperrt, sauber, von=membership, vorher=aktuell)
    return NotizErgebnis(True, sauber, fingerabdruck(sauber), jetzt)


def _historie_faellig(item: FactionAgendaItem) -> bool:
    from .models import FactionAuditLog

    seit = timezone.now() - NOTIZEN_HISTORIE_ABSTAND
    letzte = FactionAuditLog.objects.filter(
        model_name="FactionAgendaItem", object_id=item.pk, action="update", created_at__gte=seit
    ).values_list("changes", flat=True)
    return not any(isinstance(c, dict) and "notes_encrypted" in c for c in letzte)


# =============================================================================
# Aufgaben aus dem TOP: Personen und Personengruppen
# =============================================================================


@dataclass
class Gruppe:
    schluessel: str
    name: str
    art: str
    mitglieder: list[Membership] = field(default_factory=list)


#: Was die Rechteprüfung je Mitgliedschaft liest (``PermissionChecker._load_permissions``): vorgeladen prüft die
#: Vereidigung ohne eigene Abfragen je Person
RECHTE_VORLADEN = ("roles__permissions", "individual_permissions", "denied_permissions")


def _mitglieder(organization: Organization, *, nur_vereidigte: bool) -> list[Membership]:
    from apps.tenants.models import Membership

    qs = (
        Membership.objects.filter(organization=organization, is_active=True, is_guest=False)
        .select_related("user")
        .prefetch_related("roles", "expertise_topics", *(RECHTE_VORLADEN if nur_vereidigte else ()))
        .order_by("user__last_name", "user__first_name", "user__email")
    )
    return [m for m in qs if not nur_vereidigte or is_sworn_member(m)]


def erwaehnbare_personen(personen: list[Membership]) -> list[dict[str, str]]:
    """Vorschlagsliste für @-Erwähnungen in den Notizen: Kennung, Name und erste Rolle je Person."""
    liste = []
    for m in personen:
        rollen = [r.name for r in m.roles.all()]
        liste.append({"id": str(m.pk), "name": m.user.get_display_name(), "rolle": rollen[0] if rollen else ""})
    return liste


def personen_und_gruppen(organization: Organization, *, nur_vereidigte: bool) -> tuple[list[Membership], list[Gruppe]]:
    """
    Wer eine Aufgabe aus dem TOP bekommen kann: Personen und Personengruppen der Organisation.

    Gruppen sind die vorhandenen Rollen (Vorsitz, Ratsmitglieder …), die Fachgebiete der Mitglieder (Themen) und
    „Alle Mitglieder“; leere Gruppen fallen weg. Bei nichtöffentlichen TOPs nur Vereidigte.
    """
    mitglieder = _mitglieder(organization, nur_vereidigte=nur_vereidigte)
    rollen: dict[Any, Gruppe] = {}
    themen: dict[Any, Gruppe] = {}
    for m in mitglieder:
        for rolle in m.roles.all():
            rollen.setdefault(rolle.pk, Gruppe(f"rolle:{rolle.pk}", rolle.name, "Rolle")).mitglieder.append(m)
        for thema in m.expertise_topics.all():
            themen.setdefault(thema.pk, Gruppe(f"thema:{thema.pk}", thema.name, "Fachgebiet")).mitglieder.append(m)
    gruppen = sorted(rollen.values(), key=lambda g: g.name.lower()) + sorted(
        themen.values(), key=lambda g: g.name.lower()
    )
    if mitglieder:
        gruppen.append(Gruppe("alle", "Alle Mitglieder", "Alle", list(mitglieder)))
    return mitglieder, gruppen


def empfaenger_aufloesen(
    organization: Organization, personen: list[str], gruppen: list[str], *, nur_vereidigte: bool
) -> list[Membership]:
    """Ausgewählte Personen und Gruppen zu Mitgliedschaften der Organisation (ohne Doppelte, Fremdes fällt weg)."""
    mitglieder, alle_gruppen = personen_und_gruppen(organization, nur_vereidigte=nur_vereidigte)
    nach_id = {str(m.pk): m for m in mitglieder}
    nach_gruppe = {g.schluessel: g for g in alle_gruppen}
    ergebnis: dict[str, Membership] = {}
    for pid in personen:
        if pid in nach_id:
            ergebnis[pid] = nach_id[pid]
    for schluessel in gruppen:
        gruppe = nach_gruppe.get(schluessel)
        for m in gruppe.mitglieder if gruppe else []:
            ergebnis[str(m.pk)] = m
    return list(ergebnis.values())


def aufgaben_anlegen(
    item: FactionAgendaItem,
    membership: Membership,
    *,
    titel: str,
    faellig: date | None,
    empfaenger: list[Membership],
) -> list[Task]:
    """
    Eine Aufgabe je Person (Gruppen sind aufgelöst), verknüpft mit Sitzung und TOP.

    Angelegt über den Aufgabendienst: Position, Aktivität und Benachrichtigung der Zugewiesenen wie bei jeder
    neuen Aufgabe. Ohne Auswahl übernimmt die anlegende Person die Aufgabe.
    """
    from apps.work.tasks import services as aufgaben_dienst
    from apps.work.tasks.models import Task

    meeting = item.meeting
    beschreibung = f"Aus Fraktionssitzung: {meeting.title}\nTOP: {item.number} {item.title}"
    angelegt: list[Task] = []
    with transaction.atomic():
        for person in empfaenger or [membership]:
            task = Task(
                title=titel[:AUFGABE_TITEL_MAX],
                description=beschreibung[:2000],
                assigned_to=person,
                due_date=faellig,
                related_faction_meeting=meeting,
                related_faction_agenda_item=item,
            )
            angelegt.append(aufgaben_dienst.create_task(task, meeting.organization, membership))
    return angelegt


@dataclass
class AufgabenZeile:
    titel: str
    faellig: date | None
    zustaendig: list[str]
    erledigt: int
    gesamt: int
    erste_id: Any
    #: Abhaken in der Zeile (nur bei einer einzelnen Aufgabe und mit dem Recht des Aufgabenboards)
    darf_abhaken: bool = False


def aufgaben_des_tops(item: FactionAgendaItem, membership: Membership) -> list[AufgabenZeile]:
    """
    Sichtbare Aufgaben am TOP; Aufgaben, die zusammen angelegt wurden (gleicher Titel, gleiche Frist, gleiche
    anlegende Person), stehen in einer Zeile mit allen Zuständigen.

    Eine einzelne Aufgabe lässt sich in der Zeile abhaken, wenn das Mitglied sie auch auf dem Aufgabenboard abhaken
    darf (Ersteller, Zuständige, ``tasks.manage``); zusammen angelegte zeigen, wie viele erledigt sind.
    """
    from apps.work.tasks import services as aufgaben_dienst

    from . import services as faction_services

    zeilen: dict[tuple[str, Any, Any], AufgabenZeile] = {}
    darf: dict[Any, bool] = {}
    for task in cast(Any, faction_services).visible_item_tasks(item, membership).order_by("created_at"):
        key = (task.title, task.due_date, task.created_by_id)
        zeile = zeilen.get(key)
        if zeile is None:
            zeile = zeilen[key] = AufgabenZeile(task.title, task.due_date, [], 0, 0, task.pk)
            darf[task.pk] = aufgaben_dienst.can_edit_task(task, membership)
        name = task.assigned_to.user.get_display_name() if task.assigned_to_id and task.assigned_to else ""
        if name and name not in zeile.zustaendig:
            zeile.zustaendig.append(name)
        zeile.gesamt += 1
        zeile.erledigt += 1 if task.is_completed else 0
    for zeile in zeilen.values():
        zeile.darf_abhaken = zeile.gesamt == 1 and darf.get(zeile.erste_id, False)
    return list(zeilen.values())


def aufgabe_umschalten(item: FactionAgendaItem, membership: Membership, task_id: Any) -> Task | None:
    """
    Aufgabe aus dem TOP abhaken bzw. wieder öffnen wie auf dem Aufgabenboard (``toggle_completion``).

    ``None``, wenn die Aufgabe nicht zu diesem TOP gehört, das Mitglied sie nicht sehen oder nicht ändern darf.
    """
    from apps.work.tasks import services as aufgaben_dienst

    from . import services as faction_services

    pk = _kennung(task_id)
    task = cast(Any, faction_services).visible_item_tasks(item, membership).filter(id=pk).first() if pk else None
    if task is None or not aufgaben_dienst.can_edit_task(task, membership):
        return None
    return aufgaben_dienst.toggle_completion(task, membership, reopen_status="todo", record_activity=False)


def darf_top_sehen(item: FactionAgendaItem, membership: Membership) -> bool:
    return can_view_item_with_parents(item, membership)


# =============================================================================
# Laden, Anwesenheit, Rollen und Unterlagen (Schreibwege der Sitzungsansicht)
# =============================================================================


def _kennung(value: Any) -> uuid.UUID | None:
    """Kennung aus Adresse oder Formular; ungültige Werte ergeben ``None`` statt eines Serverfehlers."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def sitzung_laden(organization: Organization, meeting_id: Any) -> FactionMeeting | None:
    """Sitzung der Organisation mit Rollen; ``None`` für fremde oder unbekannte Sitzungen."""
    from .models import FactionMeeting

    pk = _kennung(meeting_id)
    if pk is None:
        return None
    return (
        FactionMeeting.objects.select_related("organization", "meeting_type", "minute_taker__user", "chaired_by__user")
        .filter(id=pk, organization=organization)
        .first()
    )


def top_laden(meeting: FactionMeeting, item_id: Any) -> FactionAgendaItem | None:
    """Angenommener TOP dieser Sitzung; ``None`` für unbekannte, fremde oder nur vorgeschlagene TOPs."""
    from .models import FactionAgendaItem

    pk = _kennung(item_id)
    if pk is None:
        return None
    return (
        FactionAgendaItem.objects.select_related(
            "meeting__organization", "parent", "related_agenda_item", "approves_meeting"
        )
        .filter(id=pk, meeting=meeting, proposal_status="active")
        .first()
    )


#: Längen wie in der Vorbereitung (Titel wie ``FactionAgendaItem.title``, Beschreibung wie die Begründung eines Vorschlags)
TOP_TITEL_MAX = 500
TOP_BESCHREIBUNG_MAX = 5000


#: Richtungen, in die die Sitzungsansicht einen TOP verschiebt (:func:`top_verschieben`)
VERSCHIEBEN = ("up", "down", "internal", "public")
#: Meldung, wenn die Sitzung zwischen Klick und Sperre beendet oder ihr Protokoll genehmigt wurde
VERSCHIEBEN_VORBEI = "Die Tagesordnung lässt sich nicht mehr ändern. Bitte laden Sie die Seite neu."


class TopNeuError(Exception):
    """Neuer TOP abgelehnt: ``meldung`` für den Browser, ``status`` 400 (Eingabe) oder 403 (Nichtöffentliches)."""

    def __init__(self, meldung: str, status: int = 400) -> None:
        super().__init__(meldung)
        self.meldung = meldung
        self.status = status


def top_hinzufuegen(
    meeting: FactionMeeting,
    membership: Membership,
    *,
    titel: str,
    teil: str,
    eltern_id: str = "",
    beschreibung: str = "",
) -> FactionAgendaItem:
    """
    TOP in der laufenden Sitzung eintragen (Issue #1166), auf demselben Weg wie in der Vorbereitung
    (:func:`apps.work.faction.agenda.add_item`): ans Ende des gewählten Teils oder als letzter Unterpunkt eines TOPs.

    Unterpunkte stehen unter einem TOP der obersten Ebene (nicht unter dem Genehmigungs-TOP). Unter einem
    nichtöffentlichen TOP ist jeder Unterpunkt nichtöffentlich; wer „nichtöffentlich“ wählt, bekommt auch unter einem
    öffentlichen TOP einen nichtöffentlichen Unterpunkt, damit nichts ungewollt öffentlich wird. Nichtöffentliches nur
    für Vereidigte (Issue #64). Das Recht (:func:`darf_top_hinzufuegen`) prüft der Aufrufer.
    """
    from . import agenda

    if not titel:
        raise TopNeuError("Bitte geben Sie einen Titel an.")
    if len(titel) > TOP_TITEL_MAX:
        raise TopNeuError("Der Titel ist zu lang.")
    if len(beschreibung) > TOP_BESCHREIBUNG_MAX:
        raise TopNeuError("Die Beschreibung ist zu lang.")
    if teil not in ("public", "internal"):
        raise TopNeuError("Ungültige Eingabe.")
    eltern = None
    if eltern_id:
        eltern = top_laden(meeting, eltern_id)
        if eltern is None or eltern.parent_id is not None or eltern.is_approval_item:
            raise TopNeuError("Den gewählten TOP gibt es nicht mehr. Bitte laden Sie die Seite neu.")
        if not darf_top_sehen(eltern, membership):
            raise TopNeuError("Gesperrte Information", status=403)
        if eltern.visibility == "internal":
            teil = "internal"
    if teil == "internal" and not can_view_internal(membership):
        raise TopNeuError("Gesperrte Information", status=403)
    # Art ohne nichtöffentlichen Teil (Issue #1240): keine neuen NÖ-TOPs, auch keine Unterpunkte bestehender
    if teil == "internal" and not meeting.nichtoeffentlich_erlaubt:
        from .regeln import NOE_AUS

        raise TopNeuError(NOE_AUS)
    if teil not in ("public", "internal"):
        raise TopNeuError("Unbekannter Teil der Tagesordnung.")
    return agenda.add_item(meeting, title=titel, visibility=teil, parent=eltern, description=beschreibung)


#: Nummer aus der Überschrift eines TOPs vor dem Titel („TOP 3.2 “, „TOP NÖ 4 “), wie sie beim Kopieren mitkommt
#: Auch mehrfach hintereinander („TOP 7 TOP 3.2 …“); in einem Durchgang, damit der Aufwand mit der Länge wächst
_TOP_VORSATZ_RE = re.compile(r"^(?:TOP\s+(?:NÖ\s*)?\d+(?:\.\d+)*[.:]?\s+)+", re.IGNORECASE)


def _titel_schluessel(titel: str) -> str:
    """Titel zum Vergleichen: Leerraum zusammengezogen, ohne vorangestellte TOP-Nummern, ohne Groß und Klein."""
    text = " ".join((titel or "").split())
    # Besteht der Titel nur aus Nummern, bleibt die letzte stehen: Sie hat keinen Leerraum mehr hinter sich
    text = _TOP_VORSATZ_RE.sub("", text) or text
    return unicodedata.normalize("NFKC", text).casefold()


def gleichnamige_tops(meeting: FactionMeeting, membership: Membership, titel: str) -> list[FactionAgendaItem]:
    """
    TOPs der Sitzung, deren Titel dem neuen gleicht (Issue #1436): ohne Groß- und Kleinschreibung, ohne mehrfachen
    Leerraum und ohne vorangestellte TOP-Nummer. In der gemeldeten Sitzung entstanden so „TOP 3.2 …“ und
    „TOP 7 TOP 3.2 …“ als neue TOPs, weil ein vorhandener TOP an eine andere Stelle sollte.

    Nur TOPs, die das Mitglied sehen darf: Sonst verriete die Warnung, dass es einen nichtöffentlichen TOP dieses
    Titels gibt.
    """
    # Zu lange Titel lehnt das Anlegen ohnehin ab; verglichen wird nur, was ein Titel sein kann
    if len(titel or "") > TOP_TITEL_MAX:
        return []
    schluessel = _titel_schluessel(titel)
    if not schluessel:
        return []
    kandidaten = (
        meeting.agenda_items.filter(proposal_status="active").select_related("parent").order_by("order", "number")
    )
    return [i for i in kandidaten if _titel_schluessel(i.title) == schluessel and darf_top_sehen(i, membership)]


def doppelt_meldung(gleiche: list[FactionAgendaItem]) -> str:
    """Warnung im Dialog „TOP hinzufügen“; nennt nur Nummern, keine Titel (Sichtschutz für Nichtöffentliches)."""
    nummern = ", ".join(f"TOP {i.number}" for i in gleiche[:3])
    return (
        f"Diesen Titel gibt es schon: {nummern}. Einen vorhandenen TOP legen Sie über „Verschieben“ am TOP an eine "
        "andere Stelle oder in den anderen Teil der Tagesordnung."
    )


class TopVerschiebenError(Exception):
    """Verschieben abgelehnt: ``meldung`` für den Browser, ``status`` 400 (geht nicht) oder 403 (Nichtöffentliches)."""

    def __init__(self, meldung: str, status: int = 400) -> None:
        super().__init__(meldung)
        self.meldung = meldung
        self.status = status


def verschiebbar(meeting: FactionMeeting, membership: Membership, item: FactionAgendaItem) -> dict[str, bool]:
    """
    Wohin sich der TOP in der laufenden Sitzung legen lässt (Menü „Verschieben“ am TOP, Issue #1436): ``up`` und
    ``down`` unter seinen Geschwistern, ``internal`` in den nichtöffentlichen und ``public`` in den öffentlichen
    Teil. Leer für den Genehmigungs-TOP. Das Recht (:func:`darf_top_verschieben`) prüft der Aufrufer.
    """
    from . import agenda

    if item.is_approval_item:
        return {}
    kennungen = [
        s.pk for s in agenda.siblings(meeting, item.parent, agenda.part_of(item)) if darf_top_sehen(s, membership)
    ]
    stelle = kennungen.index(item.pk) if item.pk in kennungen else None
    intern = is_item_internal(item)
    return {
        "up": stelle is not None and stelle > 0,
        "down": stelle is not None and stelle < len(kennungen) - 1,
        "internal": not intern and meeting.nichtoeffentlich_erlaubt and can_view_internal(membership),
        "public": intern,
    }


def top_verschieben(meeting: FactionMeeting, membership: Membership, item: FactionAgendaItem, richtung: str) -> None:
    """
    TOP in der laufenden Sitzung verschieben (Issue #1436), auf demselben Weg wie in der Vorbereitung
    (:func:`apps.work.faction.agenda.place_item`): ``up``/``down`` unter seinen Geschwistern, ``internal`` ans Ende
    des nichtöffentlichen Teils (Unterpunkte gehen mit, ein Unterpunkt wird dort ein eigener TOP), ``public`` zurück
    in den öffentlichen Teil (ein nichtöffentlicher Unterpunkt eines öffentlichen TOPs bleibt an seiner Stelle, alles
    andere kommt ans Ende). Nummern beider Teile werden neu vergeben; Notizen, Beschluss, Aufgaben und Unterlagen
    bleiben am TOP.

    Nichtöffentliches nur für Vereidigte (Issue #64); der Genehmigungs-TOP bleibt an seiner Stelle. Das Recht
    (:func:`darf_top_verschieben`) prüft der Aufrufer vorab; hier gilt es noch einmal mit dem Stand nach der Sperre
    der Sitzung: Wurde inzwischen das Protokoll genehmigt oder die Sitzung beendet, bleibt alles, wie es ist
    (Status 409).
    """
    from . import agenda

    if richtung not in VERSCHIEBEN:
        raise TopVerschiebenError("Ungültige Eingabe.")
    with transaction.atomic():
        agenda.lock_meeting(meeting)
        meeting.refresh_from_db(fields=["status", "protocol_approved"])
        if not darf_top_verschieben(membership, meeting):
            raise TopVerschiebenError(VERSCHIEBEN_VORBEI, status=409)
        _top_verschieben_gesperrt(meeting, membership, item, richtung)


def _top_verschieben_gesperrt(
    meeting: FactionMeeting, membership: Membership, item: FactionAgendaItem, richtung: str
) -> None:
    """Die Verschiebung selbst; die Sitzung ist gesperrt und ihr Stand frisch gelesen (:func:`top_verschieben`)."""
    from . import agenda
    from .regeln import NOE_AUS, NichtOeffentlichAusError

    def sichtbar(eintrag: Any) -> bool:
        return darf_top_sehen(eintrag, membership)

    if not sichtbar(item):
        raise TopVerschiebenError("Gesperrte Information", status=403)
    moeglich = verschiebbar(meeting, membership, item)
    try:
        if richtung in ("up", "down"):
            if not moeglich.get(richtung):
                raise TopVerschiebenError("Der TOP steht schon am Rand. Weiter lässt er sich nicht verschieben.")
            geschwister = [s for s in agenda.siblings(meeting, item.parent, agenda.part_of(item)) if sichtbar(s)]
            stelle = next(i for i, s in enumerate(geschwister) if s.pk == item.pk)
            neu = stelle - 1 if richtung == "up" else stelle + 1
            agenda.place_item(meeting, item, parent=item.parent, position=neu, visible=sichtbar)
        elif richtung == "internal":
            if not can_view_internal(membership):
                raise TopVerschiebenError("Gesperrte Information", status=403)
            if not meeting.nichtoeffentlich_erlaubt:
                raise TopVerschiebenError(NOE_AUS)
            if is_item_internal(item):
                raise TopVerschiebenError("Der TOP steht schon im nichtöffentlichen Teil.")
            agenda.place_item(meeting, item, parent=None, position=None, visibility="internal", visible=sichtbar)
        else:
            if not is_item_internal(item):
                raise TopVerschiebenError("Der TOP steht schon im öffentlichen Teil.")
            eltern = item.parent
            if eltern is not None and eltern.visibility == "public":
                # Nichtöffentlicher Unterpunkt eines öffentlichen TOPs: bleibt Unterpunkt an seiner Stelle
                geschwister = [s for s in agenda.siblings(meeting, eltern, eltern.visibility) if sichtbar(s)]
                platz = next((i for i, s in enumerate(geschwister) if s.pk == item.pk), None)
                agenda.place_item(meeting, item, parent=eltern, position=platz, visibility="public", visible=sichtbar)
            else:
                agenda.place_item(meeting, item, parent=None, position=None, visibility="public", visible=sichtbar)
    except agenda.VerschiebenError as abgelehnt:
        raise TopVerschiebenError(str(abgelehnt)) from abgelehnt
    except NichtOeffentlichAusError as abgelehnt:
        raise TopVerschiebenError(NOE_AUS) from abgelehnt


def protokolleintraege(item: FactionAgendaItem, membership: Membership | None = None) -> list[Any]:
    """
    Bisherige Protokolleinträge des TOPs (auch Wortbeiträge), unverändert in ihrer Reihenfolge. Mit ``membership`` nur,
    wenn das Mitglied sie lesen darf (:func:`darf_eintraege_lesen`), sonst keine.
    """
    if membership is not None and not darf_eintraege_lesen(membership, item.meeting, intern=is_item_internal(item)):
        return []
    return list(
        item.protocol_entries.select_related("speaker__user", "action_assignee__user", "created_by__user").order_by(
            "order", "created_at"
        )
    )


@dataclass(frozen=True)
class AnwesenheitGruppe:
    """Eine Gruppe der Anwesenheitsliste: Kennung (für ``id``), Überschrift und Teilnahmen nach Namen."""

    schluessel: str
    titel: str
    teilnahmen: list[Any]


@dataclass
class Anwesenheit:
    teilnahmen: list[Any]
    anwesend: int
    gesamt: int
    #: Stimmberechtigte zuerst, dann weitere Mitglieder, dann Gäste (Issue #1166); leere Gruppen fehlen
    gruppen: list[AnwesenheitGruppe] = field(default_factory=list)
    #: Aktive Mitglieder der Organisation ohne Teilnahme an dieser Sitzung, nach Namen (Issue #1436)
    uebrige: list[Any] = field(default_factory=list)


def _nach_namen(name: str) -> str:
    """
    Sortierschlüssel für angezeigte Namen: ohne Groß- und Kleinschreibung, Umlaute und Akzente wie ihr Grundbuchstabe
    („Ömer“ vor „Otto“, wie im Wörterbuch).
    """
    zerlegt = unicodedata.normalize("NFKD", name or "")
    return "".join(zeichen for zeichen in zerlegt if not unicodedata.combining(zeichen)).casefold()


def uebrige_mitglieder(meeting: FactionMeeting, teilnahmen: list[Any]) -> list[Membership]:
    """
    Aktive Mitglieder der Organisation ohne Gastzugang, die in ``teilnahmen`` nicht vorkommen, nach dem angezeigten
    Namen (Issue #1436). Das sind Mitglieder, die zur Sitzung nicht eingeladen wurden: Sie gehören nicht zu den
    Teilnehmenden der Sitzungsart oder kamen erst nach dem Beginn dazu.
    """
    from apps.tenants.models import Membership

    vorhanden = {a.membership_id for a in teilnahmen if a.membership_id}
    qs = (
        Membership.objects.filter(organization_id=meeting.organization_id, is_active=True, is_guest=False)
        .exclude(pk__in=vorhanden)
        .select_related("user")
        .prefetch_related("roles")
    )
    return sorted(qs, key=lambda m: _nach_namen(m.user.get_display_name()))


def anwesenheit(meeting: FactionMeeting, *, mit_uebrigen: bool = False) -> Anwesenheit:
    """
    Teilnahmen (Mitglieder, dann Gäste), wie viele Mitglieder anwesend sind, und die Gruppen der Liste.

    Gruppen (Issue #1166): „Stimmberechtigt“ nach derselben Regel wie die Beschlussfähigkeit
    (:func:`.quorum.stimmberechtigt`, die Administrator-Rolle allein gibt kein Stimmrecht), dann „Weitere Mitglieder“,
    dann „Gäste“. Rollen und Rechte sind vorgeladen, ohne eigene Abfragen je Teilnahme. In einer Sitzungsart ohne
    Abstimmungen (Issue #1240) stehen alle Mitglieder in einer Gruppe „Mitglieder“.

    Reihenfolge (Issue #1436): Die Gruppen sind nach dem angezeigten Namen sortiert (Vorname zuerst), so wie man die
    Liste liest. ``teilnahmen`` bleibt nach Nachnamen, in dieser Reihenfolge nennt die Niederschrift die Personen.
    ``mit_uebrigen`` liefert dazu die aktiven Mitglieder ohne Teilnahme (:func:`uebrige_mitglieder`).
    """
    from .quorum import stimmberechtigt

    teilnahmen = list(
        meeting.attendances.select_related("membership__user")
        .prefetch_related(*(f"membership__{feld}" for feld in RECHTE_VORLADEN))
        .order_by("is_guest", "membership__user__last_name", "membership__user__first_name", "guest_name")
    )
    mitglieder = [a for a in teilnahmen if not a.is_guest]
    gaeste = [a for a in teilnahmen if a.is_guest]
    aufteilung: list[tuple[str, str, list[Any]]]
    if meeting.abstimmungen_erlaubt:
        stimme = {a.pk: stimmberechtigt(a.membership) for a in mitglieder}
        aufteilung = [
            ("stimmberechtigt", "Stimmberechtigt", [a for a in mitglieder if stimme[a.pk]]),
            ("weitere", "Weitere Mitglieder", [a for a in mitglieder if not stimme[a.pk]]),
        ]
    else:
        # Sitzungsart ohne Abstimmungen (Issue #1240): ohne Stimmrecht keine Trennung der Mitglieder
        aufteilung = [("mitglieder", "Mitglieder", mitglieder)]
    gruppen = [
        AnwesenheitGruppe(schluessel, titel, sorted(liste, key=lambda a: _nach_namen(a.get_display_name())))
        for schluessel, titel, liste in (*aufteilung, ("gaeste", "Gäste", gaeste))
        if liste
    ]
    return Anwesenheit(
        teilnahmen,
        sum(1 for a in mitglieder if a.status == "present"),
        len(mitglieder),
        gruppen,
        uebrige_mitglieder(meeting, mitglieder) if mit_uebrigen else [],
    )


def rollen_auswahl(organization: Organization) -> list[Membership]:
    """Wer Sitzungsleitung oder Schriftführung sein kann: aktive Mitglieder ohne Gastzugang."""
    return _mitglieder(organization, nur_vereidigte=False)


#: Status, die die Sitzungsansicht setzen darf (anwesend, abwesend, entschuldigt)
ANWESENHEIT_STATUS = ("present", "absent", "excused")


def anwesenheit_setzen(meeting: FactionMeeting, attendance_id: Any, *, status: str, art: str) -> bool:
    """
    Status (anwesend, abwesend, entschuldigt) und/oder Teilnahmeart (vor Ort, online) einer Teilnahme setzen.

    ``False`` bei unbekannter Teilnahme oder ohne gültige Angabe. Wer anwesend wird, bekommt die Zeit des Eintritts.
    """
    from .models import FactionAttendance

    pk = _kennung(attendance_id)
    teilnahme = meeting.attendances.filter(id=pk).first() if pk else None
    if teilnahme is None:
        return False
    felder = ["updated_at"]
    if status in ANWESENHEIT_STATUS:
        if status == "present" and teilnahme.status != "present":
            teilnahme.checked_in_at = timezone.now()
            felder.append("checked_in_at")
        teilnahme.status = status
        felder.append("status")
    if art in dict(FactionAttendance.PARTICIPATION_TYPE_CHOICES):
        teilnahme.participation_type = art
        felder.append("participation_type")
    if len(felder) == 1:
        return False
    teilnahme.save(update_fields=felder)
    return True


def teilnahme_nachtragen(meeting: FactionMeeting, membership_id: Any, *, status: str, art: str) -> bool:
    """
    Aktives Mitglied der Organisation ohne Einladung in die Anwesenheit der Sitzung aufnehmen und zugleich Status
    und Teilnahmeart setzen (Issue #1436). Hat das Mitglied inzwischen eine Teilnahme, wird nur sie gesetzt.

    ``False`` bei unbekannter Kennung, fremder Organisation, inaktivem Mitglied, Gastzugang oder ohne gültigen
    Status, ebenso wenn die Anwesenheit inzwischen bestätigt oder die Sitzung beendet ist. Die Sitzung ist
    währenddessen gesperrt, damit zwei Klicks nicht zwei Teilnahmen anlegen. Das Recht
    (:func:`darf_teilnahme_nachtragen`) prüft der Aufrufer.
    """
    from apps.tenants.models import Membership

    from . import agenda
    from .models import FactionAttendance

    pk = _kennung(membership_id)
    if pk is None or status not in ANWESENHEIT_STATUS:
        return False
    mitglied = Membership.objects.filter(
        pk=pk, organization_id=meeting.organization_id, is_active=True, is_guest=False
    ).first()
    if mitglied is None:
        return False
    with transaction.atomic():
        agenda.lock_meeting(meeting)
        # Stand nach der Sperre: Wurde die Anwesenheit inzwischen bestätigt oder die Sitzung beendet, entsteht keine
        # neue Teilnahme mehr (die Prüfung des Aufrufers lief vor der Sperre)
        stand = type(meeting).objects.filter(pk=meeting.pk).values("status", "attendance_confirmed_at").first()
        if stand is None or stand["status"] != "ongoing" or stand["attendance_confirmed_at"] is not None:
            return False
        teilnahme = meeting.attendances.filter(membership=mitglied).first()
        if teilnahme is None:
            werte: dict[str, Any] = {"status": status}
            if status == "present":
                werte["checked_in_at"] = timezone.now()
            if art in dict(FactionAttendance.PARTICIPATION_TYPE_CHOICES):
                werte["participation_type"] = art
            FactionAttendance.objects.create(meeting=meeting, membership=mitglied, **werte)
            return True
    return anwesenheit_setzen(meeting, teilnahme.pk, status=status, art=art)


def gast_hinzufuegen(meeting: FactionMeeting, name: str) -> Any:
    """Gast als anwesend eintragen (Name höchstens 200 Zeichen)."""
    from .models import FactionAttendance

    return FactionAttendance.objects.create(
        meeting=meeting, is_guest=True, guest_name=name[:200], status="present", checked_in_at=timezone.now()
    )


def rollen_setzen(meeting: FactionMeeting, werte: dict[str, str]) -> bool:
    """
    Sitzungsleitung (``leitung``) und Schriftführung (``schriftfuehrung``) setzen; ein leerer Wert entfernt die Rolle.

    Nur aktive Mitglieder der Organisation ohne Gastzugang; ``False`` bei einer fremden Kennung (nichts geändert).
    """
    from . import services as faction_services

    personen: dict[str, Any] = {}
    for feld, schluessel in (("chaired_by", "leitung"), ("minute_taker", "schriftfuehrung")):
        if schluessel not in werte:
            continue
        wert = werte[schluessel]
        person = cast(Any, faction_services).org_member(meeting.organization, wert) if wert else None
        if wert and person is None:
            return False
        personen[feld] = person
    for feld, person in personen.items():
        setattr(meeting, feld, person)
    meeting.save(update_fields=[*personen, "updated_at"])
    return True


def anlage_speichern(item: FactionAgendaItem, datei: Any, membership: Membership) -> Any:
    """Geprüfte Datei als Anlage an den TOP hängen (Tischvorlage)."""
    from .models import FactionAgendaItemAttachment

    return FactionAgendaItemAttachment.objects.create(
        agenda_item=item,
        file=datei,
        filename=datei.name or "Anlage",
        mime_type=datei.content_type or "",
        file_size=datei.size or 0,
        uploaded_by=membership,
    )


#: Höchstzahl der Treffer in den Suchdialogen „RIS-Vorlage“ und „Eigenes Dokument“
SUCHE_TREFFER = 15


def vorlagen_suchen(item: FactionAgendaItem, organization: Organization, text: str) -> list[dict[str, str]]:
    """RIS-Vorlagen der Kommunen der Organisation (ab zwei Zeichen), ohne die schon verknüpften."""
    from hub.ris import selectors as ris

    bodies = cast(Any, organization).get_all_bodies()
    if len(text) < 2 or not bodies.exists():
        return []
    vorhanden = item.related_papers.values_list("id", flat=True)
    return [
        {
            "id": str(p.pk),
            "titel": p.name or "",
            "nummer": p.reference or "",
            "art": p.paper_type or "",
            "datum": p.date.strftime("%d.%m.%Y") if p.date else "",
        }
        for p in ris.search_papers(bodies, text).exclude(id__in=vorhanden)[:SUCHE_TREFFER]
    ]


def vorlage_verknuepfen(item: FactionAgendaItem, organization: Organization, paper_id: Any) -> Any:
    """RIS-Vorlage der eigenen Kommunen mit dem TOP verknüpfen; ``None``, wenn es sie dort nicht gibt."""
    from hub.ris import selectors as ris

    bodies = cast(Any, organization).get_all_bodies()
    paper = ris.paper(bodies, paper_id) if bodies.exists() else None
    if paper is not None:
        item.related_papers.add(paper)
    return paper


def dokumente_suchen(item: FactionAgendaItem, membership: Membership, text: str) -> list[dict[str, str]]:
    """Eigene Dokumente, die das Mitglied sehen darf, ohne die schon verknüpften; zuletzt geänderte zuerst."""
    from apps.work.motions.models import Motion

    qs = cast(Any, Motion).visible_to(membership).exclude(id__in=item.related_motions.values("id"))
    if text:
        qs = qs.filter(title__icontains=text)
    return [
        {"id": str(m.pk), "titel": m.title or "Dokument", "datum": m.updated_at.strftime("%d.%m.%Y")}
        for m in qs.order_by("-updated_at")[:SUCHE_TREFFER]
    ]


def dokument_verknuepfen(item: FactionAgendaItem, membership: Membership, motion_id: Any) -> Any:
    """Sichtbares eigenes Dokument mit dem TOP verknüpfen; ``None``, wenn das Mitglied es nicht sehen darf."""
    from apps.work.motions.models import Motion

    pk = _kennung(motion_id)
    motion = cast(Any, Motion).visible_to(membership).filter(id=pk).first() if pk else None
    if motion is not None:
        item.related_motions.add(motion)
    return motion

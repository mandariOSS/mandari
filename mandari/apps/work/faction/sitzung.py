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


def _besprochen(item: FactionAgendaItem) -> bool:
    decision_present = item.recorded_decision is not None
    return bool(decision_present or item.notes_encrypted or getattr(item, "eintraege", 0))


def tagesordnung(meeting: FactionMeeting, membership: Membership) -> Tagesordnung:
    """Angenommene TOPs: öffentlicher Teil, dann nichtöffentlicher Teil; Unterpunkte direkt unter ihrem TOP."""
    from .models import FactionAgendaItem

    darf_intern = can_view_internal(membership)
    alle = list(
        FactionAgendaItem.objects.filter(meeting=meeting, proposal_status="active")
        .select_related("decision", "parent", "meeting__organization", "approves_meeting")
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


def protokoll_fassung(membership: Membership) -> str:
    """Fassung der Niederschrift, die das Mitglied lesen darf (wie der PDF-Export); leer ohne Recht."""
    if can_view_internal(membership) and membership.has_permission("protocols.view_full"):
        return "intern"
    if membership.has_permission("protocols.view_public"):
        return "oeffentlich"
    return ""


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
    vorige = item.approves_meeting if item.is_approval_item and item.approves_meeting_id else None
    fassung = protokoll_fassung(membership) if vorige is not None else ""
    if vorige is not None and fassung:
        protokoll = f"Protokoll vom {timezone.localtime(vorige.start):%d.%m.%Y}" if vorige.start else "Protokoll"
        liste.append(Unterlage(f"p-{vorige.pk}", "protokoll", protokoll, vorige, fassung=fassung))
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


def herkunft(item: FactionAgendaItem) -> str:
    """Zweite Zeile im Kopf des TOPs: übergeordneter TOP, vorbereiteter Gremien-TOP, Standard-TOP."""
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
    vorige = item.approves_meeting if item.is_approval_item and item.approves_meeting_id else None
    if vorige is not None:
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
        beratungen = list(ris.consultations_of_paper(paper).exclude(agenda_item_external_id__isnull=True))
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
    import re

    text = re.sub(r"<[^>]+>", "", html or "")
    return not text.strip() and "<li" not in (html or "")


def notizen_speichern(
    item: FactionAgendaItem, html: str, *, basis: str | None, membership: Membership
) -> NotizErgebnis:
    """
    Notizen eines TOPs speichern, nur wenn niemand inzwischen gespeichert hat (``basis`` = Stand beim Laden).

    Weicht der gespeicherte Stand ab, bleibt er unverändert; zurück kommt der aktuelle Stand, damit der Browser
    beide Fassungen zusammenführen kann (keine stillen Verluste). Gespeichert wird nur die Positivliste des Editors.
    Die Änderungshistorie erhält höchstens alle zehn Minuten je TOP einen Eintrag (ohne Inhalt).
    """
    from . import audit
    from .models import FactionAgendaItem

    sauber = "" if _ist_leer(html) else sanitize_editor_html(html)
    with transaction.atomic():
        gesperrt = FactionAgendaItem.objects.select_for_update().select_related("meeting__organization").get(pk=item.pk)
        aktuell = str(cast(Any, gesperrt).get_notes_decrypted() or "")
        if basis is not None and basis != fingerabdruck(aktuell):
            return NotizErgebnis(False, aktuell, fingerabdruck(aktuell), gesperrt.notes_updated_at)
        if sauber == aktuell:
            return NotizErgebnis(True, aktuell, fingerabdruck(aktuell), gesperrt.notes_updated_at)
        cast(Any, gesperrt).set_notes_encrypted(sauber)
        jetzt = timezone.now()
        # Ohne save(): sonst entstünde je automatischer Speicherung ein Eintrag in der Änderungshistorie
        FactionAgendaItem.objects.filter(pk=item.pk).update(
            notes_encrypted=gesperrt.notes_encrypted, notes_updated_at=jetzt, updated_at=jetzt
        )
    if _historie_faellig(item):
        audit.log_event(
            "update",
            gesperrt,
            membership=membership,
            changes={"notes_encrypted": {"alt": "[verschlüsselt geändert]", "neu": "[verschlüsselt geändert]"}},
        )
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


def _mitglieder(organization: Organization, *, nur_vereidigte: bool) -> list[Membership]:
    from apps.tenants.models import Membership

    qs = (
        Membership.objects.filter(organization=organization, is_active=True, is_guest=False)
        .select_related("user")
        .prefetch_related("roles", "expertise_topics")
        .order_by("user__last_name", "user__first_name", "user__email")
    )
    return [m for m in qs if not nur_vereidigte or is_sworn_member(m)]


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
        FactionMeeting.objects.select_related("organization", "minute_taker__user", "chaired_by__user")
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


def protokolleintraege(item: FactionAgendaItem) -> list[Any]:
    """Bisherige Protokolleinträge des TOPs (auch Wortbeiträge), unverändert in ihrer Reihenfolge."""
    return list(
        item.protocol_entries.select_related("speaker__user", "action_assignee__user", "created_by__user").order_by(
            "order", "created_at"
        )
    )


@dataclass
class Anwesenheit:
    teilnahmen: list[Any]
    anwesend: int
    gesamt: int


def anwesenheit(meeting: FactionMeeting) -> Anwesenheit:
    """Teilnahmen (Mitglieder, dann Gäste) und wie viele Mitglieder anwesend sind."""
    teilnahmen = list(
        meeting.attendances.select_related("membership__user")
        .prefetch_related("membership__roles")
        .order_by("is_guest", "membership__user__last_name", "membership__user__first_name", "guest_name")
    )
    mitglieder = [a for a in teilnahmen if not a.is_guest]
    return Anwesenheit(teilnahmen, sum(1 for a in mitglieder if a.status == "present"), len(mitglieder))


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

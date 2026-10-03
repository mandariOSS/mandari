# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse aus mandari Session im kanonischen RIS-Modell (Datendrehscheibe, Etappe E4, Issues #533–#535).

Session meldet ihre fachlichen Änderungen als Ereignisse ``ris.*``. Die Verträge gehören der Drehscheibe
(``x-owner: hub.ris``, ``docs/adr/20260929-ereignisvertraege.md``): Dieselben Typen entstehen auch beim Abgleich
fremder RIS. Deshalb bildet und schreibt dieses Modul die Ereignisse; Session ruft es aus ihren Fachfunktionen
auf (``apps.session.hub_events``). Die Drehscheibe importiert Session nicht, sie liest nur Attribute der
hereingereichten Objekte – wie die Abbildung (``hub.ris.mapping.session``).

**Zustand vor und nach der Änderung.** Der Aufrufer hält in derselben Transaktion den Zustand der betroffenen
Objekte vor der Änderung fest (``meeting_state``, ``agenda_item_state``) und danach noch einmal. Aus beiden
Zuständen entstehen die Ereignisse: Was sich im kanonischen Modell nicht ändert (interne Notizen, Zugangsweg,
Zeitpunkte im Sitzungsverlauf), ergibt kein Ereignis; was sich ändert, genau eines je Objekt und Empfängerkreis.

**Kennungen** sind die kanonischen (``SessionUris.canonical_id``), dieselben, unter denen die
Session-Schnittstelle und der RIS-Bestand das Objekt führen. ``body_id`` ist die kanonische Kennung des Body der
Schnittstelle (bis #758 einer je Mandant), der Mandant ``session:<uuid>``. Damit erscheinen die Ereignisse im
Änderungsfeed der Session-Schnittstelle und des Aggregators (``hub.api.changes``).

**Sichtbarkeit** folgt der Veröffentlichung in der Schnittstelle (``apps.session.oparl_publication``):

- ``oeffentlich`` ist nur, was die Schnittstelle nach der Änderung ausliefert, und nur mit den Feldern, die sie
  ausliefert. Vor ihrer Freischaltung (Issue #319) liefert sie nichts aus; der Aufrufer übergibt dann eine
  Veröffentlichungsregel, nach der nichts veröffentlicht ist. Von einer nichtöffentlichen Sitzung mit veröffentlichtem Termin (Issue #757) sind das Name, Zeit,
  Status, Absage und Gremien, nie Ort oder Format. Ändert sich nur, was die Öffentlichkeit nicht sieht, erfährt
  sie davon nichts – der Feed bleibt unverändert.
- Wird ein Objekt veröffentlicht, ist es für öffentliche Empfänger neu (``ris.meeting.scheduled``, ``added``).
  Endet die Veröffentlichung, meldet ``ris.object.depublished`` (``operation=delete``) die Rücknahme mit Grund;
  die Änderung selbst erfahren nur die übrigen Empfänger (``nichtoeffentlich``). Gelöschtes geht wie beim
  Ingestor und bei ``hub.ris.retraction`` (``drafts``) an die Empfänger, die es kannten.
- Alles Übrige ist ``nichtoeffentlich``. Ein öffentliches Ereignis nennt keine Kennung eines nichtöffentlichen
  Objekts (die Vorlage eines Tagesordnungspunkts nur, solange sie veröffentlicht ist).

**Nutzlast:** nur Kennungen, Codes und Namen geänderter Felder (Namen des kanonischen Modells), nie Inhalte und
nie Personen. Inhalte liest der Empfänger mit Rechteprüfung beim Eigentümer.

**Reihenfolge** innerhalb einer Änderung: Sitzungen, Vorlagen, Tagesordnungspunkte (in der Reihenfolge der
Tagesordnung), Beratungen (je Vorlage in der Reihenfolge der Stationen), Anlagen, zuletzt ausdrücklich gemeldete
Vorgänge wie die Ladung. Der Sequenzierer übernimmt die Reihenfolge des Schreibens.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Final

from django.core.exceptions import ObjectDoesNotExist

from apps.events import CanonicalRef, publish, tenant_ref
from apps.events.models import Operation, Visibility
from hub.ris import retraction
from hub.ris.mapping.session import SessionUris
from hub.ris.retraction import Draft

#: Schemaversion der Ereignisse dieses Moduls
VERSION: Final = 1

MEETING_SCHEDULED: Final = "ris.meeting.scheduled"
MEETING_CHANGED: Final = "ris.meeting.changed"
MEETING_INVITED: Final = "ris.meeting.invited"
AGENDA_ITEM_CHANGED: Final = retraction.AGENDA_ITEM_CHANGED
DEPUBLISHED: Final = retraction.DEPUBLISHED

#: Sichtbarkeit einer Sitzung in der Schnittstelle: öffentlich, nur der Termin (Issue #757), gar nicht
FULL: Final = "voll"
DATE_ONLY: Final = "termin"
HIDDEN: Final = "verborgen"

#: Felder einer Sitzung im kanonischen Modell, die auch der veröffentlichte Termin einer nichtöffentlichen
#: Sitzung zeigt (``SessionMapping._meeting_date``)
MEETING_DATE_FIELDS: Final = ("name", "start", "end", "meetingState", "cancelled", "organization")
#: ... und die nur eine öffentliche Sitzung zeigt (Ort und Sitzungsformat, ``SessionMapping.meeting``)
MEETING_FULL_FIELDS: Final = ("location", "meetingFormat")
MEETING_FIELDS: Final = MEETING_DATE_FIELDS + MEETING_FULL_FIELDS

#: Felder eines Tagesordnungspunkts im kanonischen Modell (``consultation``: die beratene Vorlage,
#: ``resolutionText``: der öffentliche Beschlusstext). Ergebnis, Abstimmung, Beschlussnummer und Umsetzung melden
#: eigene Ereignisse (``DECISION_FIELDS``).
AGENDA_ITEM_FIELDS: Final = ("name", "number", "order", "public", "withdrawn", "consultation", "resolutionText")

VOTING_RECORDED: Final = "ris.voting.recorded"
RESOLUTION_ADOPTED: Final = "ris.resolution.adopted"
IMPLEMENTATION_CHANGED: Final = "ris.resolution.implementation_changed"
PROTOCOL_APPROVED: Final = "ris.protocol.approved"
PROTOCOL_PUBLISHED: Final = "ris.protocol.published"

#: Felder der Beschlussfassung am TOP: Ergebnis, Art der Abstimmung, Summen, Beschlussnummer, Umsetzungsstand
DECISION_FIELDS: Final = ("result", "votingMethod", "votes", "resolutionNumber", "implementationStatus")
#: Ergebnis „noch offen“ – alles andere ist ein gefasster bzw. festgestellter Beschluss
PENDING: Final = "pending"
#: Ergebnisse, die aus einer Abstimmung hervorgehen (``ris.voting.recorded``); vertagt, zurückgezogen und zur
#: Kenntnis genommen sind Beschlüsse ohne Abstimmung
VOTED_RESULTS: Final = frozenset({"approved", "rejected"})
#: Umsetzungsstände der Beschlusskontrolle (Vertrag ``ris.resolution.implementation_changed``)
IMPLEMENTATION_STATUSES: Final = frozenset({"open", "in_progress", "done", "deferred"})
#: Feldname der Niederschrift in den Änderungslisten einer Sitzung (Entwurf, Prüfung; intern) bzw. ihrer
#: öffentlichen Fassung (``resultsProtocol``, öffentlich)
PROTOCOL: Final = "protocol"
RESULTS_PROTOCOL: Final = "resultsProtocol"

#: Versandarten einer Ladung (Vertrag ``ris.meeting.invited``)
DISPATCH_TYPES: Final = frozenset({"invitation", "supplementary", "substitution"})

#: Feldname „öffentlich“ in den Änderungslisten, wenn sich die Sichtbarkeit einer Sitzung ändert
PUBLIC: Final = "public"

PAPER_CREATED: Final = "ris.paper.created"
PAPER_RELEASED: Final = "ris.paper.released"
PAPER_CHANGED: Final = "ris.paper.changed"
CONSULTATION_CHANGED: Final = "ris.consultation.changed"
FILE_CHANGED: Final = "ris.file.changed"

#: Felder einer Vorlage, die die Schnittstelle ausliefert (``SessionMapping.paper``) ...
PAPER_PUBLIC_FIELDS: Final = (
    "name",
    "reference",
    "date",
    "paperType",
    "originatorPerson",
    "originatorOrganization",
    "underDirectionOf",
)
#: ... und solche, die nur die übrigen Empfänger erfahren: Bearbeitungsstand, Öffentlichkeit, Sachverhalt und
#: Beschlussvorschlag (die Schnittstelle gibt Inhalte nur als Datei aus)
PAPER_INTERNAL_FIELDS: Final = ("status", "public", "mainText", "resolutionText")
PAPER_FIELDS: Final = PAPER_PUBLIC_FIELDS + PAPER_INTERNAL_FIELDS

#: Felder einer Beratung (``SessionMapping.consultation``); ``result`` (Ergebnis der Station) erfahren nur die
#: übrigen Empfänger. ``order`` nennt das Objekt der Beratung nicht, es ist aber öffentlich: Die Vorlage bettet
#: ihre Beratungsfolge in dieser Reihenfolge ein (``SessionMapping.paper``)
CONSULTATION_FIELDS: Final = ("organization", "meeting", "agendaItem", "role", "authoritative", "order", "result")
#: Angaben, die die Schnittstelle bei einer Station in einer nichtöffentlichen Sitzung weglässt
CONSULTATION_STATION_FIELDS: Final = ("organization", "role", "authoritative")

#: Felder einer Datei: Anzeigename, Fassung (Nummer und gespeicherte Datei) und woran sie hängt
FILE_FIELDS: Final = ("name", "version", "paper", "meeting", "agendaItem")
#: Bezüge einer Datei: Feld, Objektart der Schnittstelle, Schlüssel der Nutzlast
FILE_REFS: Final = (
    ("paper", "paper", "paper"),
    ("meeting", "meeting", "meeting"),
    ("agendaItem", "agendaitem", "agenda_item"),
)


# =============================================================================
# Zustände
# =============================================================================


@dataclass(frozen=True)
class MeetingState:
    """Eine Sitzung, wie das kanonische Modell sie sieht (Felder mit vergleichbaren Werten)."""

    id: uuid.UUID
    publicity: str
    fields: Mapping[str, Any]
    #: Gremien (Kennungen in Session), federführendes zuerst
    organizations: tuple[uuid.UUID, ...] = ()

    @property
    def visible(self) -> bool:
        """Liefert die Schnittstelle die Sitzung aus (ganz oder nur den Termin)?"""
        return self.publicity != HIDDEN

    def public_value(self, name: str) -> Any:
        """Wert, den die Öffentlichkeit sieht; ``None``, wenn die Schnittstelle das Feld nicht ausliefert."""
        if self.publicity == FULL or (self.publicity == DATE_ONLY and name in MEETING_DATE_FIELDS):
            return self.fields.get(name)
        return None


@dataclass(frozen=True)
class AgendaItemState:
    """Ein Tagesordnungspunkt, wie das kanonische Modell ihn sieht."""

    id: uuid.UUID
    meeting_id: uuid.UUID
    #: Liefert die Schnittstelle den Punkt aus (öffentlicher Punkt einer öffentlichen Sitzung)?
    published: bool
    fields: Mapping[str, Any]
    #: Beratene Vorlage (Kennung in Session) und ob sie veröffentlicht ist
    paper_id: uuid.UUID | None = None
    paper_published: bool = False
    #: Ergebnis, Abstimmung, Beschlussnummer, Umsetzung (``DECISION_FIELDS``) und die Beratung des TOP
    decision: Mapping[str, Any] = field(default_factory=dict)
    consultation_id: uuid.UUID | None = None
    #: Ist die Umsetzung zur Veröffentlichung freigegeben (Beschlusskontrolle, Issue #48)?
    implementation_public: bool = False

    @property
    def withdrawn(self) -> bool:
        return bool(self.fields.get("withdrawn"))

    def public_value(self, name: str) -> Any:
        if not self.published:
            return None
        if name == "consultation":
            # Die Vorlage nennt die Schnittstelle nur, solange sie veröffentlicht ist
            return self.paper_id if self.paper_published else None
        return self.fields.get(name)


def _location(meeting: Any) -> tuple[str, ...] | None:
    """Ort der Sitzung (dieselben Felder wie das Location-Objekt der Schnittstelle); ``None`` ohne Angabe."""
    parts = (meeting.location, meeting.room, meeting.street_address, meeting.postal_code, meeting.locality)
    return tuple(part or "" for part in parts) if any(parts) else None


def _meeting_format(meeting: Any) -> tuple[str, ...] | None:
    """Sitzungsformat mit Hinweis für die Öffentlichkeit; ``None`` für Präsenz ohne Übertragung (wie die Abbildung)."""
    url, note = meeting.public_access_url or "", meeting.public_access_note or ""
    if meeting.format == meeting.FORMAT_PRESENCE and not (url or note):
        return None
    return (meeting.format, url, note)


def meeting_state(meeting: Any, *, is_published: Callable[[Any], bool]) -> MeetingState:
    """
    Zustand einer Sitzung (``SessionMeeting``); weitere Gremien am besten vorgeladen (``joint_organizations``).

    ``is_published`` ist die Veröffentlichungsregel von Session samt Freischaltung der Schnittstelle: Liefert sie
    die Sitzung nicht aus, ist sie verborgen, sonst ganz öffentlich oder nur ihr Termin.
    """
    if not is_published(meeting):
        publicity = HIDDEN
    elif meeting.is_public:
        publicity = FULL
    else:
        publicity = DATE_ONLY
    organizations = tuple(meeting.participating_organization_ids)
    fields = {
        "name": meeting.name,
        "start": meeting.start,
        "end": meeting.end,
        "meetingState": meeting.meeting_state,
        "cancelled": bool(meeting.cancelled),
        "organization": organizations,
        "location": _location(meeting),
        "meetingFormat": _meeting_format(meeting),
    }
    return MeetingState(id=meeting.pk, publicity=publicity, fields=fields, organizations=organizations)


def agenda_item_state(item: Any, *, is_published: Callable[[Any], bool]) -> AgendaItemState:
    """
    Zustand eines Tagesordnungspunkts (``SessionAgendaItem`` mit geladener Sitzung und Vorlage).

    ``is_published`` ist die Veröffentlichungsregel von Session (``SessionSource.is_published``), angewandt auf
    den Punkt und auf seine Vorlage.
    """
    paper = item.paper if item.paper_id else None
    fields = {
        "name": item.name,
        "number": item.number,
        "order": item.order,
        "public": bool(item.is_public),
        "withdrawn": bool(item.is_withdrawn),
        "consultation": item.paper_id,
        # Nur der öffentliche Beschlusstext – das verschlüsselte Feld nie
        "resolutionText": item.resolution_text or "",
    }
    decision = {
        "result": item.vote_result or PENDING,
        "votingMethod": item.voting_method,
        "votes": (item.votes_yes, item.votes_no, item.votes_abstain),
        "resolutionNumber": item.resolution_number or "",
        "implementationStatus": item.implementation_status or "",
    }
    try:
        consultation = item.consultation
    except ObjectDoesNotExist:
        consultation = None
    return AgendaItemState(
        id=item.pk,
        meeting_id=item.meeting_id,
        published=bool(is_published(item)),
        fields=fields,
        paper_id=item.paper_id,
        paper_published=bool(paper is not None and is_published(paper)),
        decision=decision,
        consultation_id=consultation.pk if consultation is not None else None,
        implementation_public=bool(getattr(item, "implementation_public", False)),
    )


@dataclass(frozen=True)
class PaperState:
    """Eine Vorlage, wie das kanonische Modell sie sieht."""

    id: uuid.UUID
    #: Liefert die Schnittstelle die Vorlage aus (öffentlich und freigegeben)?
    published: bool
    fields: Mapping[str, Any]
    #: Einreichung, aus der die Vorlage entstand (Kennung in Session; nur in ``ris.paper.created``)
    submission_id: uuid.UUID | None = None

    def public_value(self, name: str) -> Any:
        return self.fields.get(name) if self.published and name in PAPER_PUBLIC_FIELDS else None

    @property
    def withdrawal_reason(self) -> str:
        """Grund, wenn die Vorlage nicht (mehr) veröffentlicht ist: zurück in Entwurf/Prüfung oder nichtöffentlich."""
        return retraction.REASON_WITHDRAWN if self.fields.get("public") else retraction.REASON_NOT_PUBLIC


@dataclass(frozen=True)
class ConsultationState:
    """Eine Station der Beratungsfolge; so öffentlich wie ihre Vorlage."""

    id: uuid.UUID
    paper_id: uuid.UUID
    published: bool
    fields: Mapping[str, Any]
    #: Sind Zielsitzung bzw. Tagesordnungspunkt öffentlich? Sonst nennt die Schnittstelle sie nicht
    meeting_public: bool = False
    item_public: bool = False
    #: Ist die Vorlage als öffentlich gekennzeichnet (auch wenn sie im Entwurf noch nicht veröffentlicht ist)?
    paper_public: bool = False

    @property
    def withdrawal_reason(self) -> str:
        """Grund der Rücknahme: wie die Vorlage – zurück im Entwurf (``zurueckgenommen``) oder nichtöffentlich."""
        return retraction.REASON_WITHDRAWN if self.paper_public else retraction.REASON_NOT_PUBLIC

    @property
    def non_public_station(self) -> bool:
        """Station in einer nichtöffentlichen Sitzung bzw. auf einem nichtöffentlichen TOP (nur „wird beraten“)."""
        return (self.fields.get("meeting") is not None and not self.meeting_public) or (
            self.fields.get("agendaItem") is not None and not self.item_public
        )

    def public_value(self, name: str) -> Any:
        if not self.published or name == "result":
            return None
        if name == "meeting":
            return self.fields.get("meeting") if self.meeting_public else None
        if name == "agendaItem":
            return self.fields.get("agendaItem") if self.item_public else None
        if name in CONSULTATION_STATION_FIELDS and self.non_public_station:
            return None
        return self.fields.get(name)


@dataclass(frozen=True)
class FileState:
    """Eine Anlage an einer Vorlage, Sitzung oder einem Tagesordnungspunkt."""

    id: uuid.UUID
    published: bool
    fields: Mapping[str, Any]
    #: Sind die Objekte, an denen die Datei hängt, veröffentlicht? Nur sie nennt ein öffentliches Ereignis
    paper_published: bool = False
    meeting_public: bool = False
    item_public: bool = False
    #: Grund, wenn die Datei nicht veröffentlicht ist (ihre Vorlage zurück im Entwurf oder nichtöffentlich)
    withdrawal_reason: str = retraction.REASON_NOT_PUBLIC

    def public_ref(self, name: str) -> Any:
        flags = {"paper": self.paper_published, "meeting": self.meeting_public, "agendaItem": self.item_public}
        return self.fields.get(name) if self.published and flags.get(name, False) else None

    def public_value(self, name: str) -> Any:
        if not self.published:
            return None
        if name in ("paper", "meeting", "agendaItem"):
            return self.public_ref(name)
        return self.fields.get(name)


def paper_state(paper: Any, *, is_published: Callable[[Any], bool]) -> PaperState:
    """Zustand einer Vorlage (``SessionPaper``)."""
    fields = {
        "name": paper.name,
        "reference": paper.reference or "",
        "date": paper.date,
        "paperType": paper.paper_type,
        "originatorPerson": paper.originator_person_id,
        "originatorOrganization": paper.originator_organization_id,
        "underDirectionOf": paper.main_organization_id,
        "status": paper.status,
        "public": bool(paper.is_public),
        "mainText": paper.main_text,
        "resolutionText": paper.resolution_text,
    }
    return PaperState(
        id=paper.pk,
        published=bool(is_published(paper)),
        fields=fields,
        submission_id=getattr(paper, "source_application_id", None),
    )


def _item_public(item: Any) -> bool:
    return bool(item is not None and item.is_public and item.meeting.is_public)


def consultation_state(consultation: Any, *, is_published: Callable[[Any], bool]) -> ConsultationState:
    """Zustand einer Beratung (``SessionConsultation`` mit geladener Vorlage, Sitzung und TOP samt Sitzung)."""
    meeting = consultation.meeting if consultation.meeting_id else None
    item = consultation.agenda_item if consultation.agenda_item_id else None
    fields = {
        "organization": consultation.organization_id,
        "meeting": consultation.meeting_id,
        "agendaItem": consultation.agenda_item_id,
        "role": consultation.role,
        "authoritative": bool(consultation.authoritative),
        "order": consultation.order,
        "result": getattr(consultation, "result", "") or "",
    }
    return ConsultationState(
        id=consultation.pk,
        paper_id=consultation.paper_id,
        published=bool(is_published(consultation.paper)),
        fields=fields,
        meeting_public=bool(meeting is not None and meeting.is_public),
        item_public=_item_public(item),
        paper_public=bool(consultation.paper.is_public),
    )


def file_state(file_obj: Any, *, is_published: Callable[[Any], bool]) -> FileState:
    """Zustand einer Anlage (``SessionFile`` mit geladener Vorlage, Sitzung und TOP samt Sitzung)."""
    paper = file_obj.paper if file_obj.paper_id else None
    meeting = file_obj.meeting if file_obj.meeting_id else None
    item = file_obj.agenda_item if file_obj.agenda_item_id else None
    stored = str(file_obj.file.name or "") if file_obj.file else ""
    fields = {
        "name": file_obj.name,
        "version": (file_obj.version, stored),
        "paper": file_obj.paper_id,
        "meeting": file_obj.meeting_id,
        "agendaItem": file_obj.agenda_item_id,
    }
    paper_published = bool(paper is not None and is_published(paper))
    # Öffentliche Anlage an einer öffentlichen Vorlage, die zurück im Entwurf ist: mit ihr zurückgenommen
    withdrawn = bool(file_obj.is_public and paper is not None and paper.is_public and not paper_published)
    return FileState(
        id=file_obj.pk,
        published=bool(is_published(file_obj)),
        fields=fields,
        paper_published=paper_published,
        meeting_public=bool(meeting is not None and meeting.is_public),
        item_public=_item_public(item),
        withdrawal_reason=retraction.REASON_WITHDRAWN if withdrawn else retraction.REASON_NOT_PUBLIC,
    )


@dataclass(frozen=True)
class ProtocolState:
    """Die Niederschrift einer Sitzung: Bearbeitungsstand, Genehmigung und öffentliche Fassung."""

    id: uuid.UUID
    meeting_id: uuid.UUID
    status: str
    #: Öffentliche Fassung (Datei an der Sitzung) – nur bei veröffentlichter Niederschrift einer öffentlichen Sitzung
    file_id: uuid.UUID | None = None
    file_created_at: Any = None
    #: Sitzung, in der genehmigt wurde (Genehmigung in der Folgesitzung); sonst ohne Genehmigungsschritt
    approval_meeting_id: uuid.UUID | None = None
    #: Zuletzt übernommene Berichtigung: Entsteht danach eine neue Fassung, ist sie berichtigt, sonst erneuert
    last_correction_at: Any = None

    @property
    def approved(self) -> bool:
        return self.status in ("approved", "published")

    @property
    def published(self) -> bool:
        return self.status == "published" and self.file_id is not None


def protocol_state(protocol: Any) -> ProtocolState:
    """Zustand einer Niederschrift (``SessionProtocol``); ``last_correction_at`` setzt der Aufrufer, wenn bekannt."""
    public_file = protocol.public_file if protocol.public_file_id else None
    return ProtocolState(
        id=protocol.pk,
        meeting_id=protocol.meeting_id,
        status=protocol.status,
        file_id=protocol.public_file_id,
        file_created_at=public_file.created_at if public_file is not None else None,
        approval_meeting_id=protocol.approval_meeting_id,
        last_correction_at=getattr(protocol, "last_correction_at", None),
    )


def _deleting(draft: Draft) -> Draft:
    """Derselbe Entwurf mit Operation ``delete`` (Entfernen ohne öffentliche Rücknahme)."""
    return Draft(
        draft.type, draft.aggregate_type, draft.aggregate_id, draft.visibility, Operation.DELETE, draft.payload
    )


def _changed(names: Iterable[str], before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    return [name for name in names if before.get(name) != after.get(name)]


# =============================================================================
# Ereignisse eines Mandanten
# =============================================================================


@dataclass
class SessionEvents:
    """
    Ereignisse eines Session-Mandanten: kanonische Kennungen, Entwürfe aus Zuständen und das Schreiben.

    ``uris`` sind die Adressen der Session-Schnittstelle des Mandanten (``SessionUris`` mit der festgeschriebenen
    Basis der Kennungen).
    """

    tenant_id: uuid.UUID
    uris: SessionUris
    _refs: dict[tuple[str, Any], uuid.UUID] = field(default_factory=dict, repr=False)

    @cached_property
    def body_id(self) -> uuid.UUID:
        """Kommune im Journal: kanonische Kennung des Body der Schnittstelle (wie der Änderungsfeed sie liest)."""
        return self.uris.canonical_id(self.uris.body())

    def ref(self, kind: str, pk: Any) -> uuid.UUID:
        """Kanonische Kennung des Session-Objekts ``pk`` der Art ``kind`` (``meeting``, ``agendaitem`` …)."""
        key = (kind, pk)
        found = self._refs.get(key)
        if found is None:
            found = self._refs[key] = self.uris.canonical_id(self.uris.obj(kind, pk))
        return found

    # -- Sitzung -----------------------------------------------------------------------------------

    def meeting_drafts(self, before: MeetingState | None, after: MeetingState | None) -> list[Draft]:
        """Ereignisse zur Änderung einer Sitzung (``before``/``after`` ``None``: neu bzw. gelöscht)."""
        if before is None and after is None:
            return []
        if before is None:
            assert after is not None
            visibility = Visibility.OEFFENTLICH if after.visible else Visibility.NICHTOEFFENTLICH
            return [self._scheduled(after, visibility)]
        meeting = self.ref("meeting", before.id)
        if after is None:
            # Sitzungen löscht Session nur im Admin; wer sie öffentlich kannte, erfährt die Rücknahme
            if not before.visible:
                return []
            return [
                *self._retract("Meeting", meeting, retraction.REASON_DELETED_AT_SOURCE),
                *self._location_gone(before),
            ]

        drafts: list[Draft] = []
        all_changed = _changed(MEETING_FIELDS, before.fields, after.fields)
        if before.publicity != after.publicity:
            all_changed.append(PUBLIC)
        if not before.visible and after.visible:
            # Für die Öffentlichkeit ist die Sitzung neu; die übrigen Empfänger lesen sie darauf ebenfalls neu
            return [self._scheduled(after, Visibility.OEFFENTLICH)]
        if before.visible and not after.visible:
            drafts.extend(self._retract("Meeting", meeting, retraction.REASON_NOT_PUBLIC))
            drafts.extend(self._location_gone(before, reason=retraction.REASON_NOT_PUBLIC))
            drafts.append(self._meeting_changed(after, all_changed, Visibility.NICHTOEFFENTLICH))
            return drafts
        if not after.visible:
            if all_changed:
                drafts.append(self._meeting_changed(after, all_changed, Visibility.NICHTOEFFENTLICH))
            return drafts

        # Sichtbar vorher und nachher: Was sieht die Öffentlichkeit anders?
        public = [name for name in MEETING_FIELDS if before.public_value(name) != after.public_value(name)]
        internal = [name for name in all_changed if name not in public]
        if public:
            drafts.append(self._meeting_changed(after, public, Visibility.OEFFENTLICH))
        if internal:
            drafts.append(self._meeting_changed(after, internal, Visibility.NICHTOEFFENTLICH))
        if before.public_value("location") is not None and after.public_value("location") is None:
            reason = retraction.REASON_NOT_PUBLIC if after.publicity != FULL else retraction.REASON_DELETED_AT_SOURCE
            drafts.extend(self._location_gone(before, reason=reason))
        return drafts

    def _scheduled(self, state: MeetingState, visibility: str) -> Draft:
        payload: dict[str, Any] = {"meeting": str(self.ref("meeting", state.id))}
        if state.organizations:
            payload["organizations"] = self._organizations(state)
        return Draft(MEETING_SCHEDULED, "Meeting", self.ref("meeting", state.id), visibility, Operation.UPSERT, payload)

    def _meeting_changed(self, state: MeetingState, changed: list[str], visibility: str) -> Draft:
        payload: dict[str, Any] = {"meeting": str(self.ref("meeting", state.id)), "changed": changed}
        if "cancelled" in changed:
            payload["cancelled"] = bool(state.fields.get("cancelled"))
        if "organization" in changed and state.organizations:
            payload["organizations"] = self._organizations(state)
        return Draft(MEETING_CHANGED, "Meeting", self.ref("meeting", state.id), visibility, Operation.UPSERT, payload)

    def _organizations(self, state: MeetingState) -> list[str]:
        # Gremien sind in der Schnittstelle immer öffentlich (``visible_organizations``)
        return list(dict.fromkeys(str(self.ref("organization", org)) for org in state.organizations))

    def _location_gone(self, before: MeetingState, *, reason: str = retraction.REASON_DELETED_AT_SOURCE) -> list[Draft]:
        """Der Ort gehört zur Sitzung und trägt deren Kennung; war er öffentlich, wird er mit zurückgenommen."""
        if before.public_value("location") is None:
            return []
        return self._retract("Location", self.ref("location", before.id), reason)

    @staticmethod
    def _retract(aggregate_type: str, object_id: uuid.UUID, reason: str) -> list[Draft]:
        return retraction.drafts(aggregate_type, object_id, reason)

    # -- Tagesordnung ------------------------------------------------------------------------------

    def agenda_drafts(
        self, before: Mapping[uuid.UUID, AgendaItemState], after: Mapping[uuid.UUID, AgendaItemState]
    ) -> list[Draft]:
        """Ereignisse zur Änderung einer oder mehrerer Tagesordnungen, in der Reihenfolge der Tagesordnung."""

        def position(key: uuid.UUID) -> tuple[int, int, str]:
            state = after.get(key) or before[key]
            return (0 if key in after else 1, int(state.fields.get("order") or 0), str(key))

        drafts: list[Draft] = []
        for key in sorted(set(before) | set(after), key=position):
            drafts.extend(self._agenda_item(before.get(key), after.get(key)))
            drafts.extend(self._decision(before.get(key), after.get(key)))
        return drafts

    def voting_id(self, item_pk: Any) -> uuid.UUID:
        """
        Kennung der Abstimmung zu einem TOP (Erweiterung des Modells): Session führt je TOP eine Abstimmung; ihre
        Kennung bildet sich wie jede kanonische aus der Adresse des TOP mit dem Zusatz ``voting``.
        """
        return self.uris.canonical_id(f"{self.uris.obj('agendaitem', item_pk)}voting")

    def _decision(self, before: AgendaItemState | None, after: AgendaItemState | None) -> list[Draft]:
        """
        Beschlussfassung am TOP: Abstimmung (``ris.voting.recorded``), Beschluss bzw. Beschlussnummer
        (``ris.resolution.adopted``), Umsetzung (``ris.resolution.implementation_changed``) und die Rücknahme
        eines festgestellten Ergebnisses.
        """
        if before is None or after is None:
            # Ein neuer TOP trägt noch keinen Beschluss; ein gelöschter meldet seine Rücknahme selbst
            return []
        old = before.decision
        new = after.decision
        if not new or old == new:
            return []
        result_old = old.get("result", PENDING)
        result_new = new.get("result", PENDING)
        public = after.published
        visibility = Visibility.OEFFENTLICH if public else Visibility.NICHTOEFFENTLICH
        item = self.ref("agendaitem", after.id)
        drafts: list[Draft] = []
        base: dict[str, Any] = {"agenda_item": str(item), "meeting": str(self.ref("meeting", after.meeting_id))}
        paper = after.public_value("consultation") if public else after.paper_id
        if paper is not None:
            base["paper"] = str(self.ref("paper", paper))

        if result_new != PENDING:
            vote_changed = any(old.get(name) != new.get(name) for name in ("result", "votingMethod", "votes"))
            if result_new in VOTED_RESULTS and vote_changed:
                payload = {
                    "voting": str(self.voting_id(after.id)),
                    **base,
                    "method": new.get("votingMethod") or "summary",
                    "result": result_new,
                }
                drafts.append(
                    Draft(VOTING_RECORDED, "Voting", self.voting_id(after.id), visibility, Operation.UPSERT, payload)
                )
            changed = [name for name in ("result", "resolutionNumber") if old.get(name) != new.get(name)]
            if changed:
                payload = {**base, "result": result_new, "changed": changed}
                consultation = after.consultation_id
                if consultation is not None and (not public or after.paper_published):
                    payload["consultation"] = str(self.ref("consultation", consultation))
                drafts.append(Draft(RESOLUTION_ADOPTED, "AgendaItem", item, visibility, Operation.UPSERT, payload))
        elif result_old != PENDING:
            # Rücknahme eines festgestellten Ergebnisses: Wer die Abstimmung öffentlich kannte, erfährt sie als
            # Rücknahme; der TOP selbst hat sich geändert (Ergebnis offen)
            if before.published and result_old in VOTED_RESULTS:
                drafts.extend(retraction.drafts("Voting", self.voting_id(after.id), retraction.REASON_WITHDRAWN))
            changed = [name for name in ("result", "resolutionNumber") if old.get(name) != new.get(name)]
            drafts.append(self._item_draft(after, "changed", changed, public))

        status_old, status_new = old.get("implementationStatus", ""), new.get("implementationStatus", "")
        if status_new != status_old and status_new in IMPLEMENTATION_STATUSES:
            if public and after.implementation_public:
                payload = {"agenda_item": str(item), "status": status_new}
                if status_old in IMPLEMENTATION_STATUSES:
                    payload["previous_status"] = status_old
                if "paper" in base:
                    payload["paper"] = base["paper"]
                drafts.append(
                    Draft(IMPLEMENTATION_CHANGED, "AgendaItem", item, Visibility.OEFFENTLICH, Operation.UPSERT, payload)
                )
            else:
                drafts.append(self._item_draft(after, "changed", ["implementationStatus"], False))
        return drafts

    # -- Niederschrift -----------------------------------------------------------------------------

    def protocol_drafts(
        self, before: ProtocolState | None, after: ProtocolState | None, *, meeting_full: bool
    ) -> list[Draft]:
        """
        Niederschrift: Entwurf und Prüfung (``ris.meeting.changed`` mit ``protocol``, intern), Genehmigung
        (``ris.protocol.approved``, intern), Veröffentlichung, Erneuerung und Berichtigung der öffentlichen Fassung
        (``ris.protocol.published``) und ihre Rücknahme (``ris.object.depublished`` der Datei mit Grund).

        ``meeting_full``: Ist die Sitzung öffentlich (sonst gibt es keine öffentliche Fassung und nichts Öffentliches)?
        """
        if after is None:
            if before is not None and before.published and before.file_id is not None:
                return self._results_protocol_gone(before, meeting_full)
            return []
        meeting = self.ref("meeting", after.meeting_id)
        drafts: list[Draft] = []
        if before is None or before.status != after.status:
            if not after.approved:
                changed = {"meeting": str(meeting), "changed": [PROTOCOL]}
                drafts.append(
                    Draft(MEETING_CHANGED, "Meeting", meeting, Visibility.NICHTOEFFENTLICH, Operation.UPSERT, changed)
                )
            elif before is None or not before.approved:
                payload: dict[str, Any] = {"protocol": str(after.id), "meeting": str(meeting)}
                if after.approval_meeting_id is not None:
                    payload["mode"] = "follow_up"
                    payload["approved_in"] = str(self.ref("meeting", after.approval_meeting_id))
                else:
                    payload["mode"] = "direct"
                drafts.append(
                    Draft(PROTOCOL_APPROVED, "Meeting", meeting, Visibility.NICHTOEFFENTLICH, Operation.UPSERT, payload)
                )
        was = before is not None and before.published
        if after.published and after.file_id is not None and meeting_full:
            if not was:
                change = "published"
            elif before is not None and before.file_id != after.file_id:
                corrected = (
                    after.last_correction_at is not None
                    and before.file_created_at is not None
                    and after.last_correction_at > before.file_created_at
                )
                change = "corrected" if corrected else "renewed"
            else:
                return drafts
            payload = {
                "protocol": str(after.id),
                "meeting": str(meeting),
                "change": change,
                "file": str(self.ref("file", after.file_id)),
            }
            drafts.append(
                Draft(PROTOCOL_PUBLISHED, "Meeting", meeting, Visibility.OEFFENTLICH, Operation.UPSERT, payload)
            )
            if was and before is not None and before.file_id is not None and before.file_id != after.file_id:
                # Die bisherige Fassung ist durch die neue ersetzt
                drafts.extend(
                    retraction.drafts("File", self.ref("file", before.file_id), retraction.REASON_DELETED_AT_SOURCE)
                )
        elif was and before is not None:
            drafts.extend(self._results_protocol_gone(before, meeting_full))
        return drafts

    def _results_protocol_gone(self, before: ProtocolState, meeting_full: bool) -> list[Draft]:
        """Öffentliche Fassung zurückgenommen: Rücknahme der Datei, die Sitzung zeigt kein Ergebnisprotokoll mehr."""
        assert before.file_id is not None
        drafts = retraction.drafts("File", self.ref("file", before.file_id), retraction.REASON_WITHDRAWN)
        meeting = self.ref("meeting", before.meeting_id)
        visibility = Visibility.OEFFENTLICH if meeting_full else Visibility.NICHTOEFFENTLICH
        payload = {"meeting": str(meeting), "changed": [RESULTS_PROTOCOL]}
        drafts.append(Draft(MEETING_CHANGED, "Meeting", meeting, visibility, Operation.UPSERT, payload))
        return drafts

    def _agenda_item(self, before: AgendaItemState | None, after: AgendaItemState | None) -> list[Draft]:
        if before is None:
            assert after is not None
            return [self._item_draft(after, "added", None, after.published)]
        item = self.ref("agendaitem", before.id)
        if after is None:
            return retraction.drafts(
                "AgendaItem",
                item,
                retraction.REASON_DELETED_AT_SOURCE,
                public=before.published,
                meeting_id=self.ref("meeting", before.meeting_id),
            )

        changed = _changed(AGENDA_ITEM_FIELDS, before.fields, after.fields)
        moved = before.meeting_id != after.meeting_id
        # Auch ohne eigene Änderung kann sich die öffentliche Sicht ändern (die Vorlage wird (un)veröffentlicht)
        public = [name for name in AGENDA_ITEM_FIELDS if before.public_value(name) != after.public_value(name)]
        if not changed and not moved and not public and before.published == after.published:
            return []
        if moved:
            change = "moved"
        elif after.withdrawn and not before.withdrawn:
            change = "withdrawn"
        else:
            change = "changed"

        if not before.published and after.published:
            # Für die Öffentlichkeit ist der Punkt neu; die übrigen Empfänger lesen ihn darauf ebenfalls neu
            return [self._item_draft(after, "added", None, True)]
        if before.published and not after.published:
            drafts = retraction.drafts("AgendaItem", item, retraction.REASON_NOT_PUBLIC)
            if changed or moved:
                drafts.append(self._item_draft(after, change, changed or None, False, previous=before, moved=moved))
            return drafts
        if after.published:
            internal = [name for name in changed if name not in public]
            drafts = []
            if public or moved:
                drafts.append(self._item_draft(after, change, public or None, True, previous=before, moved=moved))
            if internal:
                drafts.append(self._item_draft(after, "changed", internal, False))
            return drafts
        return [self._item_draft(after, change, changed or None, False, previous=before, moved=moved)]

    def _item_draft(
        self,
        state: AgendaItemState,
        change: str,
        changed: list[str] | None,
        public: bool,
        *,
        previous: AgendaItemState | None = None,
        moved: bool = False,
    ) -> Draft:
        payload: dict[str, Any] = {
            "agenda_item": str(self.ref("agendaitem", state.id)),
            "meeting": str(self.ref("meeting", state.meeting_id)),
            "change": change,
        }
        if changed and change != "added":
            payload["changed"] = changed
        if moved and previous is not None:
            payload["previous_meeting"] = str(self.ref("meeting", previous.meeting_id))
        paper = state.public_value("consultation") if public else state.paper_id
        if paper is not None:
            payload["paper"] = str(self.ref("paper", paper))
        visibility = Visibility.OEFFENTLICH if public else Visibility.NICHTOEFFENTLICH
        return Draft(
            AGENDA_ITEM_CHANGED, "AgendaItem", self.ref("agendaitem", state.id), visibility, Operation.UPSERT, payload
        )

    # -- Vorlage -----------------------------------------------------------------------------------

    def paper_drafts(self, before: PaperState | None, after: PaperState | None) -> list[Draft]:
        """Ereignisse zur Änderung einer Vorlage (``before``/``after`` ``None``: neu bzw. gelöscht)."""
        if before is None and after is None:
            return []
        if before is None:
            assert after is not None
            paper = self.ref("paper", after.id)
            payload: dict[str, Any] = {"paper": str(paper)}
            if after.submission_id is not None:
                payload["submission"] = str(after.submission_id)
            drafts = [Draft(PAPER_CREATED, "Paper", paper, Visibility.NICHTOEFFENTLICH, Operation.UPSERT, payload)]
            if after.published:
                drafts.append(self._released(after))
            return drafts
        paper = self.ref("paper", before.id)
        if after is None:
            if not before.published:
                return []
            return retraction.drafts("Paper", paper, retraction.REASON_DELETED_AT_SOURCE)

        changed = _changed(PAPER_FIELDS, before.fields, after.fields)
        if not before.published and after.published:
            # Freigegeben bzw. veröffentlicht: für die Öffentlichkeit neu
            return [self._released(after)]
        if before.published and not after.published:
            drafts = retraction.drafts("Paper", paper, after.withdrawal_reason)
            if changed:
                drafts.append(self._paper_changed(after, changed, Visibility.NICHTOEFFENTLICH))
            return drafts
        if not after.published:
            return [self._paper_changed(after, changed, Visibility.NICHTOEFFENTLICH)] if changed else []
        public = [name for name in PAPER_PUBLIC_FIELDS if before.public_value(name) != after.public_value(name)]
        internal = [name for name in changed if name not in public]
        drafts = []
        if public:
            drafts.append(self._paper_changed(after, public, Visibility.OEFFENTLICH))
        if internal:
            drafts.append(self._paper_changed(after, internal, Visibility.NICHTOEFFENTLICH))
        return drafts

    def _released(self, state: PaperState) -> Draft:
        # Öffentlich: ohne die Einreichung, aus der die Vorlage entstand (Vertrag ris.paper.released)
        paper = self.ref("paper", state.id)
        return Draft(PAPER_RELEASED, "Paper", paper, Visibility.OEFFENTLICH, Operation.UPSERT, {"paper": str(paper)})

    def _paper_changed(self, state: PaperState, changed: list[str], visibility: str) -> Draft:
        paper = self.ref("paper", state.id)
        payload = {"paper": str(paper), "changed": changed}
        return Draft(PAPER_CHANGED, "Paper", paper, visibility, Operation.UPSERT, payload)

    def papers_drafts(
        self, before: Mapping[uuid.UUID, PaperState | None], after: Mapping[uuid.UUID, PaperState]
    ) -> list[Draft]:
        """Ereignisse zu mehreren Vorlagen, in der Reihenfolge, in der sie beobachtet wurden."""
        drafts: list[Draft] = []
        for key, state in before.items():
            drafts.extend(self.paper_drafts(state, after.get(key)))
        return drafts

    # -- Beratungsfolge ----------------------------------------------------------------------------

    def consultation_drafts(
        self, before: Mapping[uuid.UUID, ConsultationState], after: Mapping[uuid.UUID, ConsultationState]
    ) -> list[Draft]:
        """Ereignisse zur Beratungsfolge, je Vorlage in der Reihenfolge der Stationen."""

        def position(key: uuid.UUID) -> tuple[str, int, int, str]:
            state = after.get(key) or before[key]
            return (str(state.paper_id), 0 if key in after else 1, int(state.fields.get("order") or 0), str(key))

        drafts: list[Draft] = []
        for key in sorted(set(before) | set(after), key=position):
            drafts.extend(self._consultation(before.get(key), after.get(key)))
        return drafts

    def _consultation(self, before: ConsultationState | None, after: ConsultationState | None) -> list[Draft]:
        if before is None:
            assert after is not None
            return [self._consultation_draft(after, "added", None, after.published)]
        consultation = self.ref("consultation", before.id)
        if after is None:
            if before.published:
                return retraction.drafts("Consultation", consultation, retraction.REASON_DELETED_AT_SOURCE)
            return [_deleting(self._consultation_draft(before, "removed", None, False))]

        changed = _changed(CONSULTATION_FIELDS, before.fields, after.fields)
        public = [name for name in CONSULTATION_FIELDS if before.public_value(name) != after.public_value(name)]
        if not changed and not public and before.published == after.published:
            return []
        scheduled = before.fields.get("agendaItem") is None and after.fields.get("agendaItem") is not None
        change = "scheduled" if scheduled else "changed"
        if not before.published and after.published:
            return [self._consultation_draft(after, "added", None, True)]
        if before.published and not after.published:
            # Die Vorlage wurde zurückgenommen bzw. nichtöffentlich: die Station mit ihr
            drafts = retraction.drafts("Consultation", consultation, after.withdrawal_reason)
            if changed:
                drafts.append(self._consultation_draft(after, change, changed, False))
            return drafts
        if not after.published:
            return [self._consultation_draft(after, change, changed, False)] if changed else []
        internal = [name for name in changed if name not in public]
        drafts = []
        if public:
            public_change = "scheduled" if "agendaItem" in public and after.item_public and scheduled else "changed"
            drafts.append(self._consultation_draft(after, public_change, public, True))
        if internal:
            drafts.append(self._consultation_draft(after, change, internal, False))
        return drafts

    def _consultation_draft(
        self, state: ConsultationState, change: str, changed: list[str] | None, public: bool
    ) -> Draft:
        consultation = self.ref("consultation", state.id)
        payload: dict[str, Any] = {
            "consultation": str(consultation),
            "paper": str(self.ref("paper", state.paper_id)),
            "change": change,
        }
        if changed and change != "added":
            payload["changed"] = changed
        for name, kind, key in (
            ("organization", "organization", "organization"),
            ("meeting", "meeting", "meeting"),
            ("agendaItem", "agendaitem", "agenda_item"),
        ):
            found = state.public_value(name) if public else state.fields.get(name)
            if found is not None:
                payload[key] = str(self.ref(kind, found))
        visibility = Visibility.OEFFENTLICH if public else Visibility.NICHTOEFFENTLICH
        return Draft(CONSULTATION_CHANGED, "Consultation", consultation, visibility, Operation.UPSERT, payload)

    # -- Anlagen -----------------------------------------------------------------------------------

    def file_drafts(self, before: Mapping[uuid.UUID, FileState], after: Mapping[uuid.UUID, FileState]) -> list[Draft]:
        """Ereignisse zu Anlagen (nach Kennung geordnet; Anlagen haben keine eigene Reihenfolge)."""
        drafts: list[Draft] = []
        for key in sorted(set(before) | set(after), key=str):
            drafts.extend(self._file(before.get(key), after.get(key)))
        return drafts

    def _file(self, before: FileState | None, after: FileState | None) -> list[Draft]:
        if before is None:
            assert after is not None
            return [self._file_draft(after, "added", after.published)]
        file_id = self.ref("file", before.id)
        if after is None:
            if before.published:
                return retraction.drafts("File", file_id, retraction.REASON_DELETED_AT_SOURCE)
            return [_deleting(self._file_draft(before, "removed", False))]
        if not before.published and after.published:
            return [self._file_draft(after, "added", True)]
        if before.published and not after.published:
            # Wer die Anlage öffentlich kannte, erfährt die Rücknahme; Anlagen haben kein „geändert“ ohne Inhalt
            return retraction.drafts("File", file_id, after.withdrawal_reason)
        changed = _changed(FILE_FIELDS, before.fields, after.fields)
        if not after.published:
            return [self._file_draft(after, self._file_change(changed), False)] if changed else []
        public = [name for name in FILE_FIELDS if before.public_value(name) != after.public_value(name)]
        internal = [name for name in changed if name not in public]
        drafts = []
        if public:
            drafts.append(self._file_draft(after, self._file_change(public), True))
        if internal:
            drafts.append(self._file_draft(after, self._file_change(internal), False))
        return drafts

    @staticmethod
    def _file_change(names: list[str]) -> str:
        if "version" in names:
            return "replaced"
        if "name" in names:
            return "renamed"
        # Neue Zugehörigkeit: Die Datei hängt jetzt (auch) woanders
        return "added"

    def _file_draft(self, state: FileState, change: str, public: bool) -> Draft:
        file_id = self.ref("file", state.id)
        payload: dict[str, Any] = {"file": str(file_id), "change": change}
        for name, kind, key in FILE_REFS:
            found = state.public_ref(name) if public else state.fields.get(name)
            if found is not None:
                payload[key] = str(self.ref(kind, found))
        visibility = Visibility.OEFFENTLICH if public else Visibility.NICHTOEFFENTLICH
        return Draft(FILE_CHANGED, "File", file_id, visibility, Operation.UPSERT, payload)

    # -- Ladung ------------------------------------------------------------------------------------

    def invited_draft(self, meeting_id: uuid.UUID, dispatch_id: uuid.UUID, dispatch_type: str) -> Draft:
        """Ladung, Nachtrag oder Vertretungsanfrage versandt (nur nichtöffentlich: Empfänger sind intern)."""
        if dispatch_type not in DISPATCH_TYPES:
            raise ValueError("Unbekannte Versandart einer Ladung.")
        meeting = self.ref("meeting", meeting_id)
        payload = {"meeting": str(meeting), "dispatch": str(dispatch_id), "dispatch_type": dispatch_type}
        return Draft(MEETING_INVITED, "Meeting", meeting, Visibility.NICHTOEFFENTLICH, Operation.UPSERT, payload)

    # -- Schreiben ---------------------------------------------------------------------------------

    def publish(self, drafts: Iterable[Draft]) -> int:
        """Entwürfe ins Journal schreiben, in der laufenden Transaktion; Zahl der Ereignisse."""
        written = 0
        tenant = tenant_ref("session", self.tenant_id)
        for draft in drafts:
            publish(
                draft.type,
                version=VERSION,
                aggregate=CanonicalRef(draft.aggregate_type, draft.aggregate_id),
                tenant=tenant,
                body_id=self.body_id,
                visibility=draft.visibility,
                operation=draft.operation,
                payload=draft.payload,
            )
            written += 1
        return written

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse aus mandari Session im kanonischen RIS-Modell (Datendrehscheibe, Etappe E4, Issue #533).

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
  ausliefert. Von einer nichtöffentlichen Sitzung mit veröffentlichtem Termin (Issue #757) sind das Name, Zeit,
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

**Reihenfolge** innerhalb einer Änderung: erst die Sitzungen, dann ihre Tagesordnungspunkte (in der Reihenfolge
der Tagesordnung), zuletzt ausdrücklich gemeldete Vorgänge wie die Ladung. Der Sequenzierer übernimmt die
Reihenfolge des Schreibens.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Final

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

#: Felder eines Tagesordnungspunkts im kanonischen Modell (``consultation``: die beratene Vorlage)
AGENDA_ITEM_FIELDS: Final = ("name", "number", "order", "public", "withdrawn", "consultation")

#: Versandarten einer Ladung (Vertrag ``ris.meeting.invited``)
DISPATCH_TYPES: Final = frozenset({"invitation", "supplementary", "substitution"})

#: Feldname „öffentlich“ in den Änderungslisten, wenn sich die Sichtbarkeit einer Sitzung ändert
PUBLIC: Final = "public"


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


def meeting_state(meeting: Any) -> MeetingState:
    """Zustand einer Sitzung (``SessionMeeting``); weitere Gremien am besten vorgeladen (``joint_organizations``)."""
    if meeting.is_public:
        publicity = FULL
    elif getattr(meeting, "date_public", False):
        publicity = DATE_ONLY
    else:
        publicity = HIDDEN
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
    }
    return AgendaItemState(
        id=item.pk,
        meeting_id=item.meeting_id,
        published=bool(is_published(item)),
        fields=fields,
        paper_id=item.paper_id,
        paper_published=bool(paper is not None and is_published(paper)),
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

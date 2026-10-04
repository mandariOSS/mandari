# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session meldet ihre fachlichen Änderungen an die Datendrehscheibe (Etappe E4, Issues #533–#535).

Fachfunktionen legen ``track()`` um ihre Änderung und nennen die betroffenen Objekte, bevor sie sie ändern::

    with hub_events.track(tenant) as tracked:
        tracked.meeting(meeting)
        tracked.agenda(meeting)
        ...  # Sitzung ändern, Tagesordnung neu nummerieren
    # beim Verlassen: Ereignisse aus Zustand vorher/nachher, in derselben Transaktion wie die Änderung

``track`` hält den Zustand vor der Änderung aus der Datenbank fest, liest ihn am Ende des Blocks erneut und
lässt die Drehscheibe daraus die Ereignisse bilden und schreiben (``hub.ris.session_events``): genau eines je
geändertem Objekt und Empfängerkreis, keines für Änderungen, die das kanonische Modell nicht kennt. Ereignisse
entstehen nur hier, aus Fachfunktionen – nie aus Signalen (``docs/adr/20260929-ereignistechnik-postgres.md``).

Ein ``track`` innerhalb eines anderen für denselben Mandanten schließt sich dem äußeren an: Das äußere hält den
früheren Zustand und schreibt am Ende einmal. So meldet etwa „Sitzung anlegen“ mit den Standard-TOPs erst die
Sitzung, dann die Punkte, ohne Doppel. Die laufende Erfassung endet noch in ihrer Transaktion: Rückrufe nach dem
Commit (``transaction.on_commit``) öffnen eine eigene, statt sich der schon geschriebenen anzuschließen.

**Öffentlich** ist nur, was die Session-Schnittstelle des Mandanten ausliefert: Ist sie nicht freigeschaltet
(``SessionTenant.oparl_public_since``, Issue #319) oder der Mandant deaktiviert, gilt alles als nichtöffentlich.

**Schalter** ``SESSION_EVENTS`` (Installation) und ``SessionTenant.hub_events`` (Mandant; leer: wie die
Installation):

- ``aus`` (Standard): nichts, keine zusätzliche Abfrage, keine zusätzliche Transaktion.
- ``schatten``: Ereignisse werden geschrieben und erreichen wie im aktiven Betrieb den Änderungsfeed und alle
  Abonnenten. Anders ist nur der Fehlerfall: Scheitert das Bilden oder Schreiben, bleibt die Änderung bestehen
  (eigener Sicherungspunkt), der Fehler steht im Protokoll (nur Fehlerklasse und Aufrufstellen, keine Inhalte).
  Für den Parallelbetrieb neben den bisherigen Wegen.
- ``aktiv``: Änderung und Ereignisse sind atomar; scheitert ein Ereignis, bleibt auch die Änderung aus.

Ereignisse brauchen den Sequenzierer (Worker); ``EVENTS_WORKER_REQUIRED`` verlangt ihn, sobald die Installation
nicht ``aus`` ist. Wer nur einzelne Mandanten einschaltet, setzt ``EVENTS_WORKER_REQUIRED=true``.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q

from apps.session import oparl_publication
from apps.session.models import (
    SessionAgendaItem,
    SessionConsultation,
    SessionFile,
    SessionMeeting,
    SessionPaper,
    SessionProtocol,
)
from hub.ris.mapping.session import SessionUris
from hub.ris.retraction import Draft
from hub.ris.session_events import (
    AgendaItemState,
    ConsultationState,
    FileState,
    MeetingState,
    PaperState,
    ProtocolState,
    SessionEvents,
    agenda_item_state,
    consultation_state,
    file_state,
    meeting_state,
    paper_state,
    protocol_state,
)

logger = logging.getLogger(__name__)

AUS: Final = "aus"
SCHATTEN: Final = "schatten"
AKTIV: Final = "aktiv"
MODES: Final = (AUS, SCHATTEN, AKTIV)


def installation_mode() -> str:
    """Schalter der Installation (``SESSION_EVENTS``); Unbekanntes gilt als ``aus``."""
    value = str(getattr(settings, "SESSION_EVENTS", AUS) or AUS).strip().lower()
    return value if value in MODES else AUS


def mode(tenant: Any) -> str:
    """Wirksamer Schalter eines Mandanten: eigener Wert, sonst der der Installation."""
    own = str(getattr(tenant, "hub_events", "") or "").strip().lower()
    return own if own in MODES else installation_mode()


def enabled(tenant: Any) -> bool:
    return mode(tenant) != AUS


def events_for(tenant: Any) -> SessionEvents:
    """Ereignisse des Mandanten mit den Adressen seiner OParl-Schnittstelle (Basis der kanonischen Kennungen)."""
    from apps.session.services.insight_service import oparl_id_base, oparl_system_url

    return SessionEvents(tenant_id=tenant.pk, uris=SessionUris(oparl_system_url(tenant), lambda: oparl_id_base(tenant)))


def interface_open(tenant: Any) -> bool:
    """
    Liefert die Session-Schnittstelle des Mandanten aus? Nur dann ist etwas öffentlich.

    Vor der Freischaltung (Issue #319: Einführung, Testdaten, Schulung, Umstieg) und bei deaktiviertem Mandanten
    antwortet jeder Endpunkt mit 404 (``apps.session.api.oparl``); Ereignisse sind dann nur ``nichtoeffentlich``.
    """
    return bool(getattr(tenant, "is_active", True) and getattr(tenant, "oparl_public_since", None) is not None)


# =============================================================================
# Zustände aus der Datenbank
# =============================================================================


class Reader:
    """
    Liest Zustände von Objekten eines Mandanten – nur seine, auch wenn eine Kennung woanders hinzeigt – mit der
    Veröffentlichungsregel seiner Schnittstelle.
    """

    #: Sammlungen, die über einen Bereich beobachtet werden (alle Objekte, die ihn erfüllen, auch neue)
    KINDS: Final = ("item", "consultation", "file")

    def __init__(self, tenant: Any) -> None:
        self.tenant_id = tenant.pk
        self.open = interface_open(tenant)

    def is_published(self, obj: Any) -> bool:
        """Liefert die Schnittstelle das Objekt aus (``oparl_publication``, nur bei freigeschalteter Schnittstelle)?"""
        return self.open and oparl_publication._is_published(obj)

    def implementation_public(self, item: Any) -> bool:
        """
        Ist der Umsetzungsstand des Beschlusses öffentlich? Dieselbe Regel wie die Beschlussseiten im Bürgerportal
        (``decision_tracking``): Opt-in der Verwaltung am Mandanten und am Beschluss, angenommen, nicht abgesetzt,
        öffentlicher TOP einer öffentlichen Sitzung, aktiv veröffentlicht.
        """
        from insight_core.services import decision_tracking

        return self.open and decision_tracking.is_publicly_visible(item)

    def meetings(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, MeetingState]:
        if not ids:
            return {}
        meetings = SessionMeeting.objects.filter(tenant_id=self.tenant_id, pk__in=ids).prefetch_related(
            "joint_organizations"
        )
        return {meeting.pk: meeting_state(meeting, is_published=self.is_published) for meeting in meetings}

    def protocols(self, meeting_ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[ProtocolState, bool]]:
        """Niederschriften der Sitzungen (Schlüssel: Sitzung) und ob die Sitzung öffentlich ist."""
        if not meeting_ids:
            return {}
        protocols = (
            SessionProtocol.objects.filter(meeting__tenant_id=self.tenant_id, meeting_id__in=meeting_ids)
            .select_related("meeting", "public_file")
            .annotate(last_correction_at=Max("corrections__applied_at", filter=Q(corrections__status="applied")))
        )
        return {
            p.meeting_id: (protocol_state(p, interface_open=self.open), self.open and bool(p.meeting.is_public))
            for p in protocols
        }

    def papers(self, ids: set[uuid.UUID]) -> dict[uuid.UUID, PaperState]:
        if not ids:
            return {}
        papers = SessionPaper.objects.filter(tenant_id=self.tenant_id, pk__in=ids)
        return {paper.pk: paper_state(paper, is_published=self.is_published) for paper in papers}

    def load(self, kind: str, scope: Q) -> dict[uuid.UUID, Any]:
        """Zustände der Sammlung ``kind`` (``KINDS``) im Bereich ``scope``."""
        if kind == "item":
            return self._items(scope)
        if kind == "consultation":
            return self._consultations(scope)
        if kind == "file":
            return self._files(scope)
        raise ValueError("Unbekannte Sammlung.")

    def _items(self, scope: Q) -> dict[uuid.UUID, AgendaItemState]:
        items = (
            SessionAgendaItem.objects.filter(meeting__tenant_id=self.tenant_id)
            .filter(scope)
            .select_related("meeting__tenant", "paper", "consultation")
        )
        return {
            item.pk: agenda_item_state(
                item, is_published=self.is_published, implementation_public=self.implementation_public
            )
            for item in items
        }

    def _consultations(self, scope: Q) -> dict[uuid.UUID, ConsultationState]:
        consultations = (
            SessionConsultation.objects.filter(paper__tenant_id=self.tenant_id)
            .filter(scope)
            .select_related("paper", "meeting", "agenda_item__meeting")
        )
        return {c.pk: consultation_state(c, is_published=self.is_published) for c in consultations}

    def _files(self, scope: Q) -> dict[uuid.UUID, FileState]:
        """Anlagen an Vorlagen, Sitzungen und TOPs; ohne die öffentliche Fassung der Niederschrift (eigene Meldung)."""
        files = (
            SessionFile.objects.filter(tenant_id=self.tenant_id)
            .filter(scope)
            .filter(Q(paper__isnull=False) | Q(meeting__isnull=False) | Q(agenda_item__isnull=False))
            .exclude(public_protocol__isnull=False)
            .select_related("paper", "meeting", "agenda_item__meeting")
        )
        return {f.pk: file_state(f, is_published=self.is_published) for f in files}


# =============================================================================
# Erfassung einer Änderung
# =============================================================================


class Tracker:
    """Betroffene Objekte einer Änderung mit ihrem Zustand davor; schreibt am Ende die Ereignisse."""

    def __init__(self, tenant: Any, *, strict: bool) -> None:
        self.tenant = tenant
        self.tenant_id = tenant.pk
        self.strict = strict
        self.events = events_for(tenant)
        self.reader = Reader(tenant)
        #: Geschrieben (bzw. abgeschlossen): Eine spätere Erfassung schließt sich nicht mehr an
        self.closed = False
        self._meetings: dict[uuid.UUID, MeetingState | None] = {}
        self._papers: dict[uuid.UUID, PaperState | None] = {}
        #: Niederschriften je Sitzung (Zustand davor; ``None``: es gab noch keine)
        self._protocols: dict[uuid.UUID, ProtocolState | None] = {}
        #: je Sammlung: beobachteter Bereich und Zustände davor
        self._scopes: dict[str, Q] = {}
        self._before: dict[str, dict[uuid.UUID, Any]] = {kind: {} for kind in Reader.KINDS}
        self._watched: set[tuple[str, Any]] = set()
        self._explicit: list[Draft] = []
        self._broken = False

    @property
    def active(self) -> bool:
        return not self._broken

    def _guard(self, step: str, work: Callable[[], None]) -> None:
        """Im Schattenbetrieb Fehler abfangen (eigener Sicherungspunkt), sonst durchreichen."""
        if self._broken:
            return
        if self.strict:
            work()
            return
        try:
            with transaction.atomic():
                work()
        except Exception as exc:  # noqa: BLE001 – Schattenbetrieb: die Änderung geht vor
            self._broken = True
            # Fehlerklasse und Aufrufstellen (Datei, Zeile, Funktion), nie die Meldung oder Quelltext: Datenbankfehler
            # nennen in der Meldung Werte der Zeile
            stelle = " < ".join(
                f"{frame.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{frame.lineno} {frame.name}"
                for frame in reversed(traceback.extract_tb(exc.__traceback__)[-3:])
            )
            logger.warning(
                "Session-Ereignisse im Schattenbetrieb übersprungen (%s, %s) bei %s", step, type(exc).__name__, stelle
            )

    def _scope(self, kind: str, scope: Q) -> None:
        """Alle Objekte der Sammlung ``kind`` im Bereich ``scope`` beobachten (Zustand jetzt, vor der Änderung)."""
        for key, state in self.reader.load(kind, scope).items():
            self._before[kind].setdefault(key, state)
        self._scopes[kind] = self._scopes[kind] | scope if kind in self._scopes else scope

    def _once(self, label: str, key: Any, step: str, work: Callable[[], None]) -> None:
        if key is None or (label, key) in self._watched:
            return
        self._watched.add((label, key))
        self._guard(step, work)

    def meeting(self, meeting: Any, *, created: bool = False) -> None:
        """
        Sitzung beobachten – vor der Änderung; ``created``: gerade erst angelegt (vorher gab es sie nicht).

        Mit ihr die Anlagen der Sitzung und die Beratungen, die in ihr stattfinden (ihre Öffentlichkeit folgt der
        Sitzung).
        """
        key = meeting.pk

        def read() -> None:
            self._meetings[key] = None if created else self.reader.meetings({key}).get(key)
            self._scope("file", Q(meeting_id=key))
            self._scope("consultation", Q(meeting_id=key))

        self._once("meeting", key, "Sitzung", read)

    def agenda(self, meeting: Any) -> None:
        """Tagesordnung einer Sitzung beobachten (alle Punkte, auch neue und gelöschte, mit Anlagen und Beratungen)."""
        key = meeting.pk

        def read() -> None:
            self._scope("item", Q(meeting_id=key))
            self._scope("file", Q(agenda_item__meeting_id=key))
            self._scope("consultation", Q(agenda_item__meeting_id=key))

        self._once("agenda", key, "Tagesordnung", read)

    def paper(self, paper: Any, *, created: bool = False) -> None:
        """
        Vorlage beobachten – vor der Änderung – samt Beratungsfolge, Anlagen und den TOPs, auf denen sie steht.

        Ohne ``created`` gilt eine Vorlage, die es noch nicht gibt, ebenfalls als neu.
        """
        key = paper.pk

        def read() -> None:
            self._papers[key] = None if created else self.reader.papers({key}).get(key)
            self._scope("consultation", Q(paper_id=key))
            self._scope("file", Q(paper_id=key))
            self._scope("item", Q(paper_id=key))

        self._once("paper", key, "Vorlage", read)

    def protocol(self, meeting: Any) -> None:
        """
        Niederschrift einer Sitzung beobachten – Entwurf, Prüfung, Genehmigung, öffentliche Fassung und ihre
        Rücknahme – samt Tagesordnung (Berichtigungen ändern Ergebnisse und Texte der TOPs).
        """
        key = meeting.pk

        def read() -> None:
            found = self.reader.protocols({key}).get(key)
            self._protocols[key] = found[0] if found is not None else None

        self._once("protocol", key, "Niederschrift", read)
        self.agenda(meeting)

    def file(self, file_obj: Any) -> None:
        """Eine Anlage beobachten (vor dem Hochladen, Ersetzen, Umbenennen oder Löschen)."""
        key = file_obj.pk
        self._once("file", key, "Anlage", lambda: self._scope("file", Q(pk=key)))

    def invited(self, meeting: Any, dispatch: Any) -> None:
        """Versandvorgang einer Ladung melden (nach den Zustandsänderungen)."""

        def build() -> None:
            self._explicit.append(self.events.invited_draft(meeting.pk, dispatch.pk, dispatch.dispatch_type))

        self._guard("Ladung", build)

    def _after(self, kind: str) -> dict[uuid.UUID, Any]:
        """Zustand danach: alles im beobachteten Bereich und alles, was vorher darin lag (gelöscht, verschoben)."""
        if kind not in self._scopes:
            return {}
        return self.reader.load(kind, self._scopes[kind] | Q(pk__in=list(self._before[kind])))

    def flush(self) -> int:
        """Zustand danach lesen, Ereignisse bilden und schreiben (in der laufenden Transaktion)."""
        written = 0

        def write() -> None:
            nonlocal written
            events = self.events
            drafts: list[Draft] = []
            after_meetings = self.reader.meetings(set(self._meetings))
            for key, before in self._meetings.items():
                drafts.extend(events.meeting_drafts(before, after_meetings.get(key)))
            drafts.extend(events.papers_drafts(self._papers, self.reader.papers(set(self._papers))))
            drafts.extend(events.agenda_drafts(self._before["item"], self._after("item")))
            drafts.extend(events.consultation_drafts(self._before["consultation"], self._after("consultation")))
            drafts.extend(events.file_drafts(self._before["file"], self._after("file")))
            after_protocols = self.reader.protocols(set(self._protocols))
            for key, before_protocol in self._protocols.items():
                found = after_protocols.get(key)
                after_protocol, full = found if found is not None else (None, False)
                drafts.extend(events.protocol_drafts(before_protocol, after_protocol, meeting_full=full))
            drafts.extend(self._explicit)
            written = events.publish(drafts)

        self._guard("Schreiben", write)
        return written


#: Laufende Erfassung (je Thread bzw. Kontext): Innere ``track`` schließen sich ihr an
_current: ContextVar[Tracker | None] = ContextVar("session_hub_events", default=None)


class _Off:
    """Ausgeschaltet: nimmt alles an und tut nichts."""

    active = False

    def meeting(self, meeting: Any, *, created: bool = False) -> None:
        return None

    def agenda(self, meeting: Any) -> None:
        return None

    def paper(self, paper: Any, *, created: bool = False) -> None:
        return None

    def file(self, file_obj: Any) -> None:
        return None

    def protocol(self, meeting: Any) -> None:
        return None

    def invited(self, meeting: Any, dispatch: Any) -> None:
        return None


_OFF: Final = _Off()


@contextmanager
def track(tenant: Any) -> Iterator[Tracker | _Off]:
    """
    Änderung an Objekten des Mandanten erfassen und am Ende als Ereignisse melden.

    Ausgeschaltet ein Durchreichen ohne Transaktion; eingeschaltet umschließt der Block eine Transaktion, in der
    am Ende die Ereignisse geschrieben werden.

    Die Erfassung endet, bevor ihre Transaktion endet: Ist diese die äußerste, laufen beim Verlassen die Rückrufe
    nach dem Commit (``transaction.on_commit``). Eine Erfassung darin ist eine neue – sie schlösse sich sonst
    einer an, die schon geschrieben hat, und ihre Änderungen blieben ungemeldet.
    """
    outer = _current.get()
    if outer is not None and not outer.closed and outer.tenant_id == tenant.pk:
        yield outer
        return
    current = mode(tenant)
    if current == AUS:
        yield _OFF
        return
    tracker = Tracker(tenant, strict=current == AKTIV)
    with transaction.atomic():
        token = _current.set(tracker)
        try:
            yield tracker
            tracker.flush()
        finally:
            tracker.closed = True
            _current.reset(token)

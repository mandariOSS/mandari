# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session meldet ihre fachlichen Änderungen an die Datendrehscheibe (Etappe E4, Issue #533).

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
Sitzung, dann die Punkte, ohne Doppel.

**Schalter** ``SESSION_EVENTS`` (Installation) und ``SessionTenant.hub_events`` (Mandant; leer: wie die
Installation):

- ``aus`` (Standard): nichts, keine zusätzliche Abfrage, keine zusätzliche Transaktion.
- ``schatten``: Ereignisse werden geschrieben, aber nichts hängt an ihnen. Scheitert das Bilden oder Schreiben,
  bleibt die Änderung bestehen (eigener Sicherungspunkt), der Fehler steht im Protokoll (nur Fehlerklasse und
  Ereignisart, keine Inhalte). Für den Parallelbetrieb neben den bisherigen Wegen.
- ``aktiv``: Änderung und Ereignisse sind atomar; scheitert ein Ereignis, bleibt auch die Änderung aus.

Ereignisse brauchen den Sequenzierer (Worker); ``EVENTS_WORKER_REQUIRED`` verlangt ihn, sobald die Installation
nicht ``aus`` ist. Wer nur einzelne Mandanten einschaltet, setzt ``EVENTS_WORKER_REQUIRED=true``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final

from django.conf import settings
from django.db import transaction

from apps.session import oparl_publication
from apps.session.models import SessionAgendaItem, SessionMeeting
from hub.ris.mapping.session import SessionUris
from hub.ris.retraction import Draft
from hub.ris.session_events import (
    AgendaItemState,
    MeetingState,
    SessionEvents,
    agenda_item_state,
    meeting_state,
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


# =============================================================================
# Zustände aus der Datenbank
# =============================================================================


def _meeting_states(ids: set[uuid.UUID]) -> dict[uuid.UUID, MeetingState]:
    if not ids:
        return {}
    meetings = SessionMeeting.objects.filter(pk__in=ids).prefetch_related("joint_organizations")
    return {meeting.pk: meeting_state(meeting) for meeting in meetings}


def _agenda_states(meeting_ids: set[uuid.UUID], item_ids: set[uuid.UUID]) -> dict[uuid.UUID, AgendaItemState]:
    """Alle Punkte der Sitzungen ``meeting_ids`` und dazu die Punkte ``item_ids`` (wohin sie auch verschoben sind)."""
    if not meeting_ids and not item_ids:
        return {}
    from django.db.models import Q

    items = SessionAgendaItem.objects.filter(Q(meeting_id__in=meeting_ids) | Q(pk__in=item_ids)).select_related(
        "meeting", "paper"
    )
    return {item.pk: agenda_item_state(item, is_published=oparl_publication._is_published) for item in items}


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
        self._meetings: dict[uuid.UUID, MeetingState | None] = {}
        self._agendas: set[uuid.UUID] = set()
        self._items: dict[uuid.UUID, AgendaItemState] = {}
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
            logger.warning(
                "Session-Ereignisse im Schattenbetrieb übersprungen (%s, %s)", step, type(exc).__name__, exc_info=True
            )

    def meeting(self, meeting: Any, *, created: bool = False) -> None:
        """Sitzung beobachten – vor der Änderung; ``created``: gerade erst angelegt (vorher gab es sie nicht)."""
        key = meeting.pk
        if key is None or key in self._meetings:
            return
        if created:
            self._meetings[key] = None
            return

        def read() -> None:
            self._meetings[key] = _meeting_states({key}).get(key)

        self._guard("Sitzung", read)

    def agenda(self, meeting: Any) -> None:
        """Tagesordnung einer Sitzung beobachten (alle Punkte, auch neue und gelöschte) – vor der Änderung."""
        key = meeting.pk
        if key is None or key in self._agendas:
            return
        self._agendas.add(key)

        def read() -> None:
            for item_id, state in _agenda_states({key}, set()).items():
                self._items.setdefault(item_id, state)

        self._guard("Tagesordnung", read)

    def invited(self, meeting: Any, dispatch: Any) -> None:
        """Versandvorgang einer Ladung melden (nach den Zustandsänderungen)."""

        def build() -> None:
            self._explicit.append(self.events.invited_draft(meeting.pk, dispatch.pk, dispatch.dispatch_type))

        self._guard("Ladung", build)

    def flush(self) -> int:
        """Zustand danach lesen, Ereignisse bilden und schreiben (in der laufenden Transaktion)."""
        written = 0

        def write() -> None:
            nonlocal written
            drafts: list[Draft] = []
            after_meetings = _meeting_states(set(self._meetings))
            for key, before in self._meetings.items():
                drafts.extend(self.events.meeting_drafts(before, after_meetings.get(key)))
            if self._agendas or self._items:
                after_items = _agenda_states(self._agendas, set(self._items))
                drafts.extend(self.events.agenda_drafts(self._items, after_items))
            drafts.extend(self._explicit)
            written = self.events.publish(drafts)

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

    def invited(self, meeting: Any, dispatch: Any) -> None:
        return None


_OFF: Final = _Off()


@contextmanager
def track(tenant: Any) -> Iterator[Tracker | _Off]:
    """
    Änderung an Objekten des Mandanten erfassen und am Ende als Ereignisse melden.

    Ausgeschaltet ein Durchreichen ohne Transaktion; eingeschaltet umschließt der Block eine Transaktion, in der
    am Ende die Ereignisse geschrieben werden.
    """
    outer = _current.get()
    if outer is not None and outer.tenant_id == tenant.pk:
        yield outer
        return
    current = mode(tenant)
    if current == AUS:
        yield _OFF
        return
    tracker = Tracker(tenant, strict=current == AKTIV)
    token = _current.set(tracker)
    try:
        with transaction.atomic():
            yield tracker
            tracker.flush()
    finally:
        _current.reset(token)

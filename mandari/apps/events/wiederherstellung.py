# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Journal und Aufträge nach einer Wiederherstellung (Issue #573, ``docs/BACKUP.md``).

Journal (``events_event``), Abonnements mit Cursor, geparkte Ereignisse, Aufträge und Zeitpläne
liegen in der Datenbank. ``pg_dump`` hält sie transaktionskonsistent fest: Sichten in derselben
Datenbank passen nach dem Einspielen zu ihren Cursorn, unterbrochene Aufträge und Leases laufen nach
ihrer Frist ab (Auftrag 60 s, Lease 30 s). Zwei Dinge liegen außerhalb der Sicherung:

1. **Folgenummern, die schon jemand gesehen hat.** Zwischen Sicherung und Ausfall hat der
   Sequenzierer weitere Nummern vergeben. Der Suchindex (externe Version gleich Folgenummer) und
   Abnehmer des Änderungsfeeds (Cursor) kennen sie. Die Sequenz der Sicherung stünde wieder davor, neue
   Ereignisse bekämen dieselben Nummern: Der Suchindex verwürfe sie als veraltet, ein Abnehmer des
   Feeds überspränge sie. ``raise_sequence`` hebt die Sequenz deshalb um einen Abstand an, **bevor**
   der Sequenzierer wieder läuft. Lücken in der Nummernfolge sind unschädlich
   (``docs/adr/20260929-sequenzierer.md``); der Feed erkennt einen Cursor, dessen Ereignis mit der
   Wiederherstellung verloren ging, und schickt den Abnehmer über den Snapshot neu hinein
   (``hub.api.changes``).
2. **Ziele außerhalb der Datenbank** (Suchindex, Mails, Fremdsysteme) stehen auf einem neueren Stand
   als das Journal. Sie werden neu aufgebaut oder nachgespielt; ``state`` nennt die Abonnements mit
   externem Effekt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from django.db import connection, transaction
from django.db.models import Count, Max, Q
from django.db.models.functions import Now

from .models import SEQUENCE_NAME, Event, Lease, ParkedEvent, ParkedState, Subscription, Task
from .registry import registered
from .sequencer import LEASE_NAME as SEQUENCER_LEASE

#: Abstand, um den die Folgenummer angehoben wird: weit mehr, als zwischen zwei Sicherungen vergeben
#: wird (Erstabgleich der größten Kommune rund 300 000), und doch klein gegen den Wertebereich (2^63)
DEFAULT_GAP: Final = 100_000_000
#: Tabellen, ohne die es nichts zu tun gibt (Sicherung von vor der Ereignistechnik)
_TABELLEN: Final = ("events_event", "events_subscription")


class SequencerRunningError(Exception):
    """Ein Sequenzierer hält eine gültige Lease; er könnte gerade Nummern aus der alten Folge vergeben."""


@dataclass(frozen=True)
class SubscriptionState:
    name: str
    state: str
    cursor: int
    #: im Code registriert mit externem Effekt (``transactional=False``); ``None``: nicht registriert
    external: bool | None


@dataclass(frozen=True)
class RestoreState:
    """Stand von Journal, Abonnements und Aufträgen in dieser Datenbank."""

    available: bool
    head_seq: int = 0
    sequence_value: int = 0
    unsequenced: int = 0
    last_recorded_at: datetime | None = None
    subscriptions: list[SubscriptionState] = field(default_factory=list)
    parked: int = 0
    dead: int = 0
    tasks: dict[str, int] = field(default_factory=dict)
    sequencer_lease_valid: bool = False

    @property
    def cursor_ahead(self) -> list[str]:
        """Abonnements, deren Cursor hinter dem Ende des Journals steht (passt nicht zur Sicherung)."""
        return [s.name for s in self.subscriptions if s.cursor > self.head_seq]

    @property
    def external(self) -> list[str]:
        return [s.name for s in self.subscriptions if s.external]


def available() -> bool:
    if connection.vendor != "postgresql":
        return False
    vorhanden = set(connection.introspection.table_names())
    return all(tabelle in vorhanden for tabelle in _TABELLEN)


def _sequence_value() -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT last_value FROM {SEQUENCE_NAME}")  # noqa: S608 – fester Name
        return int(cursor.fetchone()[0])


def state() -> RestoreState:
    """Liest den Stand; ändert nichts."""
    if not available():
        return RestoreState(available=False)
    journal = Event.objects.aggregate(
        head=Max("seq"), last=Max("recorded_at"), unsequenced=Count("pk", filter=Q(seq__isnull=True))
    )
    extern = {spec.name: not spec.transactional for spec in registered()}
    abonnements = [
        SubscriptionState(name=name, state=zustand, cursor=cursor, external=extern.get(name))
        for name, zustand, cursor in Subscription.objects.order_by("name").values_list("name", "state", "cursor_seq")
    ]
    geparkt = ParkedEvent.objects.aggregate(alle=Count("pk"), tot=Count("pk", filter=Q(state=ParkedState.TOT)))
    auftraege = dict(Task.objects.values_list("status").annotate(n=Count("pk")).values_list("status", "n"))
    return RestoreState(
        available=True,
        head_seq=journal["head"] or 0,
        sequence_value=_sequence_value(),
        unsequenced=journal["unsequenced"],
        last_recorded_at=journal["last"],
        subscriptions=abonnements,
        parked=geparkt["alle"],
        dead=geparkt["tot"],
        tasks=auftraege,
        sequencer_lease_valid=Lease.objects.filter(name=SEQUENCER_LEASE, expires_at__gt=Now()).exists(),
    )


def raise_sequence(gap: int = DEFAULT_GAP, *, force: bool = False) -> tuple[int, int] | None:
    """
    Hebt die Folgenummer um ``gap`` über den bisher höchsten Stand; gibt (vorher, nachher) zurück.

    ``None``: schon angehoben (die Sequenz steht mindestens ``gap`` über dem Ende des Journals, seither
    wurde nichts nummeriert). Ein erneuter Aufruf nach einer Wiederherstellung hebt also nicht doppelt
    an. ``SequencerRunningError``, solange ein Sequenzierer eine gültige Lease hält (``force``
    übergeht das).
    """
    if gap < 1:
        raise ValueError("Der Abstand muss mindestens 1 sein.")
    with transaction.atomic():
        # Ein Sequenzierer, der gerade übernimmt, wartet beim Vergeben auf diese Sperre (``leases.fence``)
        laeuft = Lease.objects.select_for_update().filter(name=SEQUENCER_LEASE, expires_at__gt=Now()).exists()
        if laeuft and not force:
            raise SequencerRunningError
        hoechste = Event.objects.aggregate(h=Max("seq"))["h"] or 0
        vorher = _sequence_value()
        if vorher >= hoechste + gap:
            return None
        nachher = max(vorher, hoechste) + gap
        with connection.cursor() as cursor:
            cursor.execute("SELECT setval(%s, %s, true)", [SEQUENCE_NAME, nachher])
    return vorher, nachher

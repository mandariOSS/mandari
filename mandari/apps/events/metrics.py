# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Metriken der Ereignistechnik im Prometheus-Format (Registrierung in ``EventsConfig.ready``).

- ``mandari_events_sequenced_total``: vergebene Folgenummern dieses Prozesses.
- ``mandari_events_published_total{type}``: veröffentlichte Ereignisse je Typ, gezählt beim Vergeben
  der Folgenummer. Der Sequenzierer sieht jedes festgeschriebene Ereignis genau einmal – auch die,
  die der Ingestor am Django-Prozess vorbei schreibt; ``publish()`` könnte nur seine eigenen zählen.
  Nur im Prozess des Sequenzierers (wie ``…_sequenced_total``), die Summe über alle Typen ist dieselbe.
- ``mandari_events_sequencer_blocked_seconds``: Wie lange eine offene Transaktion den
  Sequenzierer schon aufhält. 0, solange jedes festgeschriebene Ereignis eine Nummer bekommen
  kann. Sonst das Alter der Transaktion, die die Grenze ``pg_snapshot_xmin`` hält (auch in einer
  anderen Datenbank des Clusters oder vorbereitet per ``PREPARE TRANSACTION``), mindestens aber
  die Wartezeit des ältesten aufgehaltenen Ereignisses. Die zweite Angabe greift, wenn die
  Datenbankrolle fremde Sitzungen in ``pg_stat_activity`` nicht sehen darf. Alarm ab fünf Minuten.
- ``mandari_events_oldest_transaction_seconds``: Alter der ältesten offenen Transaktion mit
  Transaktionskennung im ganzen Cluster, soweit sichtbar; zeigt Stau schon, bevor Ereignisse
  warten.
- ``mandari_events_sequencer_lag_seconds``: Rückstand des Sequenzierers, gemessen ab Erfassung des
  ältesten Ereignisses, das eine Nummer bekommen könnte, aber noch keine hat. Wächst, wenn kein
  Sequenzierer läuft oder er hängt, was ``…_blocked_seconds`` nicht zeigt. Kurz nach dem Commit
  einer langen Transaktion ist der Wert bis zum nächsten Lauf hoch; Alarme brauchen deshalb eine
  Mindestdauer.
- ``mandari_events_delivered_total{subscription}``: zugestellte Ereignisse je Abonnement.
- ``mandari_events_delivery_failures_total{subscription}``: gescheiterte Zustellversuche.
- ``mandari_events_dead_total{subscription}``: nach allen Versuchen aufgegebene Ereignisse.
- ``mandari_events_listener_up``: 1, solange der Weckruf per ``LISTEN`` ankommt (Selbstprüfung),
  sonst 0; dann tragen die Abfragen allein. Nur im Prozess des Sequenzierers bzw. der Zustellung.
- ``mandari_events_parked{subscription,state}``: geparkte Ereignisse je Zustand (``wiederholen``,
  ``blockiert``, ``tot``), beim Abruf aus ``events_parked`` gezählt. Alarm bei ``tot`` > 0.
- ``mandari_events_lag_seconds{subscription}``: Rückstand eines Abonnements, gemessen ab Erfassung
  des ältesten nummerierten Ereignisses hinter seinem Cursor, das es zugestellt bekommt (Typmuster
  aus ``apps.events.registry``); 0 = aktuell. Nur für Abonnements, die im Code registriert sind –
  eine Zeile ohne Handler bekommt nie wieder etwas zugestellt und würde sonst dauerhaft alarmieren.
  Ein pausiertes Abonnement wächst bewusst; Alarm bei mehr als fünf Minuten, außer es ist pausiert.
- ``mandari_events_subscription_paused{subscription}``: 1, solange ein Abonnement pausiert ist.
- ``mandari_worker_role_up{role}``: 1, solange die Rolle im Worker arbeitet (Faden lebt und hat sich
  innerhalb der Frist gemeldet), sonst 0. Nur im Prozess von ``manage.py events_worker``.
- ``mandari_worker_role_beat_age_seconds{role}``: Sekunden seit dem letzten Lebenszeichen der Rolle
  (bei der Zustellung das älteste ihrer Abonnements). Nur im Worker.

Die Werte werden erst beim Abruf gemessen, damit das Registrieren keine Datenbankverbindung
belegt (siehe ``apps.common.metrics``, Issue #344). Die Werte zum Sequenzierer gibt es nur mit
PostgreSQL.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from django.db import connection
from prometheus_client import REGISTRY, Counter
from prometheus_client.core import GaugeMetricFamily, Metric

from apps.common.metrics import MisstErstBeimAbruf

if TYPE_CHECKING:
    from .registry import Subscriber

logger = logging.getLogger(__name__)

SEQUENCED = Counter("mandari_events_sequenced_total", "Vom Sequenzierer vergebene Folgenummern")
PUBLISHED = Counter(
    "mandari_events_published_total", "Veröffentlichte Ereignisse je Typ (beim Vergeben der Folgenummer)", ["type"]
)
DELIVERED = Counter("mandari_events_delivered_total", "Zugestellte Ereignisse je Abonnement", ["subscription"])
DELIVERY_FAILURES = Counter(
    "mandari_events_delivery_failures_total", "Gescheiterte Zustellversuche je Abonnement", ["subscription"]
)
DEAD = Counter(
    "mandari_events_dead_total", "Nach allen Versuchen aufgegebene (tote) Ereignisse je Abonnement", ["subscription"]
)

# Grenzen wie im Sequenzierer (``apps.events.sequencer._FREIE_ZEILEN``): vergebbar ist, was unter
# ``xmin`` liegt oder aus einem anderen Cluster stammt (ab ``xmax``); dazwischen wartet es.
_STAU = """
    WITH grenze AS MATERIALIZED (
        SELECT pg_snapshot_xmin(s.snap) AS xmin, pg_snapshot_xmax(s.snap) AS xmax
          FROM pg_current_snapshot() AS s(snap)
    ),
    wartend AS (
        SELECT min(e.recorded_at) AS seit
          FROM events_event e, grenze g
         WHERE e.seq IS NULL AND e.xid >= g.xmin AND e.xid < g.xmax
    ),
    vergebbar AS (
        SELECT min(e.recorded_at) AS seit
          FROM events_event e, grenze g
         WHERE e.seq IS NULL AND (e.xid < g.xmin OR e.xid >= g.xmax)
    ),
    halter AS (
        SELECT min(beginn) AS beginn FROM (
            SELECT a.xact_start AS beginn FROM pg_stat_activity a, grenze g WHERE a.backend_xid = g.xmin::xid
            UNION ALL
            SELECT p.prepared FROM pg_prepared_xacts p, grenze g WHERE p.transaction = g.xmin::xid
        ) h
    ),
    aelteste AS (
        SELECT min(beginn) AS beginn FROM (
            SELECT a.xact_start AS beginn FROM pg_stat_activity a WHERE a.backend_xid IS NOT NULL
            UNION ALL
            SELECT p.prepared FROM pg_prepared_xacts p
        ) t
    )
    SELECT EXTRACT(EPOCH FROM now() - w.seit)::float8,
           EXTRACT(EPOCH FROM now() - h.beginn)::float8,
           EXTRACT(EPOCH FROM now() - a.beginn)::float8,
           EXTRACT(EPOCH FROM now() - v.seit)::float8
      FROM wartend w, halter h, aelteste a, vergebbar v
"""


class SequencerBacklog(NamedTuple):
    """Messwerte zum Sequenzierer (Sekunden)."""

    #: wie lange eine offene Transaktion ihn aufhält (0 = nicht aufgehalten)
    blocked_seconds: float
    #: älteste offene Transaktion mit Kennung im Cluster, soweit sichtbar
    oldest_transaction_seconds: float | None
    #: ältestes vergebbares Ereignis ohne Nummer (0 = kein Rückstand)
    lag_seconds: float


def blocked_seconds(waiting_for: float | None, holder_age: float | None) -> float:
    """Stau-Dauer aus Wartezeit des ältesten aufgehaltenen Ereignisses und Alter der haltenden Transaktion.

    Die haltende Transaktion hatte ihre Kennung schon vor dem aufgehaltenen Ereignis; ihr Alter ist
    deshalb mindestens die Wartezeit. Ohne wartendes Ereignis ist nichts blockiert.
    """
    if waiting_for is None:
        return 0.0
    return max(waiting_for, holder_age or 0.0, 0.0)


def sequencer_backlog() -> SequencerBacklog | None:
    """Messwerte zum Sequenzierer oder ``None`` außerhalb von PostgreSQL."""
    if connection.vendor != "postgresql":
        return None
    with connection.cursor() as cursor:
        cursor.execute(_STAU)
        zeile = cursor.fetchone()
    wartet, halter, aelteste, rueckstand = zeile if zeile else (None, None, None, None)
    return SequencerBacklog(
        blocked_seconds=blocked_seconds(wartet, halter),
        oldest_transaction_seconds=max(aelteste, 0.0) if aelteste is not None else None,
        lag_seconds=max(rueckstand or 0.0, 0.0),
    )


class SequencerCollector(MisstErstBeimAbruf):
    def collect(self) -> Iterator[Metric]:
        try:
            werte = sequencer_backlog()
        except Exception:  # noqa: BLE001 – ein Sammler darf den Abruf nie abbrechen (z. B. Migration ausstehend)
            logger.debug("Stau des Sequenzierers nicht messbar", exc_info=True)
            return
        if werte is None:
            return
        yield GaugeMetricFamily(
            "mandari_events_sequencer_blocked_seconds",
            "Wie lange eine offene Transaktion den Sequenzierer aufhält (0 = nicht aufgehalten)",
            value=werte.blocked_seconds,
        )
        yield GaugeMetricFamily(
            "mandari_events_sequencer_lag_seconds",
            "Alter des ältesten vergebbaren Ereignisses ohne Folgenummer (0 = kein Rückstand)",
            value=werte.lag_seconds,
        )
        if werte.oldest_transaction_seconds is not None:
            yield GaugeMetricFamily(
                "mandari_events_oldest_transaction_seconds",
                "Alter der ältesten offenen Transaktion mit Kennung im Datenbank-Cluster",
                value=werte.oldest_transaction_seconds,
            )


def parked_counts() -> dict[tuple[str, str], int]:
    """Geparkte Ereignisse je (Abonnement, Zustand)."""
    from django.db.models import Count

    from .models import ParkedEvent

    zeilen = ParkedEvent.objects.values_list("subscription", "state").annotate(anzahl=Count("id")).order_by()
    return {(abonnement, zustand): anzahl for abonnement, zustand, anzahl in zeilen}


class ParkedCollector(MisstErstBeimAbruf):
    def collect(self) -> Iterator[Metric]:
        try:
            werte = parked_counts()
        except Exception:  # noqa: BLE001 – ein Sammler darf den Abruf nie abbrechen (z. B. Migration ausstehend)
            logger.debug("Geparkte Ereignisse nicht zählbar", exc_info=True)
            return
        familie = GaugeMetricFamily(
            "mandari_events_parked",
            "Geparkte Ereignisse je Abonnement und Zustand (wiederholen, blockiert, tot)",
            labels=["subscription", "state"],
        )
        for (abonnement, zustand), anzahl in sorted(werte.items()):
            familie.add_metric([abonnement, zustand], anzahl)
        yield familie


class SubscriptionLag(NamedTuple):
    """Stand eines Abonnements für Metriken und Admin-Seite."""

    name: str
    state: str
    cursor: int
    #: Alter des ältesten nummerierten, noch nicht zugestellten Ereignisses (0 = aktuell); ``None``,
    #: wenn das Abonnement nicht registriert ist (dann weiß niemand, welche Typen es bekommt)
    lag_seconds: float | None


def lag_seconds(spec: Subscriber, cursor: int, now: datetime) -> float:
    """
    Rückstand eines Abonnements: Alter (ab Erfassung) des ältesten nummerierten Ereignisses hinter dem
    Cursor, das zu seinen Typmustern passt; 0, wenn es keines gibt. ``now`` ist die Datenbankzeit.
    """
    from .models import Event

    aeltestes = (
        Event.objects.filter(seq__gt=cursor)
        .filter(spec.type_filter())
        .order_by("seq")
        .values_list("recorded_at", flat=True)
        .first()
    )
    return max((now - aeltestes).total_seconds(), 0.0) if aeltestes is not None else 0.0


def subscription_lags() -> list[SubscriptionLag]:
    """Rückstand je Abonnement in der Datenbank, nach Namen sortiert (``mandari_events_lag_seconds``)."""
    from django.db.models.functions import Now

    from .models import Subscription
    from .registry import load_subscribers

    registriert = {spec.name: spec for spec in load_subscribers()}
    ergebnis = []
    for name, zustand, cursor, jetzt in (
        Subscription.objects.annotate(jetzt=Now()).order_by("name").values_list("name", "state", "cursor_seq", "jetzt")
    ):
        spec = registriert.get(name)
        rueckstand = lag_seconds(spec, cursor, jetzt) if spec is not None else None
        ergebnis.append(SubscriptionLag(name, zustand, cursor, rueckstand))
    return ergebnis


class SubscriptionCollector(MisstErstBeimAbruf):
    def collect(self) -> Iterator[Metric]:
        from .models import SubscriptionState

        try:
            werte = subscription_lags()
        except Exception:  # noqa: BLE001 – ein Sammler darf den Abruf nie abbrechen (z. B. Migration ausstehend)
            logger.debug("Rückstand der Abonnements nicht messbar", exc_info=True)
            return
        rueckstand = GaugeMetricFamily(
            "mandari_events_lag_seconds",
            "Alter des ältesten noch nicht zugestellten Ereignisses je Abonnement (0 = aktuell)",
            labels=["subscription"],
        )
        pausiert = GaugeMetricFamily(
            "mandari_events_subscription_paused", "Abonnement pausiert (1) oder nicht (0)", labels=["subscription"]
        )
        for stand in werte:
            if stand.lag_seconds is not None:
                rueckstand.add_metric([stand.name], stand.lag_seconds)
            pausiert.add_metric([stand.name], 1.0 if stand.state == SubscriptionState.PAUSIERT else 0.0)
        yield from (rueckstand, pausiert)


class ListenerCollector(MisstErstBeimAbruf):
    """``mandari_events_listener_up``, nur in Prozessen mit Listener (sonst hieße 0 fälschlich „gestört“)."""

    def __init__(self) -> None:
        self.wert: float | None = None

    def set(self, gesund: bool) -> None:
        self.wert = 1.0 if gesund else 0.0

    def collect(self) -> Iterator[Metric]:
        if self.wert is None:
            return
        yield GaugeMetricFamily(
            "mandari_events_listener_up",
            "Weckruf per LISTEN kommt an (1) oder es tragen nur die Abfragen (0)",
            value=self.wert,
        )


LISTENER_UP = ListenerCollector()


class RoleState(NamedTuple):
    """Zustand einer Rolle im Worker (``apps.events.worker``)."""

    #: Faden lebt und hat sich innerhalb der Frist gemeldet
    up: bool
    #: Sekunden seit dem letzten Lebenszeichen (``None``: noch keins)
    beat_age: float | None


class WorkerCollector(MisstErstBeimAbruf):
    """Zustand der Rollen, nur im Prozess eines Workers (sonst hieße 0 fälschlich „ausgefallen“)."""

    def __init__(self) -> None:
        self.quelle: Callable[[], Mapping[str, RoleState]] | None = None

    def provide(self, quelle: Callable[[], Mapping[str, RoleState]] | None) -> None:
        self.quelle = quelle

    def collect(self) -> Iterator[Metric]:
        quelle = self.quelle
        if quelle is None:
            return
        zustaende = quelle()
        aktiv = GaugeMetricFamily(
            "mandari_worker_role_up",
            "Rolle arbeitet im Worker (1) oder ist ausgefallen bzw. hängt (0)",
            labels=["role"],
        )
        alter = GaugeMetricFamily(
            "mandari_worker_role_beat_age_seconds", "Sekunden seit dem letzten Lebenszeichen der Rolle", labels=["role"]
        )
        for rolle, zustand in sorted(zustaende.items()):
            aktiv.add_metric([rolle], 1.0 if zustand.up else 0.0)
            if zustand.beat_age is not None:
                alter.add_metric([rolle], zustand.beat_age)
        yield aktiv
        yield alter


WORKER_ROLES = WorkerCollector()


def register() -> None:
    # ValueError: bereits registriert (z. B. erneutes ready() in Tests)
    for sammler in _COLLECTORS:
        with contextlib.suppress(ValueError):
            REGISTRY.register(sammler)


_COLLECTOR = SequencerCollector()
_COLLECTORS = (_COLLECTOR, ParkedCollector(), SubscriptionCollector(), LISTENER_UP, WORKER_ROLES)

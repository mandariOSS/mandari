# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Metriken der Ereignistechnik im Prometheus-Format (Registrierung in ``EventsConfig.ready``).

- ``mandari_events_sequenced_total``: vergebene Folgenummern dieses Prozesses.
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

Die Werte werden erst beim Abruf gemessen, damit das Registrieren keine Datenbankverbindung
belegt (siehe ``apps.common.metrics``, Issue #344). Nur PostgreSQL.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import NamedTuple

from django.db import connection
from prometheus_client import REGISTRY, Counter
from prometheus_client.core import GaugeMetricFamily, Metric

from apps.common.metrics import MisstErstBeimAbruf

logger = logging.getLogger(__name__)

SEQUENCED = Counter("mandari_events_sequenced_total", "Vom Sequenzierer vergebene Folgenummern")

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


def register() -> None:
    # ValueError: bereits registriert (z. B. erneutes ready() in Tests)
    with contextlib.suppress(ValueError):
        REGISTRY.register(_COLLECTOR)


_COLLECTOR = SequencerCollector()

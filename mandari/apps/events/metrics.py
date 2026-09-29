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

Die Werte werden erst beim Abruf gemessen, damit das Registrieren keine Datenbankverbindung
belegt (siehe ``apps.common.metrics``, Issue #344). Nur PostgreSQL.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

from django.db import connection
from prometheus_client import REGISTRY, Counter
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

logger = logging.getLogger(__name__)

SEQUENCED = Counter("mandari_events_sequenced_total", "Vom Sequenzierer vergebene Folgenummern")

_STAU = """
    WITH grenze AS MATERIALIZED (
        SELECT pg_snapshot_xmin(pg_current_snapshot()) AS xmin
    ),
    wartend AS (
        SELECT min(e.recorded_at) AS seit
          FROM events_event e, grenze g
         WHERE e.seq IS NULL AND e.xid >= g.xmin
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
           EXTRACT(EPOCH FROM now() - a.beginn)::float8
      FROM wartend w, halter h, aelteste a
"""


def blocked_seconds(waiting_for: float | None, holder_age: float | None) -> float:
    """Stau-Dauer aus Wartezeit des ältesten aufgehaltenen Ereignisses und Alter der haltenden Transaktion.

    Die haltende Transaktion hatte ihre Kennung schon vor dem aufgehaltenen Ereignis; ihr Alter ist
    deshalb mindestens die Wartezeit. Ohne wartendes Ereignis ist nichts blockiert.
    """
    if waiting_for is None:
        return 0.0
    return max(waiting_for, holder_age or 0.0, 0.0)


def sequencer_backlog() -> tuple[float, float | None] | None:
    """(Stau-Dauer, Alter der ältesten Transaktion im Cluster) oder ``None`` außerhalb von PostgreSQL."""
    if connection.vendor != "postgresql":
        return None
    with connection.cursor() as cursor:
        cursor.execute(_STAU)
        zeile = cursor.fetchone()
    wartet, halter, aelteste = zeile if zeile else (None, None, None)
    return blocked_seconds(wartet, halter), (max(aelteste, 0.0) if aelteste is not None else None)


class SequencerCollector(Collector):
    def describe(self) -> Iterator[Metric]:
        # Leer: sonst misst prometheus_client schon beim Registrieren (Datenbankzugriff beim Import)
        return iter(())

    def collect(self) -> Iterator[Metric]:
        try:
            werte = sequencer_backlog()
        except Exception:  # noqa: BLE001 – ein Sammler darf den Abruf nie abbrechen (z. B. Migration ausstehend)
            logger.debug("Stau des Sequenzierers nicht messbar", exc_info=True)
            return
        if werte is None:
            return
        stau, aelteste = werte
        yield GaugeMetricFamily(
            "mandari_events_sequencer_blocked_seconds",
            "Wie lange eine offene Transaktion den Sequenzierer aufhält (0 = nicht aufgehalten)",
            value=stau,
        )
        if aelteste is not None:
            yield GaugeMetricFamily(
                "mandari_events_oldest_transaction_seconds",
                "Alter der ältesten offenen Transaktion mit Kennung im Datenbank-Cluster",
                value=aelteste,
            )


def register() -> None:
    # ValueError: bereits registriert (z. B. erneutes ready() in Tests)
    with contextlib.suppress(ValueError):
        REGISTRY.register(_COLLECTOR)


_COLLECTOR = SequencerCollector()

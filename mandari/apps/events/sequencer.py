# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sequenzierer: vergibt die Folgenummer ``seq`` erst nach dem Commit (``docs/adr/20260929-sequenzierer.md``).

Eine Nummer, die schon in der schreibenden Transaktion vergeben würde, würde in Commit-Reihenfolge
sichtbar, nicht in Vergabereihenfolge; ein Leser mit ``seq > cursor`` könnte Ereignisse
überspringen. Deshalb vergibt genau ein Prozess (Lease ``sequencer``) die Nummern, und zwar nur an
Zeilen, deren Transaktion älter ist als die älteste noch laufende
(``xid < pg_snapshot_xmin(pg_current_snapshot())``). Diese Transaktionen sind beendet; ihre Zeilen
sind festgeschrieben oder verworfen.

Ein Lauf:

1. Zeilen ohne Nummer unterhalb der Grenze sperren, sortiert nach ``(xid, id)``
   (``FOR UPDATE SKIP LOCKED``, höchstens ``batch_size``).
2. In derselben Transaktion prüfen, dass die Lease noch diesem Prozess gehört, und sie sperren
   (``leases.fence``). Eine Übernahme wartet dadurch, bis dieser Lauf festgeschrieben ist, und
   bekommt danach größere Nummern.
3. Nummern aus ``events_seq`` holen und in Python den sortierten Zeilen zuordnen. PostgreSQL
   garantiert die Auswertungsreihenfolge von ``nextval()`` in ``UPDATE … FROM`` nicht; so bleiben
   Ereignisse einer Transaktion sicher in Schreibreihenfolge.
4. ``pg_notify('mandari_events_seq', '')`` weckt die Zustellung; die Meldung kommt beim Commit an.

Lange Schreibtransaktionen halten den Sequenzierer auf, auch solche anderer Datenbanken im selben
Cluster. Das misst ``mandari_events_sequencer_blocked_seconds`` (``apps.events.metrics``).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from django.db import DatabaseError, close_old_connections, connection, transaction

from . import leases
from .metrics import SEQUENCED
from .models import SEQUENCE_NAME

logger = logging.getLogger(__name__)

#: Name der Leader-Lease
LEASE_NAME = "sequencer"
#: Kanal, auf dem der Sequenzierer nach jeder Vergabe die Zustellung weckt
SEQUENCED_CHANNEL = "mandari_events_seq"
#: Zeilen je Lauf
BATCH_SIZE = 1000
#: Wartezeit zwischen zwei Läufen im Dauerbetrieb (Sekunden)
POLL_INTERVAL = 1.0

_FREIE_ZEILEN = """
    WITH grenze AS MATERIALIZED (
        SELECT pg_snapshot_xmin(pg_current_snapshot()) AS xmin
    )
    SELECT e.id
      FROM events_event e, grenze g
     WHERE e.seq IS NULL
       AND e.xid < g.xmin
     ORDER BY e.xid, e.id
     LIMIT %s
       FOR UPDATE OF e SKIP LOCKED
"""

_NUMMERN_HOLEN = f"SELECT nextval('{SEQUENCE_NAME}') FROM generate_series(1, %s)"

_NUMMERN_SETZEN = """
    UPDATE events_event e
       SET seq = v.seq
      FROM unnest(%s::bigint[], %s::bigint[]) AS v(id, seq)
     WHERE e.id = v.id
"""


class SequencerUnavailableError(Exception):
    """Der Sequenzierer braucht PostgreSQL (xid8, Snapshots, Sequenzen)."""


def require_postgresql() -> None:
    if connection.vendor != "postgresql":
        raise SequencerUnavailableError("Der Sequenzierer braucht PostgreSQL.")


def assign_batch(holder: str, batch_size: int = BATCH_SIZE) -> int:
    """Ein Lauf: vergibt Folgenummern an höchstens ``batch_size`` Ereignisse; gibt deren Zahl zurück.

    Wirft ``leases.LeaseLostError``, wenn ``holder`` die Lease nicht (mehr) hält; dann ist nichts vergeben.
    """
    require_postgresql()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(_FREIE_ZEILEN, [batch_size])
        ids = [zeile[0] for zeile in cursor.fetchall()]
        if not ids:
            return 0
        leases.fence(LEASE_NAME, holder)
        cursor.execute(_NUMMERN_HOLEN, [len(ids)])
        nummern = sorted(zeile[0] for zeile in cursor.fetchall())
        cursor.execute(_NUMMERN_SETZEN, [ids, nummern])
        cursor.execute("SELECT pg_notify(%s, '')", [SEQUENCED_CHANNEL])
    SEQUENCED.inc(len(ids))
    return len(ids)


@dataclass
class Sequencer:
    """Leader-Rolle Sequenzierer für einen Prozess (Befehl ``events_sequencer``, später ``events_worker``)."""

    holder: str = field(default_factory=leases.new_holder_id)
    batch_size: int = BATCH_SIZE
    is_leader: bool = False
    _renewed_at: float = field(default=0.0, init=False, repr=False)

    def ensure_lease(self) -> bool:
        """Übernimmt die Lease oder erneuert sie, wenn die Erneuerung fällig ist."""
        faellig = time.monotonic() - self._renewed_at >= leases.RENEW_INTERVAL.total_seconds()
        if self.is_leader and not faellig:
            return True
        war_leader = self.is_leader
        self.is_leader = leases.acquire(LEASE_NAME, self.holder)
        if self.is_leader:
            self._renewed_at = time.monotonic()
            if not war_leader:
                logger.info("Sequenzierer: Lease übernommen (%s)", self.holder)
        elif war_leader:
            logger.warning("Sequenzierer: Lease an einen anderen Prozess verloren (%s)", self.holder)
        return self.is_leader

    def drain(self) -> int:
        """Vergibt Nummern, bis kein vergebbares Ereignis mehr übrig ist; gibt die Summe zurück."""
        gesamt = 0
        while self.ensure_lease():
            try:
                anzahl = assign_batch(self.holder, self.batch_size)
            except leases.LeaseLostError:
                self.is_leader = False
                logger.warning("Sequenzierer: Lease während des Laufs verloren (%s)", self.holder)
                break
            gesamt += anzahl
            if anzahl < self.batch_size:
                break
        return gesamt

    def release(self) -> None:
        if self.is_leader:
            leases.release(LEASE_NAME, self.holder)
            self.is_leader = False

    def run(self, stop: threading.Event, interval: float = POLL_INTERVAL) -> None:
        """Dauerbetrieb bis ``stop`` gesetzt ist; ein laufender Lauf wird noch festgeschrieben.

        Fällt die Datenbank kurz aus, wird der Fehler protokolliert und nach ``interval`` erneut
        versucht. Beim Ende wird die Lease freigegeben.
        """
        require_postgresql()
        try:
            while not stop.is_set():
                close_old_connections()
                try:
                    self.drain()
                except DatabaseError:
                    logger.warning("Sequenzierer: Datenbankfehler, neuer Versuch folgt", exc_info=True)
                    self.is_leader = False
                    connection.close()
                stop.wait(interval)
        finally:
            try:
                self.release()
            except DatabaseError:
                logger.warning("Sequenzierer: Lease konnte nicht freigegeben werden", exc_info=True)

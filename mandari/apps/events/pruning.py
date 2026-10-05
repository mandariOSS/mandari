# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Aufräumen des Journals festhalten (``docs/adr/20260929-ereignistechnik-postgres.md``, Punkt 10).

Das Journal darf alte Zeilen löschen (später ganze Monatspartitionen). Wer das tut, ruft in derselben
Transaktion ``record`` auf. Leser mit eigenem Stand fragen ``horizon`` und erkennen daran, ob ihnen
Zeilen fehlen können.

Warum ausdrücklich: Aus den verbliebenen Zeilen lässt sich ein Aufräumen nicht sicher ablesen. Der
Sequenzierer darf Nummern verwerfen (``nextval()`` ist nicht transaktional; ein zurückgerollter Lauf
verbraucht Nummern), das Journal kann also mit einer Lücke beginnen, ohne dass etwas gelöscht wurde.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from django.db import connection
from django.db.models import Max

from .models import JournalPruning

#: Schlüssel der Transaktionssperre, über die sich Aufräumen und Nachspielen abstimmen ("journal" in ASCII)
LOCK_KEY: Final = 0x6A6F75726E616C


@dataclass(frozen=True)
class Horizon:
    """
    Stand des Aufräumens über alle Löschungen: Zeilen bis ``through_seq`` können fehlen; alle gelöschten
    Zeilen wurden vor ``recorded_before`` erfasst.
    """

    through_seq: int
    recorded_before: datetime


def record(through_seq: int, recorded_before: datetime) -> None:
    """
    Ein Aufräumen festhalten – in derselben Transaktion wie das Löschen.

    ``through_seq``: höchste Folgenummer unter den gelöschten Zeilen; ``recorded_before``: Zeitpunkt, vor
    dem alle gelöschten Zeilen erfasst wurden (etwa die Obergrenze der gelöschten Partition).
    """
    if through_seq < 1:
        raise ValueError("through_seq muss mindestens 1 sein.")
    if recorded_before.tzinfo is None:
        raise ValueError("recorded_before braucht eine Zeitzone.")
    JournalPruning.objects.create(through_seq=through_seq, recorded_before=recorded_before)


def horizon() -> Horizon | None:
    """Stand des Aufräumens; ``None``, solange nie etwas gelöscht wurde."""
    stand = JournalPruning.objects.aggregate(seq=Max("through_seq"), before=Max("recorded_before"))
    if stand["seq"] is None or stand["before"] is None:
        return None
    return Horizon(through_seq=int(stand["seq"]), recorded_before=stand["before"])


def pruned_through(seq: int) -> int | None:
    """Kann die Folgenummer ``seq`` aufgeräumt sein? Dann die höchste aufgeräumte Folgenummer, sonst ``None``."""
    stand = horizon()
    if stand is None or seq > stand.through_seq:
        return None
    return stand.through_seq


def lock(*, shared: bool) -> None:
    """
    Aufräumen und Nachspielen abstimmen, bis die laufende Transaktion endet (``pg_advisory_xact_lock``).

    Jeder Löschschritt des Aufräumens nimmt die Sperre geteilt, bevor er den kleinsten Cursor liest; ein
    Nachspielen nimmt sie exklusiv, bevor es einen Cursor zurücksetzt. So liest ein Löschschritt entweder
    den Cursor nach dem festgeschriebenen Nachspielen, oder das Nachspielen wartet, bis der Schritt
    festgeschrieben ist (und ``pruned_through`` nennt, was fehlt). Ohne die Sperre könnte ein Schritt mit
    dem Cursor von vorher Zeilen löschen, die das eben zurückgesetzte Abonnement noch braucht. Die
    Zustellung nimmt die Sperre nicht und wartet nie darauf. Nur PostgreSQL; SQLite (Tests) schreibt
    nicht nebenläufig.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("pruning.lock gilt nur innerhalb einer Transaktion (transaction.atomic)")
    if connection.vendor != "postgresql":
        return
    funktion = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {funktion}(%s)", [LOCK_KEY])

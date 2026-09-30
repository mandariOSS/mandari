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

from django.db.models import Max

from .models import JournalPruning


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

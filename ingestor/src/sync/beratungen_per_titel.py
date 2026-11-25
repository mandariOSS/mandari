# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beratungen anstehender Sitzungen über den Titel ihrer Vorlage einem Tagesordnungspunkt zuordnen (Issue #1578).

Work und Insight finden die Vorlage eines Tagesordnungspunkts über seine Beratung (``agenda_item_external_id``).
ALLRIS (CC e-gov) liefert diese Zuordnung erst nach der Sitzung: Vorher nennt der Punkt keine Beratung
(``AgendaItem.consultation`` fehlt), und die Beratungen der Vorlage nennen nur das Gremium, weder Sitzung noch Punkt.
Zur Vorbereitung einer Sitzung fehlen so alle Vorlagen. Der Punkt trägt aber fast immer den Titel seiner Vorlage.

Nach den Listen einer Kommune ordnet der Abgleich deshalb eine Beratung ohne Sitzung und ohne Punkt dem Punkt einer
Sitzung von ``ZURUECK`` bis ``VORAUS`` zu, wenn der Titel der Vorlage dem des Punkts gleicht (Groß- und
Kleinschreibung, Leerraum egal, mindestens ``MINDESTLAENGE`` Zeichen), ein Gremium der Beratung eines der Sitzung ist
und der Punkt weder selbst eine Beratung nennt noch schon eine hat. Die Kandidaten sucht eine Abfrage
(``DatabaseStorage.consultation_title_candidates``); zugeordnet wird nur, was in beide Richtungen eindeutig ist
(``eindeutige_paare``): ein Kandidat für den Punkt und ein Punkt für die Beratung.

Die Zuordnung ist an der Beratung markiert (``agenda_item_inferred``). Liefert die Quelle später Sitzung oder Punkt,
gelten ihre Angaben (``DatabaseStorage.upsert_consultation``). Eine abgeleitete Zuordnung, deren Punkt gelöscht ist, die
der Punkt selbst anders nennt oder auf deren Punkt inzwischen eine Beratung der Quelle zeigt, nimmt der nächste Lauf
zurück (``DatabaseStorage.release_inferred_consultation_links``). ``sync_config["beratungen_per_titel"] = false``
schaltet das je Quelle ab. Keine Abrufe: Alles kommt aus dem Bestand.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

#: Schlüssel in ``sync_config``: ``false`` schaltet die Zuordnung für die Quelle ab
SCHALTER = "beratungen_per_titel"
#: Zeitraum der Sitzungen, deren Punkte zugeordnet werden
ZURUECK = timedelta(days=14)
VORAUS = timedelta(days=60)
#: Kürzere Titel („Anfragen“, „Mitteilungen“) sind zu allgemein für eine Zuordnung
MINDESTLAENGE = 10


def eingeschaltet(sync_config: Mapping[str, Any] | None) -> bool:
    """Ob die Zuordnung für die Quelle läuft: Standard ja, aus mit ``sync_config[SCHALTER]`` ``false`` (oder ``0``)."""
    if not isinstance(sync_config, Mapping):
        return True
    wert = sync_config.get(SCHALTER, True)
    return not (wert is False or (isinstance(wert, int) and wert == 0))


@dataclass(frozen=True)
class Kandidat:
    """Beratung, deren Vorlage den Titel des Tagesordnungspunkts trägt und deren Gremium in der Sitzung tagt."""

    agenda_item_external_id: str
    agenda_item_id: UUID
    meeting_id: UUID
    consultation_external_id: str


def eindeutige_paare(kandidaten: Iterable[Kandidat]) -> list[Kandidat]:
    """
    Die Kandidaten, die in beide Richtungen eindeutig sind: Für den Punkt gibt es genau eine Beratung, für die
    Beratung genau einen Punkt. Zwei Vorlagen gleichen Titels, zwei Beratungen derselben Vorlage im selben Gremium
    oder derselbe Titel in zwei Sitzungen bleiben ohne Zuordnung. Sortiert nach Punkt.
    """
    alle = set(kandidaten)
    je_punkt: defaultdict[str, set[str]] = defaultdict(set)
    je_beratung: defaultdict[str, set[str]] = defaultdict(set)
    for kandidat in alle:
        je_punkt[kandidat.agenda_item_external_id].add(kandidat.consultation_external_id)
        je_beratung[kandidat.consultation_external_id].add(kandidat.agenda_item_external_id)
    return sorted(
        (
            kandidat
            for kandidat in alle
            if len(je_punkt[kandidat.agenda_item_external_id]) == 1
            and len(je_beratung[kandidat.consultation_external_id]) == 1
        ),
        key=lambda kandidat: (kandidat.agenda_item_external_id, kandidat.consultation_external_id),
    )

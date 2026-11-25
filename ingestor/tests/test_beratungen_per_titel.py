# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zuordnung von Beratungen über den Titel der Vorlage (Issue #1578): Eindeutigkeit und Schalter, ohne Datenbank."""

from __future__ import annotations

import uuid

from src.sync.beratungen_per_titel import Kandidat, eindeutige_paare, eingeschaltet

SITZUNG = uuid.uuid4()


def _kandidat(punkt: str, beratung: str, sitzung: uuid.UUID = SITZUNG) -> Kandidat:
    return Kandidat(punkt, uuid.uuid5(uuid.NAMESPACE_URL, punkt), sitzung, beratung)


def test_eindeutige_paare_werden_zugeordnet() -> None:
    a = _kandidat("top/1", "beratung/1")
    b = _kandidat("top/2", "beratung/2")
    assert eindeutige_paare([b, a]) == [a, b]


def test_doppelte_kandidaten_zaehlen_einmal() -> None:
    """Dieselbe Paarung über zwei gemeinsame Gremien kommt aus der Abfrage nur einmal an, hier doppelt."""
    a = _kandidat("top/1", "beratung/1")
    assert eindeutige_paare([a, a]) == [a]


def test_zwei_beratungen_fuer_einen_punkt_bleiben_offen() -> None:
    """Zwei Vorlagen gleichen Titels oder zwei Beratungen derselben Vorlage im selben Gremium."""
    assert eindeutige_paare([_kandidat("top/1", "beratung/1"), _kandidat("top/1", "beratung/2")]) == []


def test_eine_beratung_fuer_zwei_punkte_bleibt_offen() -> None:
    """Derselbe Titel in zwei Sitzungen des Gremiums: Welche gemeint ist, sagt erst die Quelle."""
    zweite = uuid.uuid4()
    eindeutig = _kandidat("top/3", "beratung/3")
    kandidaten = [_kandidat("top/1", "beratung/1"), _kandidat("top/2", "beratung/1", zweite), eindeutig]
    assert eindeutige_paare(kandidaten) == [eindeutig]


def test_schalter() -> None:
    assert eingeschaltet(None)
    assert eingeschaltet({})
    assert eingeschaltet({"beratungen_per_titel": True})
    assert not eingeschaltet({"beratungen_per_titel": False})
    # Wie beim Kontingent des Nachladens schaltet auch 0 ab, ein leerer Wert nicht
    assert not eingeschaltet({"beratungen_per_titel": 0})
    assert eingeschaltet({"beratungen_per_titel": None})

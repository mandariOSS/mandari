# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zähler nicht auflösbarer Verweise im Abgleich einer Kommune (Issue #1441).

Nennt eine Quelle an der Sitzung ein Gremium, das sie nicht ausgibt (gesperrt, HTTP 401), bleibt die Sitzung
gespeichert; eine Mitgliedschaft ohne Person oder Gremium im Bestand wird nicht gespeichert. Beides war bisher nur
am einzelnen Objekt zu sehen. Der Speicher zählt solche Verweise je Art, das Ergebnis der Kommune und das
Sync-Protokoll nennen sie unter ``unresolved``.

Die Schreibwege selbst (``upsert_meeting``, ``upsert_membership``) prüft ``test_quellenbindung.py`` an PostgreSQL.
Antworten aus ``httpx.MockTransport``, keine Abrufe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from src.storage import database
from tests.test_aenderungsfenster import (  # noqa: F401 - _ohne_nebenlaeufe: autouse-Fixture
    Quelle,
    Speicher,
    _lauf,
    _ohne_nebenlaeufe,
)


def test_zaehler_beginnt_je_abgleich_neu_und_zaehlt_je_art() -> None:
    erster = database.start_unresolved_references()
    database._note_unresolved(database.UNRESOLVED_MEETING_ORGANIZATION, 2)
    database._note_unresolved(database.UNRESOLVED_MEETING_ORGANIZATION)
    database._note_unresolved(database.UNRESOLVED_MEMBERSHIP_PERSON)
    # Nichts aufzulösen ist kein Eintrag
    database._note_unresolved(database.UNRESOLVED_MEMBERSHIP_ORGANIZATION, 0)

    assert erster == {"meeting.organization": 3, "membership.person": 1}

    zweiter = database.start_unresolved_references()
    database._note_unresolved(database.UNRESOLVED_MEMBERSHIP_ORGANIZATION)

    assert zweiter == {"membership.organization": 1}
    assert erster == {"meeting.organization": 3, "membership.person": 1}


async def test_abgleich_nennt_nicht_aufloesbare_verweise_im_ergebnis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Was der Speicher während des Abgleichs zählt, steht unter ``unresolved`` im Ergebnis der Kommune."""
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    speicher = Speicher()

    async def upsert_body(body: Any, source_id: UUID) -> UUID:
        # Stellvertretend für die Schreibwege des Speichers (``upsert_meeting``, ``upsert_membership``)
        database._note_unresolved(database.UNRESOLVED_MEETING_ORGANIZATION, 4)
        return UUID(int=1)

    monkeypatch.setattr(speicher, "upsert_body", upsert_body)

    stats = await _lauf(quelle, speicher, full=True)

    assert stats["unresolved"] == {"meeting.organization": 4}
    # Der Zähler ist eine Auskunft: Der Abgleich bleibt vollständig und ohne Fehler
    assert stats["incomplete"] == []
    assert stats["errors"] == []


async def test_zaehlung_einer_kommune_geht_nicht_in_die_naechste_ein(monkeypatch: pytest.MonkeyPatch) -> None:
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))
    speicher = Speicher()
    laeufe = iter([3, 0])

    async def upsert_body(body: Any, source_id: UUID) -> UUID:
        database._note_unresolved(database.UNRESOLVED_MEMBERSHIP_PERSON, next(laeufe))
        return UUID(int=1)

    monkeypatch.setattr(speicher, "upsert_body", upsert_body)

    erster = await _lauf(quelle, speicher, full=True)
    zweiter = await _lauf(quelle, speicher, full=True)

    assert erster["unresolved"] == {"membership.person": 3}
    assert "unresolved" not in zweiter


async def test_ohne_nicht_aufloesbare_verweise_kein_eintrag() -> None:
    quelle = Quelle(geaendert=datetime.now(UTC) - timedelta(days=30))

    stats = await _lauf(quelle, Speicher(), full=True)

    assert "unresolved" not in stats

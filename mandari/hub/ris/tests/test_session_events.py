# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse aus Session-Zuständen (``hub.ris.session_events``, Issue #533): Sichtbarkeit je Feld und Übergang.

Die Zustände stehen hier ohne Datenbank; die Fachfunktionen prüft ``apps/session/tests/test_drehscheibe_sitzungen.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from hub.ris.mapping.session import SessionUris
from hub.ris.session_events import (
    DATE_ONLY,
    FULL,
    HIDDEN,
    AgendaItemState,
    MeetingState,
    SessionEvents,
)

BASIS = "https://mandari.example/session/nord/api/oparl/"
SITZUNG = uuid.UUID("00000000-0000-4000-8000-000000000001")
ANDERE = uuid.UUID("00000000-0000-4000-8000-000000000002")
TOP = uuid.UUID("00000000-0000-4000-8000-000000000003")
VORLAGE = uuid.UUID("00000000-0000-4000-8000-000000000004")


@pytest.fixture
def events() -> SessionEvents:
    return SessionEvents(tenant_id=uuid.uuid4(), uris=SessionUris(BASIS))


def sitzung(publicity: str = FULL, **felder: Any) -> MeetingState:
    werte = {
        "name": "Rat",
        "start": datetime(2031, 3, 1, 17, tzinfo=UTC),
        "end": None,
        "meetingState": "scheduled",
        "cancelled": False,
        "organization": (ANDERE,),
        "location": ("Rathaus", "", "", "", ""),
        "meetingFormat": None,
    }
    werte.update(felder)
    return MeetingState(id=SITZUNG, publicity=publicity, fields=werte, organizations=(ANDERE,))


def top(
    *, published: bool = True, meeting: uuid.UUID = SITZUNG, paper_published: bool = True, **felder: Any
) -> AgendaItemState:
    werte = {
        "name": "Radweg",
        "number": "1",
        "order": 1,
        "public": published,
        "withdrawn": False,
        "consultation": VORLAGE,
    }
    werte.update(felder)
    return AgendaItemState(
        id=TOP, meeting_id=meeting, published=published, fields=werte, paper_id=VORLAGE, paper_published=paper_published
    )


def kurz(drafts: list[Any]) -> list[tuple[str, str, str]]:
    return [(d.type, d.aggregate_type, d.visibility) for d in drafts]


def test_oeffentlich_auf_termin_nimmt_ort_zurueck(events: SessionEvents) -> None:
    drafts = events.meeting_drafts(sitzung(FULL), sitzung(DATE_ONLY))
    assert kurz(drafts) == [
        ("ris.meeting.changed", "Meeting", "oeffentlich"),
        ("ris.meeting.changed", "Meeting", "nichtoeffentlich"),
        ("ris.object.depublished", "Location", "oeffentlich"),
    ]
    assert drafts[0].payload["changed"] == ["location"], "Der Ort verschwindet für die Öffentlichkeit"
    assert drafts[1].payload["changed"] == ["public"]
    assert drafts[2].payload["reason"] == "nichtoeffentlich"


def test_ort_geloescht_bei_oeffentlicher_sitzung(events: SessionEvents) -> None:
    drafts = events.meeting_drafts(sitzung(), sitzung(location=None))
    assert kurz(drafts) == [
        ("ris.meeting.changed", "Meeting", "oeffentlich"),
        ("ris.object.depublished", "Location", "oeffentlich"),
    ]
    assert drafts[1].payload["reason"] == "quelle_geloescht"


def test_verborgene_sitzung_geloescht_meldet_nichts(events: SessionEvents) -> None:
    assert events.meeting_drafts(sitzung(HIDDEN), None) == []
    assert kurz(events.meeting_drafts(sitzung(), None)) == [
        ("ris.object.depublished", "Meeting", "oeffentlich"),
        ("ris.object.depublished", "Location", "oeffentlich"),
    ]


def test_unveraenderte_sitzung_meldet_nichts(events: SessionEvents) -> None:
    assert events.meeting_drafts(sitzung(), sitzung()) == []
    assert events.meeting_drafts(None, None) == []


def test_top_in_andere_sitzung_verschoben(events: SessionEvents) -> None:
    drafts = events.agenda_drafts({TOP: top()}, {TOP: top(meeting=ANDERE)})
    assert kurz(drafts) == [("ris.agendaitem.changed", "AgendaItem", "oeffentlich")]
    assert drafts[0].payload["change"] == "moved"
    assert drafts[0].payload["previous_meeting"] == str(events.ref("meeting", SITZUNG))
    assert drafts[0].payload["meeting"] == str(events.ref("meeting", ANDERE))


def test_vorlage_wird_unveroeffentlicht(events: SessionEvents) -> None:
    """Die Vorlage geht zurück in den Entwurf: öffentlich verschwindet der Bezug, intern bleibt er."""
    drafts = events.agenda_drafts({TOP: top()}, {TOP: top(paper_published=False)})
    assert kurz(drafts) == [("ris.agendaitem.changed", "AgendaItem", "oeffentlich")]
    assert drafts[0].payload["changed"] == ["consultation"]
    assert "paper" not in drafts[0].payload


def test_nichtoeffentlicher_top_nennt_seine_vorlage(events: SessionEvents) -> None:
    drafts = events.agenda_drafts({}, {TOP: top(published=False, paper_published=False)})
    assert kurz(drafts) == [("ris.agendaitem.changed", "AgendaItem", "nichtoeffentlich")]
    assert drafts[0].payload["paper"] == str(events.ref("paper", VORLAGE))


def test_unbekannte_versandart(events: SessionEvents) -> None:
    with pytest.raises(ValueError):
        events.invited_draft(SITZUNG, uuid.uuid4(), "brieftaube")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse aus Session-Zuständen (``hub.ris.session_events``, Issues #533–#535): Sichtbarkeit je Feld und Übergang.

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
    ConsultationState,
    FileState,
    MeetingState,
    PaperState,
    ProtocolState,
    SessionEvents,
)

BASIS = "https://mandari.example/session/nord/api/oparl/"
SITZUNG = uuid.UUID("00000000-0000-4000-8000-000000000001")
ANDERE = uuid.UUID("00000000-0000-4000-8000-000000000002")
TOP = uuid.UUID("00000000-0000-4000-8000-000000000003")
VORLAGE = uuid.UUID("00000000-0000-4000-8000-000000000004")
STATION = uuid.UUID("00000000-0000-4000-8000-000000000005")
DATEI = uuid.UUID("00000000-0000-4000-8000-000000000006")
ANTRAG = uuid.UUID("00000000-0000-4000-8000-000000000007")
PROTOKOLL = uuid.UUID("00000000-0000-4000-8000-000000000008")


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


# -- Vorlagen, Beratungen, Anlagen (Issue #534) ---------------------------------------------------------------


def vorlage(*, published: bool = True, public: bool = True, **felder: Any) -> PaperState:
    werte: dict[str, Any] = {
        "name": "Radweg",
        "reference": "2026/0001",
        "date": None,
        "paperType": "proposal",
        "originatorPerson": None,
        "originatorOrganization": None,
        "underDirectionOf": ANDERE,
        "status": "approved" if published else "draft",
        "public": public,
        "mainText": "",
        "resolutionText": "",
    }
    werte.update(felder)
    return PaperState(id=VORLAGE, published=published, fields=werte, submission_id=ANTRAG)


def station(*, published: bool = True, meeting_public: bool = True, item: uuid.UUID | None = None) -> ConsultationState:
    werte: dict[str, Any] = {
        "organization": ANDERE,
        "meeting": SITZUNG,
        "agendaItem": item,
        "role": "decision",
        "authoritative": True,
        "order": 1,
        "result": "",
    }
    return ConsultationState(
        id=STATION,
        paper_id=VORLAGE,
        published=published,
        fields=werte,
        meeting_public=meeting_public,
        item_public=item is not None and meeting_public,
        paper_public=True,
    )


def anlage(**felder: Any) -> FileState:
    werte: dict[str, Any] = {
        "name": "plan.pdf",
        "version": (1, "a"),
        "paper": VORLAGE,
        "meeting": None,
        "agendaItem": None,
    }
    werte.update(felder)
    return FileState(id=DATEI, published=True, fields=werte, paper_published=True)


def test_veroeffentlicht_angelegte_vorlage_nennt_einreichung_nur_nichtoeffentlich(events: SessionEvents) -> None:
    drafts = events.paper_drafts(None, vorlage())
    assert kurz(drafts) == [
        ("ris.paper.created", "Paper", "nichtoeffentlich"),
        ("ris.paper.released", "Paper", "oeffentlich"),
    ]
    assert drafts[0].payload["submission"] == str(ANTRAG)
    assert "submission" not in drafts[1].payload


def test_nichtoeffentliche_vorlage_zurueckgenommen_aus_dem_entwurf(events: SessionEvents) -> None:
    drafts = events.paper_drafts(vorlage(), vorlage(published=False, public=False))
    assert drafts[0].payload["reason"] == "nichtoeffentlich"
    drafts = events.paper_drafts(vorlage(), vorlage(published=False, status="draft"))
    assert drafts[0].payload["reason"] == "zurueckgenommen"


def test_station_in_nichtoeffentlicher_sitzung(events: SessionEvents) -> None:
    drafts = events.consultation_drafts({}, {STATION: station(meeting_public=False)})
    assert kurz(drafts) == [("ris.consultation.changed", "Consultation", "oeffentlich")]
    assert set(drafts[0].payload) == {"consultation", "paper", "change"}


def test_station_terminiert(events: SessionEvents) -> None:
    drafts = events.consultation_drafts({STATION: station()}, {STATION: station(item=TOP)})
    assert kurz(drafts) == [("ris.consultation.changed", "Consultation", "oeffentlich")]
    assert drafts[0].payload["change"] == "scheduled"
    assert drafts[0].payload["agenda_item"] == str(events.ref("agendaitem", TOP))


def test_anlage_ersetzt_und_umbenannt(events: SessionEvents) -> None:
    assert events.file_drafts({DATEI: anlage()}, {DATEI: anlage(version=(2, "b"))})[0].payload["change"] == "replaced"
    assert events.file_drafts({DATEI: anlage()}, {DATEI: anlage(name="Plan.pdf")})[0].payload["change"] == "renamed"
    assert events.file_drafts({DATEI: anlage()}, {DATEI: anlage()}) == []


# -- Niederschrift (Issue #535) ---------------------------------------------------------------------------------


def niederschrift(status: str, datei: uuid.UUID | None = None, **werte: Any) -> ProtocolState:
    return ProtocolState(id=PROTOKOLL, meeting_id=SITZUNG, status=status, file_id=datei, **werte)


def test_niederschrift_berichtigt_oder_erneuert(events: SessionEvents) -> None:
    vorher = niederschrift("published", DATEI, file_created_at=datetime(2026, 10, 1, tzinfo=UTC))
    berichtigt = niederschrift("published", ANTRAG, last_correction_at=datetime(2026, 10, 2, tzinfo=UTC))
    erneuert = niederschrift("published", ANTRAG, last_correction_at=datetime(2026, 9, 30, tzinfo=UTC))
    assert events.protocol_drafts(vorher, berichtigt, meeting_full=True)[0].payload["change"] == "corrected"
    assert events.protocol_drafts(vorher, erneuert, meeting_full=True)[0].payload["change"] == "renewed"


def test_niederschrift_nichtoeffentlicher_sitzung_nur_intern(events: SessionEvents) -> None:
    """Ohne öffentliche Fassung (nichtöffentliche Sitzung) gibt es nichts Öffentliches zu melden."""
    drafts = events.protocol_drafts(niederschrift("approved"), niederschrift("published"), meeting_full=False)
    assert drafts == []
    drafts = events.protocol_drafts(niederschrift("review"), niederschrift("published"), meeting_full=False)
    assert kurz(drafts) == [("ris.protocol.approved", "Meeting", "nichtoeffentlich")]
    assert drafts[0].payload["mode"] == "direct"


def test_genehmigung_in_der_folgesitzung(events: SessionEvents) -> None:
    drafts = events.protocol_drafts(
        niederschrift("review"), niederschrift("approved", approval_meeting_id=ANDERE), meeting_full=True
    )
    assert drafts[0].payload["mode"] == "follow_up"
    assert drafts[0].payload["approved_in"] == str(events.ref("meeting", ANDERE))

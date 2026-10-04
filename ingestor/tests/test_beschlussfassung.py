# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlussfassung aus den Erweiterungen ``mandari:*`` in die Spalten des RIS-Bestands (Issue #525).

Der Ingestor übersetzt wie der Spiegel in Django (``mandari_oparl.extensions``): Beschlussnummer, Abstimmung,
Einzelstimmen nur bei namentlicher Abstimmung, Umsetzungsstand und Genehmigung der Niederschrift. Was nicht passt,
wird verworfen; eine fremde Quelle schreibt so nichts Unerwartetes in den Bestand.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from mandari_oparl.extensions import AGENDA_ITEM_COLUMNS, MEETING_COLUMNS, agenda_item_columns, meeting_columns

from src.storage import models
from src.storage.database import _extension_columns
from src.sync.processor import OParlProcessor

BASIS = "https://mandari.example/session/nord/api/oparl/"

TOP: dict[str, Any] = {
    "id": f"{BASIS}agendaitem/1/",
    "meeting": f"{BASIS}meeting/1/",
    "name": "Radweg",
    "mandari:resolutionNumber": "B/2026/7",
    "mandari:vote": {"method": "roll_call", "result": "approved", "yes": 2, "no": 1, "abstain": 0},
    "mandari:rollCall": [
        {"name": "Petra Muster", "vote": "yes", "voteLabel": "Ja"},
        {"name": "Max Beispiel", "vote": "excluded"},
    ],
    "mandari:implementation": {
        "status": "in_progress",
        "deadline": "2026-12-31",
        "note": "Ausschreibung läuft.",
        "modified": "2026-09-20T10:00:00+02:00",
    },
}


def test_tagesordnungspunkt_mit_beschlussfassung() -> None:
    punkt = OParlProcessor().process_agenda_item(TOP)

    assert punkt.decision == {
        "resolution_number": "B/2026/7",
        "vote_method": "roll_call",
        "vote_result": "approved",
        "votes_yes": 2,
        "votes_no": 1,
        "votes_abstain": 0,
        "roll_call": [{"name": "Petra Muster", "vote": "yes"}, {"name": "Max Beispiel", "vote": "excluded"}],
        "implementation_status": "in_progress",
        "implementation_deadline": dt.date(2026, 12, 31),
        "implementation_public_note": "Ausschreibung läuft.",
        "implementation_modified": dt.datetime(2026, 9, 20, 8, 0, tzinfo=dt.UTC),
    }


def test_ohne_erweiterungen_sind_alle_spalten_leer() -> None:
    punkt = OParlProcessor().process_agenda_item({"id": f"{BASIS}agendaitem/2/", "name": "Verschiedenes"})

    assert punkt.decision == dict.fromkeys(AGENDA_ITEM_COLUMNS)
    # Upsert: alle Spalten, damit eine Quelle, die eine Erweiterung nicht mehr liefert, sie auch leert
    assert _extension_columns({}, AGENDA_ITEM_COLUMNS) == dict.fromkeys(AGENDA_ITEM_COLUMNS)


@pytest.mark.parametrize(
    ("erweiterungen", "erwartet"),
    [
        # Unbekannte Codes, falsche Typen und unplausible Zahlen werden verworfen
        ({"mandari:vote": {"method": "show_of_hands", "result": "maybe"}}, {}),
        ({"mandari:vote": {"method": "open", "yes": True, "no": -1, "abstain": 10**9}}, {"vote_method": "open"}),
        ({"mandari:vote": ["roll_call"]}, {}),
        ({"mandari:resolutionNumber": "  "}, {}),
        ({"mandari:resolutionNumber": "B" * 101}, {}),
        # Einzelstimmen nur bei namentlicher Abstimmung, nur mit Namen und bekanntem Code
        (
            {"mandari:vote": {"method": "secret"}, "mandari:rollCall": [{"name": "Petra Muster", "vote": "yes"}]},
            {"vote_method": "secret"},
        ),
        (
            {
                "mandari:vote": {"method": "roll_call"},
                "mandari:rollCall": [{"name": "", "vote": "yes"}, {"name": "X", "vote": "absent"}, "Petra"],
            },
            {"vote_method": "roll_call"},
        ),
        # Umsetzungsstand nur mit gültigem Status; ein Zeitpunkt ohne Zeitzone gilt nicht
        ({"mandari:implementation": {"note": "Ohne Status"}}, {}),
        (
            {"mandari:implementation": {"status": "done", "deadline": "31.12.2026", "modified": "2026-09-20T10:00"}},
            {"implementation_status": "done"},
        ),
    ],
)
def test_was_nicht_passt_wird_verworfen(erweiterungen: dict[str, Any], erwartet: dict[str, Any]) -> None:
    assert agenda_item_columns(erweiterungen) == {**dict.fromkeys(AGENDA_ITEM_COLUMNS), **erwartet}


def test_genehmigung_der_niederschrift() -> None:
    daten = {
        "id": f"{BASIS}meeting/1/",
        "name": "Rat",
        "mandari:protocolApproval": {"mode": "follow_up", "date": "2026-09-15", "meeting": f"{BASIS}meeting/2/"},
    }

    sitzung = OParlProcessor().process_meeting(daten)

    assert sitzung.protocol_approval == {
        "protocol_approval_mode": "follow_up",
        "protocol_approved_on": dt.date(2026, 9, 15),
        "protocol_approved_in_external_id": f"{BASIS}meeting/2/",
    }
    assert meeting_columns({"mandari:protocolApproval": {"mode": "spaeter", "date": "2026-09-15"}}) == dict.fromkeys(
        MEETING_COLUMNS
    )
    assert OParlProcessor().process_meeting({"id": f"{BASIS}meeting/3/"}).protocol_approval == dict.fromkeys(
        MEETING_COLUMNS
    )


def test_spalten_gibt_es_im_modell_des_ingestors() -> None:
    """Jede übersetzte Spalte steht in der Tabelle, in die der Upsert schreibt (Gegenstück: Schema-Contract)."""
    assert set(AGENDA_ITEM_COLUMNS) <= set(models.OParlAgendaItem.__table__.columns.keys())
    assert set(MEETING_COLUMNS) <= set(models.OParlMeeting.__table__.columns.keys())

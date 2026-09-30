"""
Sitzungsort aus dem Session-RIS von mandari.

Die Session-OParl-Schnittstelle (``apps/session/api/oparl.py``) liefert den Ort einer Sitzung
als OParl-``location``-Objekt und zusätzlich als fertigen Text in ``mandari:locationName``,
``mandari:locationRoom`` und ``mandari:locationAddress`` (ältere Stände nur als Text). Der Ingestor
muss den Text genauso abbilden wie der Spiegel ``insight_sync/session_mirror.py`` – sonst verlieren
Session-Sitzungen im Bürgerportal ihren Ort, sobald der Ingestor-Daemon die Quelle synchronisiert.

Der Ort gehört zur Sitzung und trägt deren Kennung. Im Bestand steht er deshalb als Text an der Sitzung
und nicht als eigenes Location-Objekt: Ein eigenes Objekt bliebe über den Aggregator abrufbar, wenn die
Sitzung zurückgenommen wird. Hat ein älterer Stand des Ingestors eines angelegt, wird es bei der nächsten
Änderung und bei der Rücknahme der Sitzung markiert.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from src.sync.orchestrator import SyncOrchestrator
from src.sync.processor import OParlProcessor, session_location_id

BASIS = "https://mandari.example/session/nord/api/oparl/"
LOKAL = "http://localhost:8000/session/demo-stadt/api/oparl/"


def _session_sitzung(**felder: Any) -> dict[str, Any]:
    """Sitzung, wie ``serialize_meeting`` sie ausgibt (leere Felder fallen dort weg)."""
    daten: dict[str, Any] = {
        "id": f"{BASIS}meeting/7f0c/",
        "type": "https://schema.oparl.org/1.1/Meeting",
        "name": "Rat",
        "start": "2026-10-01T17:00:00+02:00",
        "organization": [f"{BASIS}organization/1/"],
        "agendaItem": [],
        "auxiliaryFile": [],
        "created": "2026-09-01T10:00:00+02:00",
        "modified": "2026-09-02T10:00:00+02:00",
    }
    daten.update(felder)
    return daten


def _session_ort(**felder: Any) -> dict[str, Any]:
    """Location-Objekt, wie die Session-Schnittstelle es in die Sitzung einbettet."""
    daten: dict[str, Any] = {
        "id": f"{BASIS}location/7f0c/",
        "type": "https://schema.oparl.org/1.1/Location",
        "description": "Rathaus",
        "room": "Ratssaal",
        "streetAddress": "Markt 1",
        "postalCode": "12345",
        "locality": "Musterstadt",
        "meetings": [f"{BASIS}meeting/7f0c/"],
    }
    daten.update(felder)
    return daten


def test_ort_und_anschrift_aus_mandari_erweiterung() -> None:
    meeting = OParlProcessor().process_meeting(
        _session_sitzung(
            **{
                "mandari:locationName": "Rathaus",
                "mandari:locationRoom": "Ratssaal",
                "mandari:locationAddress": "Markt 1, 12345 Musterstadt",
            }
        ),
        f"{BASIS}body/",
    )

    assert meeting.location_name == "Rathaus"
    assert meeting.location_address == "Markt 1, 12345 Musterstadt"
    assert meeting.location_external_id is None


def test_nur_raum_ergibt_ortsnamen() -> None:
    meeting = OParlProcessor().process_meeting(
        _session_sitzung(**{"mandari:locationRoom": "Sitzungszimmer 2"}), f"{BASIS}body/"
    )

    assert meeting.location_name == "Sitzungszimmer 2"
    assert meeting.location_address is None


def test_ohne_ort_bleibt_leer() -> None:
    meeting = OParlProcessor().process_meeting(_session_sitzung(), f"{BASIS}body/")

    assert meeting.location_name is None
    assert meeting.location_address is None


def test_oparl_location_fremder_quelle_gilt_wie_bisher() -> None:
    """Liefert eine Quelle nur ein OParl-Location-Objekt, ergeben sich Ort und Anschrift daraus."""
    location = {
        "id": "https://ris.example/location/1",
        "type": "https://schema.oparl.org/1.1/Location",
        "room": "Saal A",
        "streetAddress": "Hauptstraße 1",
    }
    meeting = OParlProcessor().process_meeting(
        {"id": "https://ris.example/meeting/1", "name": "Rat", "location": location},
        "https://ris.example/body/1",
    )

    assert meeting.location_name == "Saal A"
    assert meeting.location_address == "Hauptstraße 1"
    assert meeting.location_external_id == "https://ris.example/location/1"
    assert [entity.external_id for entity in meeting.nested_entities] == ["https://ris.example/location/1"]


def test_session_mit_location_objekt_behaelt_ort_und_anschrift() -> None:
    """
    Session liefert Location-Objekt und Text. Im Bürgerportal bleibt der Ort wie bisher (Gebäude vor
    Raum, Anschrift mit Postleitzahl und Ort); ein eigenes Location-Objekt entsteht im Bestand nicht.
    """
    meeting = OParlProcessor().process_meeting(
        _session_sitzung(
            location=_session_ort(),
            **{
                "mandari:locationName": "Rathaus",
                "mandari:locationRoom": "Ratssaal",
                "mandari:locationAddress": "Markt 1, 12345 Musterstadt",
            },
        ),
        f"{BASIS}body/",
    )

    assert meeting.location_name == "Rathaus"
    assert meeting.location_address == "Markt 1, 12345 Musterstadt"
    assert meeting.location_external_id is None
    assert meeting.nested_entities == []


def test_session_nur_mit_location_objekt_ergibt_denselben_text() -> None:
    """Ohne die abgekündigten Textfelder ergibt das Location-Objekt denselben Ort und dieselbe Anschrift."""
    meeting = OParlProcessor().process_meeting(_session_sitzung(location=_session_ort()), f"{BASIS}body/")

    assert meeting.location_name == "Rathaus"
    assert meeting.location_address == "Markt 1, 12345 Musterstadt"
    assert meeting.location_external_id is None
    assert meeting.nested_entities == []


def test_session_location_objekt_nur_mit_raum() -> None:
    ort = _session_ort(description=None, streetAddress=None, postalCode=None, locality=None)
    meeting = OParlProcessor().process_meeting(_session_sitzung(location=ort), f"{BASIS}body/")

    assert meeting.location_name == "Ratssaal"
    assert meeting.location_address is None
    assert meeting.nested_entities == []


def test_anderer_ort_an_session_sitzung_bleibt_eigenes_objekt() -> None:
    """Nur der Ort unter der Kennung der Sitzung gehört zu ihr; jeder andere Ort ist ein eigenes Objekt."""
    anderer = f"{BASIS}location/ffff/"
    meeting = OParlProcessor().process_meeting(_session_sitzung(location=_session_ort(id=anderer)), f"{BASIS}body/")

    assert meeting.location_external_id == anderer
    assert [entity.external_id for entity in meeting.nested_entities] == [anderer]


@pytest.mark.parametrize(
    ("sitzung", "ort"),
    [
        (f"{BASIS}meeting/7f0c/", f"{BASIS}location/7f0c/"),
        (f"{LOKAL}meeting/1/", f"{LOKAL}location/1/"),
        # Sitzungen anderer Quellen: Dort sind Orte eigene Objekte mit eigener Kennung
        ("https://ris.example/oparl/meeting/7", None),
        ("https://ris.example/oparl/meeting/7/", None),
        ("https://mandari.example/oparl/v1/meeting/7f0c", None),
        (f"{BASIS}paper/7f0c/", None),
        ("", None),
        (None, None),
    ],
)
def test_kennung_des_sitzungsortes(sitzung: str | None, ort: str | None) -> None:
    assert session_location_id(sitzung) == ort


# =============================================================================
# Bestand: kein eigenes Location-Objekt, vorhandene werden zurückgenommen
# =============================================================================


class MerkSpeicher:
    """Speicher, der die Aufrufe samt Kennung mitschreibt."""

    def __init__(self) -> None:
        self.aufrufe: list[tuple[str, ...]] = []

    async def upsert_meeting(self, meeting: Any, body_id: Any) -> Any:
        self.aufrufe.append(("upsert_meeting", meeting.external_id))
        return uuid4()

    async def mark_entity_deleted(self, entity_type: str, external_id: str, modified: Any = None) -> Any:
        self.aufrufe.append(("mark_entity_deleted", entity_type, external_id))
        return uuid4()


def _orchestrator(speicher: MerkSpeicher) -> SyncOrchestrator:
    orch = SyncOrchestrator.__new__(SyncOrchestrator)
    orch.storage = speicher  # type: ignore[assignment]
    orch.processor = OParlProcessor()
    return orch


def test_upsert_einer_session_sitzung_nimmt_eigenes_ortsobjekt_zurueck() -> None:
    """Auch ohne Ortsangabe: Ein früher angelegtes Objekt darf nicht neben der Sitzung veralten."""
    speicher = MerkSpeicher()
    for daten in (_session_sitzung(location=_session_ort()), _session_sitzung()):
        meeting = OParlProcessor().process_meeting(daten, f"{BASIS}body/")
        asyncio.run(_orchestrator(speicher)._store_entity(meeting, uuid4(), "meeting", "Musterstadt"))

    assert (
        speicher.aufrufe
        == [
            ("upsert_meeting", f"{BASIS}meeting/7f0c/"),
            ("mark_entity_deleted", "location", f"{BASIS}location/7f0c/"),
        ]
        * 2
    )


def test_ruecknahme_einer_session_sitzung_nimmt_den_ort_mit() -> None:
    speicher = MerkSpeicher()
    grabstein = {"id": f"{BASIS}meeting/7f0c/", "deleted": True, "modified": "2026-09-30T10:00:00+02:00"}

    markiert = asyncio.run(_orchestrator(speicher)._mark_deleted(grabstein, "meeting", None))

    assert markiert is True
    assert speicher.aufrufe == [
        ("mark_entity_deleted", "location", f"{BASIS}location/7f0c/"),
        ("mark_entity_deleted", "meeting", f"{BASIS}meeting/7f0c/"),
    ]


def test_sitzungen_anderer_quellen_beruehren_keinen_ort() -> None:
    speicher = MerkSpeicher()
    orch = _orchestrator(speicher)
    fremd = "https://ris.example/oparl/meeting/7"
    meeting = OParlProcessor().process_meeting({"id": fremd, "name": "Rat"}, "https://ris.example/oparl/body/1")

    asyncio.run(orch._store_entity(meeting, uuid4(), "meeting", "Musterstadt"))
    asyncio.run(orch._mark_deleted({"id": fremd, "deleted": True}, "meeting", None))
    # Andere Objekttypen des Session-RIS: nur das Objekt selbst
    asyncio.run(orch._mark_deleted({"id": f"{BASIS}paper/7f0c/", "deleted": True}, "paper", None))

    assert speicher.aufrufe == [
        ("upsert_meeting", fremd),
        ("mark_entity_deleted", "meeting", fremd),
        ("mark_entity_deleted", "paper", f"{BASIS}paper/7f0c/"),
    ]

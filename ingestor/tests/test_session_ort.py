"""
Sitzungsort aus dem Session-RIS von mandari.

Die Session-OParl-Schnittstelle (``apps/session/api/oparl.py``) liefert den Ort einer Sitzung
als OParl-``location``-Objekt und zusätzlich als fertigen Text in ``mandari:locationName``,
``mandari:locationRoom`` und ``mandari:locationAddress`` (ältere Stände nur als Text). Der Ingestor
muss den Text genauso abbilden wie der Spiegel ``insight_sync/session_mirror.py`` – sonst verlieren
Session-Sitzungen im Bürgerportal ihren Ort, sobald der Ingestor-Daemon die Quelle synchronisiert.
"""

from __future__ import annotations

from typing import Any

from src.sync.processor import OParlProcessor

BASIS = "https://mandari.example/session/nord/api/oparl/"


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
    meeting = OParlProcessor().process_meeting(
        _session_sitzung(
            location={
                "id": "https://ris.example/location/1",
                "type": "https://schema.oparl.org/1.1/Location",
                "room": "Saal A",
                "streetAddress": "Hauptstraße 1",
            },
        ),
        "https://ris.example/body/1",
    )

    assert meeting.location_name == "Saal A"
    assert meeting.location_address == "Hauptstraße 1"
    assert meeting.location_external_id == "https://ris.example/location/1"


def test_session_mit_location_objekt_behaelt_ort_und_anschrift() -> None:
    """
    Session liefert Location-Objekt und Text. Im Bürgerportal bleibt der Ort wie bisher (Gebäude vor
    Raum, Anschrift mit Postleitzahl und Ort); das Location-Objekt wird zusätzlich übernommen.
    """
    location_id = f"{BASIS}location/7f0c/"
    meeting = OParlProcessor().process_meeting(
        _session_sitzung(
            location={
                "id": location_id,
                "type": "https://schema.oparl.org/1.1/Location",
                "description": "Rathaus",
                "room": "Ratssaal",
                "streetAddress": "Markt 1",
                "postalCode": "12345",
                "locality": "Musterstadt",
            },
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
    assert meeting.location_external_id == location_id
    assert [entity.external_id for entity in meeting.nested_entities] == [location_id]

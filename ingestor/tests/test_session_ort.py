"""
Sitzungsort aus dem Session-RIS von mandari.

Die Session-OParl-Schnittstelle (``apps/session/api/oparl.py``) liefert den Ort einer Sitzung
nicht als OParl-``location``-Objekt, sondern als Erweiterung ``mandari:locationName``,
``mandari:locationRoom`` und ``mandari:locationAddress``. Der Ingestor muss diese Felder
genauso abbilden wie der Spiegel ``insight_sync/session_mirror.py`` – sonst verlieren
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


def test_oparl_location_hat_vorrang() -> None:
    """Liefert eine Quelle ein echtes OParl-Location-Objekt, gilt es wie bisher."""
    meeting = OParlProcessor().process_meeting(
        _session_sitzung(
            location={
                "id": "https://ris.example/location/1",
                "type": "https://schema.oparl.org/1.1/Location",
                "room": "Saal A",
                "streetAddress": "Hauptstraße 1",
            },
            **{"mandari:locationName": "Rathaus", "mandari:locationAddress": "Markt 1"},
        ),
        "https://ris.example/body/1",
    )

    assert meeting.location_name == "Saal A"
    assert meeting.location_address == "Hauptstraße 1"
    assert meeting.location_external_id == "https://ris.example/location/1"

# SPDX-License-Identifier: AGPL-3.0-or-later
"""``import_streets --with-addresses``: Hausnummern-Punkte aus einer gefälschten Overpass-Antwort (Issue #54)."""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command

from insight_core.management.commands import import_streets
from insight_core.models import Address, OParlBody, Street

pytestmark = pytest.mark.django_db

STREET_ELEMENTS = [
    {
        "type": "way",
        "id": 100,
        "tags": {"name": "Wolbecker Straße", "highway": "residential"},
        "center": {"lat": 51.95, "lon": 7.64},
    },
    {
        "type": "way",
        "id": 101,
        "tags": {"name": "Wolbecker Straße", "highway": "residential"},
        "center": {"lat": 51.951, "lon": 7.641},
    },
    {"type": "way", "id": 102, "tags": {"highway": "residential"}, "center": {"lat": 51.95, "lon": 7.64}},
]

ADDRESS_ELEMENTS = [
    {
        "type": "node",
        "id": 1,
        "lat": 51.9501,
        "lon": 7.6402,
        "tags": {"addr:street": "Wolbecker Straße", "addr:housenumber": "12 a", "addr:postcode": "48155"},
    },
    {
        "type": "way",
        "id": 2,
        "center": {"lat": 51.9502, "lon": 7.6403},
        "tags": {"addr:street": "Wolbecker Str.", "addr:housenumber": "14"},
    },
    {"type": "node", "id": 3, "lat": 51.95, "lon": 7.64, "tags": {"addr:housenumber": "1"}},
    {"type": "node", "id": 4, "tags": {"addr:street": "Hafenweg", "addr:housenumber": "5"}},
]


class _FakeResponse:
    status_code = 200

    def __init__(self, elements: list[dict[str, Any]]) -> None:
        self._elements = elements

    def json(self) -> dict[str, Any]:
        return {"elements": self._elements}


def _fake_post(calls: list[str]) -> Any:
    def _post(url: str, data: dict[str, str], timeout: float, headers: dict[str, str]) -> _FakeResponse:
        query = data["data"]
        calls.append(query)
        if "addr:housenumber" in query:
            return _FakeResponse(ADDRESS_ELEMENTS)
        return _FakeResponse(STREET_ELEMENTS)

    return _post


def test_parse_address_element_handles_nodes_ways_and_gaps() -> None:
    node = import_streets.parse_address_element(ADDRESS_ELEMENTS[0])
    assert node is not None
    assert node["normalized_street"] == "wolbecker strasse"
    assert node["normalized_house_number"] == "12a"
    assert node["postal_code"] == "48155"
    assert (node["latitude"], node["longitude"]) == (51.9501, 7.6402)

    way = import_streets.parse_address_element(ADDRESS_ELEMENTS[1])
    assert way is not None
    assert way["osm_type"] == "way"
    assert way["normalized_street"] == "wolbecker strasse"

    assert import_streets.parse_address_element(ADDRESS_ELEMENTS[2]) is None  # ohne Straße
    assert import_streets.parse_address_element(ADDRESS_ELEMENTS[3]) is None  # ohne Koordinaten
    assert import_streets.parse_address_element({"type": "area", "id": 9}) is None


def test_import_with_addresses_is_idempotent(geo_body: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("insight_core.management.commands.import_streets.httpx.post", _fake_post(calls))
    monkeypatch.setattr("insight_core.management.commands.import_streets.time.sleep", lambda _seconds: None)

    out = StringIO()
    call_command("import_streets", body="beispielstadt", with_addresses=True, stdout=out)

    assert len(calls) == 2
    assert 'nwr["addr:housenumber"]["addr:street"]' in calls[1]
    assert Street.objects.filter(body=geo_body).count() == 2
    assert Address.objects.filter(body=geo_body).count() == 2
    assert "2 Adressen neu" in out.getvalue()

    address = Address.objects.get(body=geo_body, osm_type="way", osm_id=2)
    assert address.house_number == "14"
    assert address.normalized_street == "wolbecker strasse"

    # Zweiter Lauf: Upsert, keine Duplikate
    out = StringIO()
    call_command("import_streets", body="beispielstadt", with_addresses=True, stdout=out)
    assert Address.objects.filter(body=geo_body).count() == 2
    assert "0 Adressen neu, 2 aktualisiert" in out.getvalue()


def test_import_without_flag_skips_addresses(geo_body: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("insight_core.management.commands.import_streets.httpx.post", _fake_post(calls))
    call_command("import_streets", body="beispielstadt", stdout=StringIO())
    assert len(calls) == 1
    assert Address.objects.count() == 0

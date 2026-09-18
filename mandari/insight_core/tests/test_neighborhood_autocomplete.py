# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nachbarschafts-Autocomplete aus Straßen-/Adressverzeichnis, Photon nur als Fallback (Issue #54)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client

from insight_core.models import Address, OParlBody, Street
from insight_core.services.neighborhood import autocomplete_places

pytestmark = pytest.mark.django_db


@pytest.fixture
def street_body(
    geo_body: OParlBody, make_street: Callable[..., Street], make_address: Callable[..., Address]
) -> OParlBody:
    # Zwei Ways derselben Straße → eine Zeile, Zentroid gemittelt
    make_street(geo_body, "Wolbecker Straße", 51.950, 7.640)
    make_street(geo_body, "Wolbecker Straße", 51.952, 7.642)
    make_street(geo_body, "Wolfgang-Borchert-Weg", 51.96, 7.60)
    make_street(geo_body, "Am Wolbecker Hof", 51.97, 7.65)
    make_street(geo_body, "Hafenweg", 51.94, 7.63)
    make_address(geo_body, "Wolbecker Straße", "12a", 51.951, 7.641)
    make_address(geo_body, "Wolbecker Straße", "120", 51.953, 7.643)
    return geo_body


def _client_for(body: OParlBody) -> Client:
    client = Client()
    session = client.session
    session["active_body_id"] = str(body.id)
    session.save()
    return client


def test_prefix_before_substring_and_merged_centroid(street_body: OParlBody) -> None:
    results = autocomplete_places(street_body, "wolb")
    assert [r["name"] for r in results] == ["Wolbecker Straße", "Am Wolbecker Hof"]
    assert results[0]["lat"] == pytest.approx(51.951)
    assert results[0]["lon"] == pytest.approx(7.641)


def test_house_number_uses_address_points(street_body: OParlBody) -> None:
    results = autocomplete_places(street_body, "Wolbecker Str. 12")
    assert [r["name"] for r in results] == ["Wolbecker Straße 120", "Wolbecker Straße 12a"]
    assert results[1]["lat"] == pytest.approx(51.951)

    # Unbekannte Hausnummer → Straße als Rückfall
    results = autocomplete_places(street_body, "Wolbecker Straße 99")
    assert [r["name"] for r in results] == ["Wolbecker Straße"]


def test_limit_and_short_queries(street_body: OParlBody, make_street: Callable[..., Street]) -> None:
    for i in range(15):
        make_street(street_body, f"Testweg {i:02d}", 51.9, 7.6)
    assert len(autocomplete_places(street_body, "testweg")) == 10
    assert autocomplete_places(street_body, "t") == []


def test_view_uses_local_gazetteer_without_photon(street_body: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Photon darf mit Straßenverzeichnis nicht aufgerufen werden")

    monkeypatch.setattr("insight_core.services.neighborhood.httpx.get", _no_network)
    response = _client_for(street_body).get("/insight/nachbarschaft/autocomplete/", {"q": "hafen"})
    assert response.status_code == 200
    assert response.json() == [{"name": "Hafenweg", "lat": 51.94, "lon": 7.63}]


def test_view_falls_back_to_photon_without_streets(geo_body: OParlBody, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "features": [
                    {
                        "geometry": {"coordinates": [7.62, 51.96]},
                        "properties": {"name": "Prinzipalmarkt", "city": "Beispielstadt"},
                    },
                    # Außerhalb der Bounding-Box → verworfen
                    {"geometry": {"coordinates": [13.4, 52.5]}, "properties": {"name": "Alexanderplatz"}},
                ]
            }

    calls: list[dict[str, str]] = []

    def _fake_get(url: str, params: dict[str, str], timeout: float, headers: dict[str, str]) -> _Response:
        calls.append(params)
        return _Response()

    monkeypatch.setattr("insight_core.services.neighborhood.httpx.get", _fake_get)
    response = _client_for(geo_body).get("/insight/nachbarschaft/autocomplete/", {"q": "Prinzipal"})
    assert response.status_code == 200
    assert response.json() == [{"name": "Prinzipalmarkt, Beispielstadt", "lat": 51.96, "lon": 7.62}]
    assert calls and calls[0]["lat"] == str(geo_body.latitude)

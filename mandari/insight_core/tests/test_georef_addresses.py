# SPDX-License-Identifier: AGPL-3.0-or-later
"""Georeferenzierung bevorzugt Hausnummern-Punkte; jeder Lauf spiegelt in ``PaperLocation`` (Issue #54)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from insight_core.models import Address, OParlBody, OParlPaper, PaperLocation, Street
from insight_core.services.gazetteer import StreetGazetteer, normalize_house_number, resolve_address_point
from insight_core.services.georeferencing import extract_with_gazetteer, update_paper_georef
from insight_core.services.paper_locations import remove_location, sync_paper_locations

pytestmark = pytest.mark.django_db

STREET_LAT, STREET_LON = 51.9500, 7.6400
HOUSE_LAT, HOUSE_LON = 51.9530, 7.6450


@pytest.fixture
def gazetteer_body(
    geo_body: OParlBody, make_street: Callable[..., Street], make_address: Callable[..., Address]
) -> OParlBody:
    make_street(geo_body, "Wolbecker Straße", STREET_LAT, STREET_LON)
    make_street(geo_body, "Hafenweg", 51.94, 7.63)
    make_address(geo_body, "Wolbecker Straße", "12a", HOUSE_LAT, HOUSE_LON)
    return geo_body


def test_normalize_house_number() -> None:
    assert normalize_house_number("12 A") == "12a"
    assert normalize_house_number(" 7 ") == "7"
    assert normalize_house_number(None) == ""


def test_resolve_address_point(gazetteer_body: OParlBody) -> None:
    assert resolve_address_point(gazetteer_body, "wolbecker strasse", "12 A") == (HOUSE_LAT, HOUSE_LON)
    assert resolve_address_point(gazetteer_body, "wolbecker strasse", "99") is None
    assert resolve_address_point(gazetteer_body, "wolbecker strasse", None) is None


def test_extract_prefers_house_number_point(gazetteer_body: OParlBody) -> None:
    gazetteer = StreetGazetteer(gazetteer_body)
    text = "Der Antrag betrifft das Grundstück Wolbecker Str. 12a sowie den Hafenweg insgesamt."
    locations = extract_with_gazetteer(text, gazetteer, person_names=set())
    by_name = {loc["name"]: loc for loc in locations}

    assert by_name["Wolbecker Straße 12a"]["source"] == "address_match"
    assert (by_name["Wolbecker Straße 12a"]["lat"], by_name["Wolbecker Straße 12a"]["lon"]) == (HOUSE_LAT, HOUSE_LON)
    # Ohne Hausnummer bleibt der Straßen-Zentroid
    assert by_name["Hafenweg"]["source"] == "street_match"
    assert (by_name["Hafenweg"]["lat"], by_name["Hafenweg"]["lon"]) == (51.94, 7.63)


def test_extract_falls_back_to_street_centroid_without_address(gazetteer_body: OParlBody) -> None:
    gazetteer = StreetGazetteer(gazetteer_body)
    locations = extract_with_gazetteer("Baustelle Wolbecker Straße 77", gazetteer, person_names=set())
    assert len(locations) == 1
    assert locations[0]["name"] == "Wolbecker Straße 77"
    assert (locations[0]["lat"], locations[0]["lon"]) == (STREET_LAT, STREET_LON)


def test_refine_marks_precision(gazetteer_body: OParlBody) -> None:
    gazetteer = StreetGazetteer(gazetteer_body)
    hit = gazetteer.lookup("Wolbecker Straße 12a")
    assert hit is not None
    refined = gazetteer.refine(hit)
    assert refined["precision"] == "address"
    plain = gazetteer.lookup("Wolbecker Straße")
    assert plain is not None
    assert "precision" not in gazetteer.refine(plain)


def test_update_paper_georef_syncs_table_and_respects_removal(
    gazetteer_body: OParlBody, make_paper: Callable[..., OParlPaper]
) -> None:
    paper = make_paper(gazetteer_body)
    result: dict[str, Any] = {
        "status": "completed",
        "method": "gazetteer",
        "locations": [
            {
                "lat": HOUSE_LAT,
                "lon": HOUSE_LON,
                "name": "Wolbecker Straße 12a",
                "source": "address_match",
                "confidence": 0.9,
            },
            {"lat": 51.94, "lon": 7.63, "name": "Hafenweg", "source": "street_match", "confidence": 0.8},
        ],
    }
    update_paper_georef(paper, result)
    assert PaperLocation.objects.filter(paper=paper).count() == 2

    # Betreiber entfernt „Hafenweg“ – ein erneuter Lauf legt ihn nicht wieder an
    remove_location(PaperLocation.objects.get(paper=paper, name="Hafenweg"))
    update_paper_georef(paper, result)
    paper.refresh_from_db()
    assert paper.locations is not None
    assert [loc["name"] for loc in paper.locations] == ["Wolbecker Straße 12a"]
    assert PaperLocation.objects.filter(paper=paper, status=PaperLocation.STATUS_REMOVED).count() == 1
    assert PaperLocation.objects.filter(paper=paper, status=PaperLocation.STATUS_AUTO).count() == 1
    assert not sync_paper_locations(paper).changed

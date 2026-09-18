# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gemeinsame Fixtures für die Geo-Tests (Issue #54): Kommune, Vorgänge, Straßen, Adressen."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from insight_core.models import Address, OParlBody, OParlPaper, OParlSource, Street
from insight_core.services.gazetteer import normalize_house_number, normalize_street_name

# Münster-Zentrum als Bezugspunkt aller Geo-Tests
CENTER_LAT = 51.9606649
CENTER_LON = 7.6261347


@pytest.fixture
def geo_body(db: Any) -> OParlBody:
    source = OParlSource.objects.create(name="Beispielquelle", url="https://ris.beispielstadt.example/oparl/system")
    return OParlBody.objects.create(
        external_id="https://ris.beispielstadt.example/oparl/bodies/1",
        source=source,
        name="Beispielstadt",
        slug="beispielstadt",
        latitude=CENTER_LAT,
        longitude=CENTER_LON,
        bbox_north=CENTER_LAT + 0.1,
        bbox_south=CENTER_LAT - 0.1,
        bbox_east=CENTER_LON + 0.15,
        bbox_west=CENTER_LON - 0.15,
        osm_relation_id=62591,
        ags="05515000",
    )


@pytest.fixture
def make_paper(db: Any) -> Callable[..., OParlPaper]:
    """make_paper(body, locations=[...], **felder) → gespeicherter Vorgang."""

    def _make(body: OParlBody, locations: list[dict[str, Any]] | None = None, **fields: Any) -> OParlPaper:
        fields.setdefault("name", "Sanierung Spielplatz")
        fields.setdefault("reference", f"V/{uuid.uuid4().hex[:6]}")
        return OParlPaper.objects.create(
            external_id=f"https://ris.beispielstadt.example/oparl/papers/{uuid.uuid4()}",
            body=body,
            locations=locations,
            **fields,
        )

    return _make


@pytest.fixture
def make_street(db: Any) -> Callable[..., Street]:
    def _make(body: OParlBody, name: str, lat: float, lon: float, osm_id: int | None = None) -> Street:
        return Street.objects.create(
            body=body,
            osm_id=osm_id or Street.objects.count() + 1,
            name=name,
            normalized_name=normalize_street_name(name),
            latitude=lat,
            longitude=lon,
        )

    return _make


@pytest.fixture
def make_address(db: Any) -> Callable[..., Address]:
    def _make(body: OParlBody, street: str, house_number: str, lat: float, lon: float) -> Address:
        return Address.objects.create(
            body=body,
            osm_type="node",
            osm_id=Address.objects.count() + 1,
            street=street,
            normalized_street=normalize_street_name(street),
            house_number=house_number,
            normalized_house_number=normalize_house_number(house_number),
            latitude=lat,
            longitude=lon,
        )

    return _make

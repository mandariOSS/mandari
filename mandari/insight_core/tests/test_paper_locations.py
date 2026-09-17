# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verortungstabelle ``PaperLocation``: Abgleich mit dem JSON, Sperre entfernter Punkte,
Umkreissuche mit Bounding-Box-Vorfilter (Issue #54).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from insight_core.models import OParlBody, OParlPaper, PaperLocation
from insight_core.services.paper_locations import (
    bounding_box,
    confirm_location,
    haversine_m,
    nearby_papers,
    remove_location,
    sync_paper_locations,
)
from insight_core.tests.conftest import CENTER_LAT, CENTER_LON

pytestmark = pytest.mark.django_db

# 0.001° Breite ≈ 111 m
STEP = 0.001


def _loc(lat: float, lon: float, name: str = "Hauptstraße 1", source: str = "address_match") -> dict[str, Any]:
    return {"lat": lat, "lon": lon, "name": name, "source": source, "confidence": 0.9}


def test_sync_creates_updates_and_deletes_rows(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = make_paper(geo_body, locations=[_loc(CENTER_LAT, CENTER_LON), _loc(CENTER_LAT + STEP, CENTER_LON)])

    result = sync_paper_locations(paper)
    assert (result.created, result.updated, result.deleted) == (2, 0, 0)
    assert PaperLocation.objects.filter(paper=paper).count() == 2

    # Zweiter Lauf ohne Änderung ist ein No-op
    assert not sync_paper_locations(paper).changed

    # Name ändert sich, ein Punkt fällt weg
    paper.locations = [_loc(CENTER_LAT, CENTER_LON, name="Hauptstraße 1a")]
    paper.save(update_fields=["locations"])
    result = sync_paper_locations(paper)
    assert (result.created, result.updated, result.deleted) == (0, 1, 1)
    row = PaperLocation.objects.get(paper=paper)
    assert row.name == "Hauptstraße 1a"
    assert row.body_id == geo_body.id
    assert row.source == "address_match"


def test_sync_ignores_invalid_entries(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = make_paper(geo_body, locations=[{"name": "ohne Koordinaten"}, "kein dict", _loc(CENTER_LAT, CENTER_LON)])
    sync_paper_locations(paper)
    assert PaperLocation.objects.filter(paper=paper).count() == 1
    paper.refresh_from_db()
    assert paper.locations is not None
    assert len(paper.locations) == 1


def test_removed_location_is_suppressed_on_rerun(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    """Entfernte Verortung: verschwindet aus dem JSON und wird vom nächsten Lauf nicht wieder angelegt."""
    paper = make_paper(geo_body, locations=[_loc(CENTER_LAT, CENTER_LON), _loc(CENTER_LAT + STEP, CENTER_LON)])
    sync_paper_locations(paper)
    row = PaperLocation.objects.get(paper=paper, latitude=CENTER_LAT)

    remove_location(row)
    row.refresh_from_db()
    paper.refresh_from_db()
    assert row.status == PaperLocation.STATUS_REMOVED
    assert row.reviewed_at is not None
    assert paper.locations is not None
    assert [entry["lat"] for entry in paper.locations] == [round(CENTER_LAT + STEP, 7)]

    # Der automatische Lauf liefert den Punkt erneut (leicht versetzt, < 50 m): bleibt gesperrt
    paper.locations = [_loc(CENTER_LAT + 0.0001, CENTER_LON), _loc(CENTER_LAT + STEP, CENTER_LON)]
    paper.save(update_fields=["locations"])
    result = sync_paper_locations(paper)
    assert result.suppressed == 1
    paper.refresh_from_db()
    assert paper.locations is not None
    assert len(paper.locations) == 1
    statuses = list(PaperLocation.objects.filter(paper=paper).values_list("status", flat=True))
    assert sorted(statuses) == [PaperLocation.STATUS_AUTO, PaperLocation.STATUS_REMOVED]


def test_confirmed_location_survives_rerun(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = make_paper(geo_body, locations=[_loc(CENTER_LAT, CENTER_LON)])
    sync_paper_locations(paper)
    row = PaperLocation.objects.get(paper=paper)
    confirm_location(row)

    # Neuer Lauf ohne diesen Punkt: bestätigte Zeile bleibt und kehrt ins JSON zurück
    paper.locations = None
    paper.save(update_fields=["locations"])
    result = sync_paper_locations(paper)
    assert result.deleted == 0
    assert result.json_changed
    paper.refresh_from_db()
    assert paper.locations is not None
    assert paper.locations[0]["name"] == "Hauptstraße 1"
    assert PaperLocation.objects.get(paper=paper).status == PaperLocation.STATUS_CONFIRMED


def test_confirm_lifts_removal(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = make_paper(geo_body, locations=[_loc(CENTER_LAT, CENTER_LON)])
    sync_paper_locations(paper)
    row = PaperLocation.objects.get(paper=paper)
    remove_location(row)
    paper.refresh_from_db()
    assert paper.locations is None

    confirm_location(row)
    paper.refresh_from_db()
    assert paper.locations is not None
    assert len(paper.locations) == 1


def test_bounding_box_contains_radius() -> None:
    south, north, west, east = bounding_box(CENTER_LAT, CENTER_LON, 500)
    for lat, lon in ((north, CENTER_LON), (south, CENTER_LON), (CENTER_LAT, east), (CENTER_LAT, west)):
        assert haversine_m(CENTER_LAT, CENTER_LON, lat, lon) >= 499


def test_nearby_papers_filters_by_distance_and_dedupes(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
) -> None:
    near = make_paper(
        geo_body, name="Nah", locations=[_loc(CENTER_LAT + 0.002, CENTER_LON), _loc(CENTER_LAT + 0.0005, CENTER_LON)]
    )
    far = make_paper(geo_body, name="Fern", locations=[_loc(CENTER_LAT + 0.02, CENTER_LON)])
    deleted = make_paper(geo_body, name="Gelöscht", deleted=True, locations=[_loc(CENTER_LAT, CENTER_LON)])
    for paper in (near, far, deleted):
        sync_paper_locations(paper)

    results = nearby_papers(geo_body, CENTER_LAT, CENTER_LON, 500)
    assert [r["name"] for r in results] == ["Nah"]
    # Nächster Punkt je Vorgang (≈ 55 m), nicht der weiter entfernte
    assert results[0]["distance"] < 100
    assert results[0]["url"] == f"/insight/vorgaenge/{near.id}/"

    # Größerer Radius, sortiert nach Entfernung
    results = nearby_papers(geo_body, CENTER_LAT, CENTER_LON, 5000)
    assert [r["name"] for r in results] == ["Nah", "Fern"]


def test_nearby_papers_excludes_removed_rows(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = make_paper(geo_body, locations=[_loc(CENTER_LAT, CENTER_LON)])
    sync_paper_locations(paper)
    remove_location(PaperLocation.objects.get(paper=paper))
    assert nearby_papers(geo_body, CENTER_LAT, CENTER_LON, 500) == []


def test_nearby_papers_scales_with_index(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    """20 000 Verortungen: eine Abfrage, Antwort deutlich unter einer Sekunde (SQLite)."""
    rng = random.Random(54)
    papers = [
        OParlPaper(
            external_id=f"https://ris.beispielstadt.example/oparl/papers/perf-{i}",
            body=geo_body,
            name=f"Vorgang {i}",
            reference=f"P/{i}",
        )
        for i in range(2000)
    ]
    OParlPaper.objects.bulk_create(papers, batch_size=500)
    rows = [
        PaperLocation(
            paper=papers[i % 2000],
            body=geo_body,
            name=f"Ort {i}",
            source="street_match",
            latitude=CENTER_LAT + rng.uniform(-0.09, 0.09),
            longitude=CENTER_LON + rng.uniform(-0.14, 0.14),
        )
        for i in range(20_000)
    ]
    PaperLocation.objects.bulk_create(rows, batch_size=1000)
    # Ein garantierter Treffer direkt am Suchpunkt
    PaperLocation.objects.create(paper=papers[0], body=geo_body, latitude=CENTER_LAT, longitude=CENTER_LON)

    with CaptureQueriesContext(connection) as ctx:
        started = time.perf_counter()
        results = nearby_papers(geo_body, CENTER_LAT, CENTER_LON, 500)
        elapsed = time.perf_counter() - started

    assert len(ctx.captured_queries) == 1
    assert elapsed < 1.0, f"Umkreissuche zu langsam: {elapsed:.3f}s"
    assert results and results[0]["distance"] == 0
    assert all(r["distance"] <= 500 for r in results)
    assert len({r["id"] for r in results}) == len(results)

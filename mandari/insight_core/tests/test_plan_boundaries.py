# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Amtliche Umringe von Bebauungsplänen (Issue #598): Titel lesen, Quellen abrufen (nur gemockt),
Zwischenspeicher abgleichen, Vorlagen zuordnen, Umkreissuche und Vorgangsseite.
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable
from datetime import date
from io import StringIO
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.test import Client

from insight_core.models import (
    OParlBody,
    OParlPaper,
    PaperLocation,
    PaperPlanReference,
    PlanBoundary,
    PlanBoundarySource,
)
from insight_core.services import plan_boundaries as pb
from insight_core.services import polygon_geometry as geo
from insight_core.services.georeferencing import update_paper_georef
from insight_core.services.paper_locations import nearby_papers, remove_location
from insight_core.tests.conftest import CENTER_LAT, CENTER_LON

M_LAT = 1 / 111_320.0
M_LON = 1 / (111_320.0 * math.cos(math.radians(CENTER_LAT)))


def square(center_lat: float, center_lon: float, half_m: float) -> list[list[float]]:
    """Geschlossener Ring eines Quadrats (Länge, Breite) mit ``2 * half_m`` Metern Kantenlänge."""
    south, north = center_lat - half_m * M_LAT, center_lat + half_m * M_LAT
    west, east = center_lon - half_m * M_LON, center_lon + half_m * M_LON
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def feature(nr: str, ring: list[list[float]], **properties: Any) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": f"DE_05515000_{nr}",
        "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]},
        "properties": {
            "nr": nr,
            "officialTitle": properties.pop("title", "Gievenbeck – Oxford-Quartier"),
            "officialDocument": f"https://geo.beispielstadt.example/bplan?nr={nr}",
            "planTypeName.code": properties.pop("code", 1000),
            **properties,
        },
    }


@pytest.fixture(autouse=True)
def _kein_netz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kein Test darf einen Geodienst erreichen: Abrufe gehen nur über gemockte Funktionen."""

    def _blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Netzabruf im Test")

    monkeypatch.setattr(pb, "download_to", _blocked)
    monkeypatch.setattr(pb, "_sleep", lambda seconds: None)


@pytest.fixture
def nrw_source(geo_body: OParlBody) -> PlanBoundarySource:
    return PlanBoundarySource.objects.create(
        body=geo_body,
        name="Land NRW – rechtskräftige Bebauungspläne",
        kind=PlanBoundarySource.KIND_OGC_API,
        url="https://ogc.example/collections/spatialplan/items",
        query_params={"gkz": "05515000"},
        property_filter={"planTypeName.code": [1000]},
        number_property="nr",
        title_property="officialTitle",
        link_property="officialDocument",
        attribution="Land NRW, dl-de/by-2-0",
        license_url="https://www.govdata.de/dl-de/by-2-0",
        priority=200,
    )


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], list[str]]:
    """serve({url_teil: antwort}) → Liste der abgerufenen Adressen; Antworten nach Adressteil."""

    def _serve(responses: dict[str, Any]) -> list[str]:
        calls: list[str] = []

        def _get_json(url: str) -> Any:
            calls.append(url)
            for part, payload in responses.items():
                if part in url:
                    return payload
            raise AssertionError(f"Unerwartete Adresse {url}")

        monkeypatch.setattr(pb, "_get_json", _get_json)
        return calls

    return _serve


def collection(*features: dict[str, Any], next_url: str | None = None) -> dict[str, Any]:
    links = [{"rel": "next", "type": "application/geo+json", "href": next_url}] if next_url else []
    return {"type": "FeatureCollection", "features": list(features), "links": links}


# =============================================================================
# Titel und Plannummern
# =============================================================================


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("1. Änderung des Bebauungsplans Nr. 579 - Entwurf zur Veröffentlichung", [("579", "579", "1")]),
        ("Bebauungsplan Nr. 388 - 3. Änderung - Änderungsbeschluss", [("388", "388", "3")]),
        (
            "4. Änderung des Bebauungsplans Nr. 144 - Kenntnisnahme des Entwurfs [Erweiterung Zentralklinikum]",
            [("144", "144", "4")],
        ),
        ("Vorhabenbezogener Bebauungsplan Nr. 612: Hiltrup – Osttor", [("612", "612", "")]),
        ("Aufstellung des Bebauungsplanes Nr. 45 a – Wohnen am Kanal", [("45 a", "45a", "")]),
        ("Änderung Nr. 2 des Bebauungsplans Nr. 10", [("10", "10", "2")]),
        ("Bebauungspläne Nr. 12 und 13 – Aufhebung", [("12", "12", ""), ("13", "13", "")]),
        ("B-Plan Nr. 7 Aufhebungsbeschluss", [("7", "7", "")]),
        ("Bebauungsplan Nr. 579, 1. Änderung", [("579", "579", "1")]),
        ("Sanierung Spielplatz Hauptstraße", []),
        ("42. Änderung des Flächennutzungsplans", []),
        (None, []),
    ],
)
def test_parse_plan_references(title: str | None, expected: list[tuple[str, str, str]]) -> None:
    found = [(ref.number_label, ref.number_key, ref.change_number) for ref in pb.parse_plan_references(title)]
    assert found == expected


def test_split_source_number_reads_nrw_and_plain_numbers() -> None:
    assert pb.split_source_number("_579__1") == ("579", "579", "1")
    assert pb.split_source_number("_579__") == ("579", "579", "")
    assert pb.split_source_number("Albachten_7_A_") == ("Albachten 7 A", "albachten7a", "")
    # INSPIRE-Kennung (Münster-WFS „planid“, Objekt-ID der Landes-API)
    assert pb.split_source_number("DE_05515000__579__1") == ("579", "579", "1")
    assert pb.split_source_number("DE_05515000_Albachten_7_A_") == ("Albachten 7 A", "albachten7a", "")
    assert pb.split_source_number("Albachten7A") == ("Albachten7A", "albachten7a", "")
    assert pb.split_source_number("0579") == ("0579", "579", "")
    assert pb.split_source_number(None) == ("", "", "")


# =============================================================================
# Geometrie
# =============================================================================


def test_geometry_area_contains_distance_and_point_inside_l_shape() -> None:
    polygons = geo.polygons_from_geojson({"type": "Polygon", "coordinates": [square(CENTER_LAT, CENTER_LON, 50)]})
    assert geo.area_m2(polygons) == pytest.approx(10_000, rel=0.01)
    assert geo.contains(polygons, CENTER_LON, CENTER_LAT)
    assert geo.distance_m(polygons, CENTER_LON, CENTER_LAT) == 0
    assert geo.distance_m(polygons, CENTER_LON + 150 * M_LON, CENTER_LAT) == pytest.approx(100, abs=1)

    # L-Form: Der Schwerpunkt liegt außerhalb, der Punkt im Umring trotzdem darin
    w, s = CENTER_LON, CENTER_LAT
    l_shape = [
        [w, s],
        [w + 100 * M_LON, s],
        [w + 100 * M_LON, s + 10 * M_LAT],
        [w + 10 * M_LON, s + 10 * M_LAT],
        [w + 10 * M_LON, s + 100 * M_LAT],
        [w, s + 100 * M_LAT],
        [w, s],
    ]
    polygons = geo.polygons_from_geojson({"type": "Polygon", "coordinates": [l_shape]})
    lon, lat = geo.representative_point(polygons)
    assert geo.contains(polygons, lon, lat)

    # Loch: Ein Punkt im Loch liegt nicht in der Fläche
    with_hole = geo.polygons_from_geojson({"type": "Polygon", "coordinates": [square(s, w, 100), square(s, w, 20)]})
    assert not geo.contains(with_hole, w, s)
    assert geo.contains(with_hole, w + 50 * M_LON, s)


@pytest.mark.parametrize(
    "geometry",
    [
        None,
        {"type": "Point", "coordinates": [7.6, 51.9]},
        {"type": "Polygon", "coordinates": [[[7.6, 51.9], [7.7, 51.9]]]},
        {"type": "Polygon", "coordinates": [[[401962.8, 5757789.3], [401970.0, 5757789.3], [401970.0, 5757800.0]]]},
    ],
)
def test_invalid_geometries_are_rejected(geometry: Any) -> None:
    with pytest.raises(geo.GeometryError):
        geo.polygons_from_geojson(geometry)


# =============================================================================
# Abruf und Zwischenspeicher
# =============================================================================


def test_ogc_api_pages_are_followed_and_stored(
    nrw_source: PlanBoundarySource, serve: Callable[[dict[str, Any]], list[str]]
) -> None:
    calls = serve(
        {
            "offset=1": collection(feature("_579__1", square(CENTER_LAT, CENTER_LON, 20))),
            "items?": collection(
                feature("_579__", square(CENTER_LAT, CENTER_LON, 100)),
                feature("_2__", square(CENTER_LAT, CENTER_LON, 30), code=2000),  # Flächennutzungsplan
                next_url="https://ogc.example/collections/spatialplan/items?f=json&gkz=05515000&offset=1",
            ),
        }
    )

    result = pb.refresh_source(nrw_source)

    assert result.error == ""
    assert (result.fetched, result.stored, result.created) == (3, 2, 2)
    assert result.skipped == {"Filter": 1}
    assert "gkz=05515000" in calls[0] and "f=json" in calls[0] and "limit=1000" in calls[0]
    assert len(calls) == 2
    base = PlanBoundary.objects.get(source=nrw_source, change_number="")
    assert (base.plan_number, base.number_key, base.title) == ("579", "579", "Gievenbeck – Oxford-Quartier")
    assert base.document_url == "https://geo.beispielstadt.example/bplan?nr=_579__"
    assert geo.contains(geo.polygons_from_geojson(base.geometry), base.point_lon, base.point_lat)
    assert base.area_m2 == pytest.approx(40_000, rel=0.01)
    nrw_source.refresh_from_db()
    assert nrw_source.feature_count == 2 and nrw_source.last_success_at is not None


def test_refresh_updates_deletes_and_keeps_stock_on_empty_answer(
    nrw_source: PlanBoundarySource, serve: Callable[[dict[str, Any]], list[str]]
) -> None:
    serve(
        {
            "items?": collection(
                feature("_579__", square(CENTER_LAT, CENTER_LON, 100)),
                feature("_12__", square(CENTER_LAT, CENTER_LON, 10)),
            )
        }
    )
    pb.refresh_source(nrw_source)
    assert PlanBoundary.objects.count() == 2

    # Unverändert: nichts zu tun
    again = pb.refresh_source(nrw_source)
    assert (again.created, again.updated, again.deleted) == (0, 0, 0)

    # Titel geändert, Plan 12 entfällt
    serve({"items?": collection(feature("_579__", square(CENTER_LAT, CENTER_LON, 100), title="Neuer Titel"))})
    changed = pb.refresh_source(nrw_source)
    assert (changed.created, changed.updated, changed.deleted) == (0, 1, 1)
    assert PlanBoundary.objects.get().title == "Neuer Titel"

    # Leere Antwort eines Dienstes mit Bestand: nichts löschen, Fehler vermerken
    serve({"items?": collection()})
    empty = pb.refresh_source(nrw_source)
    assert "Bestand bleibt erhalten" in empty.error
    assert PlanBoundary.objects.count() == 1
    nrw_source.refresh_from_db()
    assert nrw_source.last_error == empty.error


def test_fetch_errors_are_fixed_texts_without_exception_details(
    nrw_source: PlanBoundarySource, monkeypatch: pytest.MonkeyPatch
) -> None:
    geheim = "interner-hostname.local:5432 Zugangsdaten"

    def _fail(target: io.BytesIO, url: str, **kwargs: Any) -> None:
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(geheim, request=request, response=httpx.Response(503, request=request))

    monkeypatch.setattr(pb, "download_to", _fail)
    result = pb.refresh_source(nrw_source)
    assert result.error == "Dienst antwortete mit HTTP 503"
    nrw_source.refresh_from_db()
    assert nrw_source.last_error == "Dienst antwortete mit HTTP 503"
    assert geheim not in nrw_source.last_error

    def _broken(target: io.BytesIO, url: str, **kwargs: Any) -> None:
        target.write(b"<html>kein JSON</html>")

    monkeypatch.setattr(pb, "download_to", _broken)
    assert pb.refresh_source(nrw_source).error == "Antwort ist kein gültiges GeoJSON"


def test_wfs_request_parameters_and_axis_order(
    geo_body: OParlBody, serve: Callable[[dict[str, Any]], list[str]]
) -> None:
    source = PlanBoundarySource.objects.create(
        body=geo_body,
        name="Stadt – Bebauungspläne im Verfahren",
        kind=PlanBoundarySource.KIND_WFS,
        url="https://geo.example/mapserv/bplan_serv",
        layer="ms:bplan1",
        query_params={"outputFormat": "GEOJSON"},
        number_property="plannr",
        link_property="scanurl",
        plan_status=PlanBoundarySource.PLAN_STATUS_IN_PROCEDURE,
        attribution="Stadt Beispielstadt",
    )
    ring = square(CENTER_LAT, CENTER_LON, 50)
    swapped = [[lat, lon] for lon, lat in ring]  # Dienst liefert Breite vor Länge
    calls = serve(
        {
            "bplan_serv": collection(
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [swapped]},
                    "properties": {"plannr": "579", "name": "Oxford-Quartier", "scanurl": "javascript:alert(1)"},
                },
                # Fremde Kommune (Fehlkonfiguration): wird übersprungen
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [square(50.94, 6.96, 50)]},
                    "properties": {"plannr": "1"},
                },
            )
        }
    )

    result = pb.refresh_source(source)

    url = calls[0]
    for part in (
        "SERVICE=WFS",
        "REQUEST=GetFeature",
        "TYPENAMES=ms%3Abplan1",
        "SRSNAME=EPSG%3A4326",
        "outputFormat=GEOJSON",
    ):
        assert part in url
    assert "OUTPUTFORMAT=application" not in url
    assert result.skipped == {"außerhalb der Kommune": 1}
    boundary = PlanBoundary.objects.get(source=source)
    assert boundary.plan_status == PlanBoundarySource.PLAN_STATUS_IN_PROCEDURE
    assert boundary.document_url == ""  # nur http(s)-Links
    assert abs(boundary.point_lat - CENTER_LAT) < 0.001 and abs(boundary.point_lon - CENTER_LON) < 0.001


# =============================================================================
# Zuordnung zu Vorlagen
# =============================================================================


def _boundary(source: PlanBoundarySource, nr: str, half_m: float = 100.0, **fields: Any) -> PlanBoundary:
    label, key, change = pb.split_source_number(nr)
    polygons = geo.polygons_from_geojson({"type": "Polygon", "coordinates": [square(CENTER_LAT, CENTER_LON, half_m)]})
    lon, lat = geo.representative_point(polygons)
    south, north, west, east = geo.bounding_box(polygons)
    values: dict[str, Any] = {
        "source": source,
        "body": source.body,
        "feature_key": f"{nr}-{PlanBoundary.objects.count()}",
        "plan_number": label,
        "number_key": key,
        "change_number": change,
        "plan_status": source.plan_status,
        "geometry": geo.to_geojson(polygons),
        "bbox_south": south,
        "bbox_north": north,
        "bbox_west": west,
        "bbox_east": east,
        "point_lat": lat,
        "point_lon": lon,
        "area_m2": geo.area_m2(polygons),
        "fetched_at": "2026-09-29T00:00:00Z",
        "document_url": f"https://geo.beispielstadt.example/bplan?nr={nr}",
    }
    values.update(fields)
    return PlanBoundary.objects.create(**values)


def test_link_matches_change_writes_location_and_is_idempotent(
    geo_body: OParlBody, nrw_source: PlanBoundarySource, make_paper: Callable[..., OParlPaper]
) -> None:
    _boundary(nrw_source, "_579__")
    change = _boundary(nrw_source, "_579__1", half_m=20)
    paper = make_paper(geo_body, name="1. Änderung des Bebauungsplans Nr. 579 - Entwurf zur Veröffentlichung")
    gap = make_paper(geo_body, name="Bebauungsplan Nr. 999 – Aufstellungsbeschluss")
    make_paper(geo_body, name="Sanierung Spielplatz")

    result = pb.link_body_papers(geo_body)

    assert (result.papers, result.references, result.matched, result.unmatched) == (2, 2, 1, 1)
    reference = PaperPlanReference.objects.get(paper=paper)
    assert reference.match == PaperPlanReference.MATCH_CHANGE
    assert list(reference.boundaries.all()) == [change]
    paper.refresh_from_db()
    assert paper.locations is not None
    assert paper.locations[0]["source"] == "plan_boundary"
    assert paper.locations[0]["name"] == "Bebauungsplan Nr. 579, 1. Änderung (amtlicher Umring)"
    assert PaperLocation.objects.filter(paper=paper, source="plan_boundary").count() == 1

    # Lücke protokolliert, ohne Verortung
    missing = PaperPlanReference.objects.get(paper=gap)
    assert missing.match == PaperPlanReference.MATCH_NONE
    gap.refresh_from_db()
    assert gap.locations is None

    # Zweiter Lauf: nichts zu schreiben
    again = pb.link_body_papers(geo_body)
    assert (again.papers_changed, again.locations_changed) == (0, 0)


def test_link_prefers_procedure_then_base_plan(
    geo_body: OParlBody, nrw_source: PlanBoundarySource, make_paper: Callable[..., OParlPaper]
) -> None:
    base = _boundary(nrw_source, "_388__")
    paper = make_paper(geo_body, name="Bebauungsplan Nr. 388 - 3. Änderung - Änderungsbeschluss")
    pb.link_body_papers(geo_body)
    reference = PaperPlanReference.objects.get(paper=paper)
    assert reference.match == PaperPlanReference.MATCH_BASE_PLAN
    assert list(reference.boundaries.all()) == [base]

    procedure_source = PlanBoundarySource.objects.create(
        body=geo_body,
        name="Stadt – im Verfahren",
        kind=PlanBoundarySource.KIND_WFS,
        url="https://geo.example/wfs",
        layer="ms:bplan1",
        plan_status=PlanBoundarySource.PLAN_STATUS_IN_PROCEDURE,
        attribution="Stadt Beispielstadt",
        priority=10,
    )
    procedure = _boundary(procedure_source, "388", half_m=30)
    pb.link_body_papers(geo_body)
    reference.refresh_from_db()
    assert reference.match == PaperPlanReference.MATCH_PROCEDURE
    assert list(reference.boundaries.all()) == [procedure]
    paper.refresh_from_db()
    assert paper.locations is not None
    assert paper.locations[0]["name"] == "Bebauungsplan Nr. 388, 3. Änderung (Umring im Verfahren)"


def test_title_change_removes_reference_and_location(
    geo_body: OParlBody, nrw_source: PlanBoundarySource, make_paper: Callable[..., OParlPaper]
) -> None:
    _boundary(nrw_source, "_579__")
    street = {"lat": round(CENTER_LAT + 0.01, 7), "lon": CENTER_LON, "name": "Hauptstraße", "source": "street_match"}
    paper = make_paper(geo_body, name="Bebauungsplan Nr. 579", locations=[street])
    pb.link_body_papers(geo_body)
    paper.refresh_from_db()
    assert paper.locations is not None
    assert [loc["source"] for loc in paper.locations] == ["plan_boundary", "street_match"]

    paper.name = "Spielplatz an der Hauptstraße"
    paper.save(update_fields=["name"])
    pb.link_body_papers(geo_body)
    paper.refresh_from_db()
    assert paper.locations == [street]
    assert not PaperPlanReference.objects.filter(paper=paper).exists()
    assert not PaperLocation.objects.filter(paper=paper, source="plan_boundary").exists()


def test_georef_rerun_keeps_plan_location(
    geo_body: OParlBody, nrw_source: PlanBoundarySource, make_paper: Callable[..., OParlPaper]
) -> None:
    _boundary(nrw_source, "_579__")
    paper = make_paper(geo_body, name="Bebauungsplan Nr. 579")
    pb.link_body_papers(geo_body)
    paper.refresh_from_db()

    update_paper_georef(paper, {"status": "no_locations", "locations": []})

    paper.refresh_from_db()
    assert paper.locations is not None
    assert [loc["source"] for loc in paper.locations] == ["plan_boundary"]


def test_removed_plan_location_stays_removed_everywhere(
    geo_body: OParlBody, nrw_source: PlanBoundarySource, make_paper: Callable[..., OParlPaper]
) -> None:
    _boundary(nrw_source, "_579__")
    paper = make_paper(geo_body, name="Bebauungsplan Nr. 579")
    pb.link_body_papers(geo_body)
    remove_location(PaperLocation.objects.get(paper=paper, source="plan_boundary"))

    result = pb.link_body_papers(geo_body)

    assert result.locations_changed == 0
    paper.refresh_from_db()
    assert paper.locations is None
    assert pb.paper_plan_context(paper) == []
    assert nearby_papers(geo_body, CENTER_LAT, CENTER_LON, 100) == []


# =============================================================================
# Umkreissuche und Vorgangsseite
# =============================================================================


def test_nearby_papers_finds_paper_when_search_point_touches_boundary(
    geo_body: OParlBody, nrw_source: PlanBoundarySource, make_paper: Callable[..., OParlPaper]
) -> None:
    _boundary(nrw_source, "_579__", half_m=400)  # 800 m breiter Umring, Punkt in der Mitte
    paper = make_paper(geo_body, name="Bebauungsplan Nr. 579")
    pb.link_body_papers(geo_body)

    # Suchpunkt am Rand des Umrings, 350 m vom Punkt im Umring entfernt: Radius 100 m reicht
    inside = nearby_papers(geo_body, CENTER_LAT, CENTER_LON + 350 * M_LON, 100)
    assert [(item["id"], item["distance"]) for item in inside] == [(str(paper.id), 0)]

    # 50 m außerhalb des Umrings
    outside = nearby_papers(geo_body, CENTER_LAT, CENTER_LON + 450 * M_LON, 100)
    assert len(outside) == 1 and 45 <= outside[0]["distance"] <= 55

    # Zu weit weg
    assert nearby_papers(geo_body, CENTER_LAT, CENTER_LON + 700 * M_LON, 100) == []

    # Gelöschte Vorgänge nie
    paper.deleted = True
    paper.save(update_fields=["deleted"])
    assert nearby_papers(geo_body, CENTER_LAT, CENTER_LON, 100) == []


def test_paper_page_shows_boundary_source_and_plan_link(
    geo_body: OParlBody, nrw_source: PlanBoundarySource, make_paper: Callable[..., OParlPaper]
) -> None:
    _boundary(nrw_source, "_579__")
    nrw_source.attribution = "Land NRW <b>dl-de/by-2-0</b>"
    nrw_source.save(update_fields=["attribution"])
    paper = make_paper(geo_body, name="Bebauungsplan Nr. 579 – Satzungsbeschluss")
    pb.link_body_papers(geo_body)

    html = Client().get(f"/insight/vorgaenge/{paper.id}/").content.decode()

    assert "Quelle: Land NRW &lt;b&gt;dl-de/by-2-0&lt;/b&gt;" in html
    assert 'href="https://geo.beispielstadt.example/bplan?nr=_579__"' in html
    assert "Amtlicher Umring, Rechtskräftig" in html
    assert ">amtlich<" in html  # Verortung aus dem Umring in der Ortsliste
    assert 'id="paper-map-data"' in html and '"areas": [{"name": "Bebauungsplan Nr. 579"' in html
    assert "L.map(" not in html  # Kartenlogik steckt in frontend/js/paper-map.ts


def test_paper_page_without_places_has_no_map(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = make_paper(geo_body, name="Haushaltssatzung 2027")
    html = Client().get(f"/insight/vorgaenge/{paper.id}/").content.decode()
    assert 'id="paper-map"' not in html


# =============================================================================
# Befehl
# =============================================================================


def test_command_adds_nrw_source_and_reports_coverage(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper], serve: Callable[[dict[str, Any]], list[str]]
) -> None:
    out = StringIO()
    call_command("sync_plan_boundaries", "--add-nrw-source", "--body", "beispielstadt", stdout=out)
    source = PlanBoundarySource.objects.get(body=geo_body)
    assert source.query_params == {"gkz": "05515000"}
    assert source.number_property == "nr"

    serve({"items?": collection(feature("_579__", square(CENTER_LAT, CENTER_LON, 100)))})
    make_paper(
        geo_body, name="Bebauungsplan Nr. 579 – Satzungsbeschluss", date=date(2026, 9, 1), reference="V/0001/2026"
    )
    make_paper(geo_body, name="Bebauungsplan Nr. 640 – Aufstellung", date=date(2026, 8, 1), reference="V/0002/2026")
    make_paper(geo_body, name="Bebauungsplan Nr. 1 – alt", date=date(2019, 1, 1), reference="V/0003/2019")

    out = StringIO()
    call_command("sync_plan_boundaries", stdout=out)
    text = out.getvalue()

    assert "1 Objekte, 1 Umringe (neu 1" in text
    assert "3 Vorgänge mit Plannummer, 3 Bezüge (1 mit Umring, 2 ohne)" in text
    assert "2 Vorlagen mit Plannummer, 1 mit Umring (50,0 %), 1 Lücken" in text
    assert "Lücke: V/0002/2026 (01.08.2026) – Bebauungsplan Nr. 640" in text
    assert "V/0003/2019" not in text  # außerhalb des Zeitraums


def test_command_dry_run_saves_nothing(
    geo_body: OParlBody,
    nrw_source: PlanBoundarySource,
    make_paper: Callable[..., OParlPaper],
    serve: Callable[[dict[str, Any]], list[str]],
) -> None:
    serve({"items?": collection(feature("_579__", square(CENTER_LAT, CENTER_LON, 100)))})
    paper = make_paper(geo_body, name="Bebauungsplan Nr. 579")
    call_command("sync_plan_boundaries", "--dry-run", stdout=StringIO())
    assert not PlanBoundary.objects.exists()
    assert not PaperPlanReference.objects.exists()
    paper.refresh_from_db()
    assert paper.locations is None

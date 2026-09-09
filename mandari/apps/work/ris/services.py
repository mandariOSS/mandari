# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwendungsfälle der RIS-Datenansicht im Work-Portal (Issue #160, Service-Layer).

Die RIS-Ansicht ist rein lesend; hier liegen die Fälle, die mehr als einen
Selector kombinieren oder externe Dienste ansprechen: die Suche über
Elasticsearch mit ORM-Fallback sowie Kartenkonfiguration und GeoJSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

from insight_core.models import OParlBody

from . import selectors
from .selectors import Bodies

logger = logging.getLogger(__name__)

SEARCH_PAGE_SIZE = 25
RESULT_TYPES = ("papers", "meetings", "persons", "organizations", "files")
DEFAULT_MAP_CENTER = (51.5, 7.5)
DEFAULT_MAP_ZOOM = 13


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


@dataclass
class SearchQuery:
    """Vom Nutzer gesetzte Suchparameter (bereits bereinigt)."""

    query: str = ""
    date_from: str = ""
    date_to: str = ""
    committee: str = ""
    paper_type: str = ""
    result_type: str = ""
    body_filter: str = ""
    page: int = 1

    @property
    def has_filters(self) -> bool:
        return any([self.date_from, self.date_to, self.committee, self.paper_type, self.body_filter])

    @property
    def is_empty(self) -> bool:
        return not self.query and not self.has_filters

    def index_names(self) -> list[str] | None:
        """Elasticsearch-Indexe, auf die die Filter wirken (``None`` = alle)."""
        if self.result_type in RESULT_TYPES:
            return [self.result_type]
        if self.committee or self.paper_type:
            # Diese Filter wirken nur auf Dokument-/Sitzungs-Indexe
            return ["papers"] if self.paper_type else ["papers", "meetings", "files"]
        return None


@dataclass
class SearchResult:
    """Ergebnis der Suche; ``backend`` ist ``elasticsearch`` oder ``orm``."""

    backend: str
    total: int = 0
    page: int = 1
    pages: int = 1
    es_results: list[dict[str, Any]] = field(default_factory=list)
    orm_results: dict[str, Any] = field(default_factory=dict)


def resolve_search_bodies(bodies: Bodies, body_filter: str) -> tuple[list[str], str]:
    """
    Kommunen-Filter anwenden.

    Returns:
        (zu durchsuchende Body-IDs, wirksamer Filterwert – leer, wenn ungültig)
    """
    all_body_ids = [str(b.id) for b in bodies]
    if body_filter and body_filter in all_body_ids:
        return [body_filter], body_filter
    return all_body_ids, ""


def _elasticsearch_search(params: SearchQuery, body_ids: list[str]) -> SearchResult:
    from insight_core.services.search_service import ElasticsearchService

    service: Any = cast(Any, ElasticsearchService)()
    result = service.search_all(
        query=params.query,
        body_ids=body_ids,
        page=params.page,
        page_size=SEARCH_PAGE_SIZE,
        index_names=params.index_names(),
        date_from=params.date_from or None,
        date_to=params.date_to or None,
        organization_name=params.committee or None,
        paper_type=params.paper_type or None,
    )
    # Django-Templates erlauben keinen Zugriff auf _-Attribute
    for doc in result["results"]:
        doc["formatted"] = doc.get("_formatted", {})
    return SearchResult(
        backend="elasticsearch",
        total=result["total"],
        page=result["page"],
        pages=result["pages"],
        es_results=result["results"],
    )


def _orm_search(params: SearchQuery, body_ids: list[str]) -> SearchResult:
    results = selectors.orm_search(
        body_ids,
        query=params.query,
        date_from=params.date_from,
        date_to=params.date_to,
        paper_type=params.paper_type,
        committee=params.committee,
    )
    total = results.pop("total")
    return SearchResult(backend="orm", total=total, orm_results=results)


def search(params: SearchQuery, body_ids: list[str]) -> SearchResult:
    """
    RIS-Suche über Elasticsearch (inkl. OCR-Volltexte) mit ORM-Fallback.

    Fällt auf eine einfache ORM-Suche zurück, wenn Elasticsearch nicht erreichbar ist.
    """
    try:
        return _elasticsearch_search(params, body_ids)
    except Exception as exc:  # Fallback bewusst breit: jeder ES-/Netzwerkfehler darf die Suche nicht brechen
        logger.warning(f"RIS-Suche: Elasticsearch nicht verfügbar, ORM-Fallback: {exc}")
    return _orm_search(params, body_ids)


# ---------------------------------------------------------------------------
# Karte
# ---------------------------------------------------------------------------


def map_config(body: OParlBody) -> dict[str, Any]:
    """Kartenzentrum, Zoom und Begrenzungsrahmen der primären Kommune."""
    return {
        "center_lat": float(body.latitude) if body.latitude else DEFAULT_MAP_CENTER[0],
        "center_lng": float(body.longitude) if body.longitude else DEFAULT_MAP_CENTER[1],
        "zoom": DEFAULT_MAP_ZOOM,
        "bbox": {
            "north": float(body.bbox_north) if body.bbox_north else None,
            "south": float(body.bbox_south) if body.bbox_south else None,
            "east": float(body.bbox_east) if body.bbox_east else None,
            "west": float(body.bbox_west) if body.bbox_west else None,
        }
        if body.bbox_north
        else None,
    }


def geojson_features(bodies: Bodies) -> dict[str, Any]:
    """GeoJSON-FeatureCollection aller georeferenzierten Vorgänge (ein Punkt je Ort)."""
    features: list[dict[str, Any]] = []
    for paper in selectors.papers_with_locations(bodies):
        if not paper.locations:
            continue
        for location in paper.locations:
            if not isinstance(location, dict):
                continue
            lat = location.get("lat") or location.get("latitude")
            lng = location.get("lng") or location.get("lon") or location.get("longitude")
            if not (lat and lng):
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(lng), float(lat)]},
                    "properties": {
                        "id": str(paper.id),
                        "title": paper.name or "Vorgang",
                        "reference": paper.reference,
                        "date": paper.date.isoformat() if paper.date else None,
                        "location_name": location.get("name", ""),
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}

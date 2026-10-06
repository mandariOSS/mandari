# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwendungsfälle der RIS-Datenansicht im Work-Portal (Issue #160, Service-Layer).

Die RIS-Ansicht ist rein lesend; hier liegen die Fälle, die mehr als einen
Selector kombinieren oder externe Dienste ansprechen: die Suche über
Elasticsearch mit ORM-Fallback sowie Kartenkonfiguration und die Punkte der
Karte (Ausschnitt und Zeitraum, Issue #853).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, cast

from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

from hub.ris import selectors as ris
from insight_core.models import OParlBody

# ``SearchQuery`` und ``RESULT_TYPES`` liegen seit 10/2026 in ``search_filters`` (Issue #853); die Namen bleiben hier
from insight_core.services.search_filters import RESULT_TYPES as RESULT_TYPES
from insight_core.services.search_filters import SearchQuery as SearchQuery

from . import selectors
from .selectors import Bodies

logger = logging.getLogger(__name__)

SEARCH_PAGE_SIZE = 25
DEFAULT_MAP_CENTER = (51.5, 7.5)
DEFAULT_MAP_ZOOM = 13


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


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


#: Felder, deren Treffer-Hervorhebung die Suchseite als HTML zeigt
HIGHLIGHT_FIELDS = ("name", "text_content")


def highlight_html(value: Any) -> SafeString:
    """
    Treffer-Hervorhebung als HTML: alles maskiert, nur die Such-Markierung bleibt.

    Elasticsearch fügt ``HIGHLIGHT_PRE``/``HIGHLIGHT_POST`` in den unmaskierten Quelltext ein;
    Namen und Volltexte stammen aus fremden Ratsinformationssystemen und PDFs.
    """
    from insight_core.services.search_presentation import clean_snippet
    from insight_core.services.search_service import HIGHLIGHT_POST, HIGHLIGHT_PRE

    teile: list[str] = []
    # Symbolschrift- und Steuerzeichen sowie offene Silbentrennung aus PDF-Texten vor dem Maskieren entfernen
    for index, abschnitt in enumerate(clean_snippet(str(value or "")).split(HIGHLIGHT_PRE)):
        if index == 0:
            teile.append(escape(abschnitt))
            continue
        markiert, _, rest = abschnitt.partition(HIGHLIGHT_POST)
        teile.append(f"{HIGHLIGHT_PRE}{escape(markiert)}{HIGHLIGHT_POST}{escape(rest)}")
    return mark_safe("".join(teile))


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
    # Django-Templates erlauben keinen Zugriff auf _-Attribute; als HTML nur maskiert mit Markierung
    for doc in result["results"]:
        formatted = doc.get("_formatted") or {}
        doc["formatted"] = {name: highlight_html(formatted[name]) for name in HIGHLIGHT_FIELDS if formatted.get(name)}
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


#: Zeiträume der Karte (Issue #853): Schlüssel aus der Adresse → Monate, ``None`` = ohne Grenze
KARTE_ZEITRAEUME: dict[str, int | None] = {"3": 3, "12": 12, "36": 36, "alle": None}
KARTE_ZEITRAUM_STANDARD = "12"
KARTE_ZEITRAUM_NAMEN = {"3": "3 Monate", "12": "12 Monate", "36": "3 Jahre", "alle": "Alle"}
#: Höchstens so viele Punkte je Antwort; mehr im Ausschnitt meldet die Antwort mit ``truncated``
KARTE_HOECHSTENS = 2000


def karte_zeitraum(raw: str | None) -> str:
    """Gewählter Zeitraum der Karte; Unbekanntes ergibt den Standard (12 Monate)."""
    return raw if raw in KARTE_ZEITRAEUME else KARTE_ZEITRAUM_STANDARD


def karte_seite(body: OParlBody, params: Mapping[str, str]) -> dict[str, Any]:
    """Kontext der Kartenseite: Zentrum und Rahmen der primären Kommune, die Zeiträume und der gewählte."""
    return {
        "map_config": map_config(body),
        "karte_zeitraeume": list(KARTE_ZEITRAUM_NAMEN.items()),
        "karte_zeitraum": karte_zeitraum(params.get("zeitraum")),
    }


def karte_ausschnitt(raw: str | None) -> ris.Area | None:
    """Kartenausschnitt aus ``west,süd,ost,nord`` (Leaflet ``toBBoxString``); ungültig oder leer ergibt ``None``."""
    if not raw:
        return None
    try:
        west, south, east, north = (float(teil) for teil in raw.split(","))
    except ValueError:
        return None
    gueltig = -180 <= west <= east <= 180 and -90 <= south <= north <= 90
    return (west, south, east, north) if gueltig else None


def karte_daten(bodies: Bodies, params: Mapping[str, str], *, heute: date | None = None) -> dict[str, Any]:
    """
    Punkte der Karte als GeoJSON: Vorgänge der Kommunen im Ausschnitt (``bbox``) und Zeitraum (``zeitraum``),
    neueste zuerst, ohne gelöschte. Sind es mehr als ``KARTE_HOECHSTENS``, steht ``truncated`` in der Antwort und
    die Karte bittet ums Hineinzoomen – kein stilles Abschneiden wie früher bei den 500 neuesten.
    """
    zeitraum = karte_zeitraum(params.get("zeitraum"))
    monate = KARTE_ZEITRAEUME[zeitraum]
    seit = None
    if monate is not None:
        seit = (heute or timezone.localdate()) - timedelta(days=monate * 365 // 12)
    orte = ris.paper_places(bodies, area=karte_ausschnitt(params.get("bbox")), since=seit, limit=KARTE_HOECHSTENS + 1)
    truncated = len(orte) > KARTE_HOECHSTENS
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [ort.longitude, ort.latitude]},
            "properties": {
                "id": str(ort.paper_id),
                "title": ort.title,
                "reference": ort.reference,
                "date": ort.paper_date.isoformat() if ort.paper_date else None,
                "location_name": ort.place,
            },
        }
        for ort in orte[:KARTE_HOECHSTENS]
    ]
    return {"type": "FeatureCollection", "features": features, "truncated": truncated, "zeitraum": zeitraum}

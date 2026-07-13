# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import logging
from datetime import timedelta

import httpx
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from ..models import (
    OParlPaper,
    TileCache,
)
from ._helpers import get_active_body

# =============================================================================
# Karte
# =============================================================================


class MapView(TemplateView):
    """Kartenansicht mit Vorgängen der letzten 4 Wochen."""

    template_name = "pages/map.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)

        if body:
            context["active_body"] = body

            # Geodaten für initiale Kartenansicht
            if body.latitude and body.longitude:
                context["map_center"] = {
                    "lat": float(body.latitude),
                    "lng": float(body.longitude),
                }

            # Bounding Box für Zoom
            if body.bbox_north and body.bbox_south and body.bbox_east and body.bbox_west:
                context["map_bounds"] = {
                    "north": float(body.bbox_north),
                    "south": float(body.bbox_south),
                    "east": float(body.bbox_east),
                    "west": float(body.bbox_west),
                }

        return context


@require_GET
def map_markers(request):
    """GeoJSON-Endpoint für Karten-Marker.

    Query-Parameter:
        weeks: Anzahl Wochen zurück (Standard: 4, Max: 52)
        all: Wenn "1", alle Papers mit Locations (kein Zeitfilter)
    """
    body = get_active_body(request)
    if not body:
        return JsonResponse({"type": "FeatureCollection", "features": []})

    papers = OParlPaper.objects.filter(body=body, locations__isnull=False)

    # Zeitfilter: ?all=1 deaktiviert den Filter
    show_all = request.GET.get("all") == "1"
    if not show_all:
        weeks = min(int(request.GET.get("weeks", "4") or "4"), 52)
        cutoff = timezone.now() - timedelta(weeks=weeks)
        papers = papers.filter(date__gte=cutoff)

    features = []
    for paper in papers:
        if paper.locations and isinstance(paper.locations, list):
            for loc in paper.locations:
                if "lat" in loc and "lon" in loc:
                    features.append(
                        {
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [loc["lon"], loc["lat"]]},
                            "properties": {
                                "id": str(paper.id),
                                "title": paper.name,
                                "reference": paper.reference,
                                "url": f"/insight/vorgaenge/{paper.id}/",
                                "location_name": loc.get("name", ""),
                            },
                        }
                    )

    return JsonResponse({"type": "FeatureCollection", "features": features})


# =============================================================================
# Tile Proxy (DSGVO-konform)
# =============================================================================


@require_GET
def tile_proxy(request, z, x, y):
    """
    Proxy für OpenStreetMap Raster-Tiles (für Leaflet).

    1. Prüft zuerst den lokalen Tile-Cache (Datenbank)
    2. Falls nicht im Cache, lädt von OSM und speichert im Cache
    3. Liefert das Tile aus

    Dies ist 100% DSGVO-konform, da alle Tiles serverseitig geladen werden.
    OSM Tile Usage Policy: https://operations.osmfoundation.org/policies/tiles/
    """
    # 1. Prüfe den lokalen Cache
    tile_data, content_type = TileCache.get_tile(z, x, y)

    if tile_data:
        # Tile aus Cache liefern (super schnell!)
        # SECURITY NOTE: CORS "*" is intentional for public map tiles.
        # Map tiles must be accessible from any origin for proper rendering.
        # This endpoint only serves static, public image data with no auth.
        return HttpResponse(
            tile_data,
            content_type=content_type,
            headers={
                "Cache-Control": "public, max-age=604800",  # 7 Tage Browser-Cache
                "Access-Control-Allow-Origin": "*",  # nosec: intentional for public tiles
                "X-Tile-Source": "cache",
            },
        )

    # 2. Nicht im Cache - von OSM laden
    subdomain = ["a", "b", "c"][x % 3]
    tile_url = f"https://{subdomain}.tile.openstreetmap.org/{z}/{x}/{y}.png"

    try:
        with httpx.Client(
            timeout=10.0,
            headers={"User-Agent": "Mandari/1.0 (https://mandari.dev; contact@mandari.dev)"},
        ) as client:
            response = client.get(tile_url)

            if response.status_code == 200:
                # Im Cache speichern für zukünftige Requests
                TileCache.store_tile(z, x, y, response.content, "image/png", "openstreetmap")

                # SECURITY NOTE: CORS "*" is intentional for public map tiles.
                return HttpResponse(
                    response.content,
                    content_type="image/png",
                    headers={
                        "Cache-Control": "public, max-age=604800",  # 7 Tage Browser-Cache
                        "Access-Control-Allow-Origin": "*",  # nosec: intentional for public tiles
                        "X-Tile-Source": "osm",
                    },
                )
            else:
                from django.http import HttpResponseNotFound

                return HttpResponseNotFound()
    except Exception as e:
        from django.http import HttpResponseServerError

        logging.getLogger(__name__).exception(f"Tile proxy error: {e}")
        return HttpResponseServerError("Tile proxy error")


@require_GET
@cache_page(60 * 60 * 24)  # Cache für 24 Stunden
def style_proxy(request):
    """
    Proxy für VersaTiles Style JSON.

    Lädt die Style-Konfiguration und ersetzt die Tile-URLs
    mit lokalen Proxy-URLs.
    """
    style_url = "https://tiles.versatiles.org/assets/styles/colorful/style.json"

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(style_url)

            if response.status_code == 200:
                style = response.json()

                # Ersetze externe Tile-URLs mit lokalem Proxy
                if "sources" in style:
                    for source_name, source in style["sources"].items():
                        if "tiles" in source:
                            # Ersetze VersaTiles URL mit lokalem Proxy
                            source["tiles"] = [request.build_absolute_uri("/insight/tiles/{z}/{x}/{y}")]
                        if "url" in source:
                            # Für TileJSON URLs
                            del source["url"]
                            source["tiles"] = [request.build_absolute_uri("/insight/tiles/{z}/{x}/{y}")]

                # Ersetze Sprite und Glyphs URLs
                if "sprite" in style:
                    style["sprite"] = request.build_absolute_uri("/insight/map-assets/sprite")
                if "glyphs" in style:
                    style["glyphs"] = request.build_absolute_uri("/insight/map-assets/glyphs/{fontstack}/{range}.pbf")

                return JsonResponse(style, safe=False)
            else:
                return JsonResponse({"error": "Style not found"}, status=404)
    except Exception as e:
        logging.getLogger(__name__).exception(f"Style proxy error: {e}")
        return JsonResponse({"error": "Style proxy error"}, status=500)


@require_GET
@cache_page(60 * 60 * 24)
def map_sprite(request, filename="sprite"):
    """Proxy für Map Sprites."""
    ext = request.GET.get("ext", "json")
    if filename.endswith(".png"):
        ext = "png"
        filename = filename[:-4]
    elif filename.endswith(".json"):
        ext = "json"
        filename = filename[:-5]

    sprite_url = f"https://tiles.versatiles.org/assets/styles/colorful/{filename}.{ext}"

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(sprite_url)
            if response.status_code == 200:
                from django.http import HttpResponse

                content_type = "application/json" if ext == "json" else "image/png"
                return HttpResponse(response.content, content_type=content_type)
    except Exception:
        pass

    from django.http import HttpResponseNotFound

    return HttpResponseNotFound()


@require_GET
@cache_page(60 * 60 * 24)
def map_glyphs(request, fontstack, range_):
    """Proxy für Map Glyphs (Fonts)."""
    glyphs_url = f"https://tiles.versatiles.org/assets/fonts/{fontstack}/{range_}.pbf"

    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(glyphs_url)
            if response.status_code == 200:
                from django.http import HttpResponse

                return HttpResponse(response.content, content_type="application/x-protobuf")
    except Exception:
        pass

    from django.http import HttpResponseNotFound

    return HttpResponseNotFound()

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import contextlib
import logging
from datetime import timedelta

import httpx
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from ..models import TileCache
from ..services import karten_punkte
from ._helpers import ActiveBodyRequiredMixin, get_active_body

#: Höchste Zoomstufe der Karten (Leaflet maxZoom) und Größengrenze einer Kachel
MAX_TILE_ZOOM = 19
MAX_TILE_BYTES = 1024 * 1024
_TILE_COUNT_CACHE_KEY = "insight:tiles:count"


def _tile_cache_full() -> bool:
    """Obergrenze des Kachel-Caches erreicht? Die Zahl wird nur alle zehn Minuten neu gezählt."""
    from .. import throttle

    limit = throttle.setting("INSIGHT_TILE_CACHE_MAX_TILES")
    if limit <= 0:
        return False
    count = cache.get(_TILE_COUNT_CACHE_KEY)
    if count is None:
        count = TileCache.objects.count()
        cache.set(_TILE_COUNT_CACHE_KEY, count, timeout=600)
    return count >= limit


def _count_stored_tile() -> None:
    """Gespeicherte Kachel mitzählen, damit die Grenze auch zwischen zwei Zählungen hält."""
    # ValueError: noch nicht gezählt – die nächste Prüfung zählt neu
    with contextlib.suppress(ValueError):
        cache.incr(_TILE_COUNT_CACHE_KEY)


# =============================================================================
# Karte
# =============================================================================


class MapView(ActiveBodyRequiredMixin, TemplateView):
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

        # Startausschnitt für das gemeinsame Kartenmodul (frontend/js/vorgangskarte.ts): Rahmen, sonst Zentrum
        context["karten_start"] = {
            "rahmen": context.get("map_bounds"),
            "zentrum": [context["map_center"]["lat"], context["map_center"]["lng"]]
            if "map_center" in context
            else None,
        }

        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title="Karte",
            description="Kommunalpolitik auf der Karte: Vorgänge und Sitzungsorte mit Ortsbezug entdecken.",
            body=body,
        ).to_dict()
        return context


#: Obergrenze ausgelieferter Punkte je Antwort (keine Vollauslieferungen), gemeinsam mit Work (Issue #853)
MAP_MARKERS_MAX_FEATURES = karten_punkte.HOECHSTENS


@require_GET
def map_markers(request):
    """GeoJSON-Endpoint für Karten-Marker.

    Query-Parameter:
        weeks: Anzahl Wochen zurück (Standard: 4, Max: 52)
        all: Wenn "1", alle Vorgänge mit Ortsbezug (kein Zeitfilter)
        bbox: "west,south,east,north" — nur Marker im Kartenausschnitt

    Die Punkte kommen aus derselben Abfrage wie die Karte der Recherche in Work
    (``services.karten_punkte``: Tabelle der Verortungen, ohne gelöschte Vorgänge und im Admin entfernte
    Verortungen, neueste zuerst). Die Antwort ohne Ausschnitt wird je Kommune und Zeitraum gecacht
    (MAP_MARKERS_CACHE_SECONDS, Standard 10 Min); jede Antwort trägt höchstens MAP_MARKERS_MAX_FEATURES Punkte
    (sonst "truncated": true, die Karte lädt dann je Ausschnitt nach).
    """
    body = get_active_body(request)
    if not body:
        return JsonResponse({"type": "FeatureCollection", "features": []})

    show_all = request.GET.get("all") == "1"
    try:
        weeks = min(int(request.GET.get("weeks", "4") or "4"), 52)
    except (TypeError, ValueError):
        weeks = 4
    weeks = max(weeks, 1)
    since = None if show_all else timezone.localdate() - timedelta(weeks=weeks)
    area = karten_punkte.ausschnitt(request.GET.get("bbox"))

    def vorgang_url(paper_id):
        return reverse("insight_core:insight:paper_detail", args=[paper_id])

    if area is not None:
        return JsonResponse(karten_punkte.geojson([body], area=area, since=since, url=vorgang_url))
    cache_key = f"map_markers:{body.id}:{'all' if show_all else weeks}"
    daten = cache.get(cache_key)
    if daten is None:
        daten = karten_punkte.geojson([body], since=since, url=vorgang_url)
        cache.set(cache_key, daten, getattr(settings, "MAP_MARKERS_CACHE_SECONDS", 600))
    return JsonResponse(daten)


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

    Nur gültige Kacheln (Zoom 0–19, x/y innerhalb des Zoomlevels); Abrufe bei OSM sind je IP und
    insgesamt gedrosselt, der Cache hat eine Obergrenze. Kacheln aus dem Cache sind nicht gedrosselt.
    """
    from django.http import HttpResponseNotFound

    from .. import throttle

    if not (0 <= z <= MAX_TILE_ZOOM and 0 <= x < 2**z and 0 <= y < 2**z):
        return HttpResponseNotFound()

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

    # 2. Nicht im Cache - von OSM laden (gedrosselt: je IP und insgesamt, OSM-Nutzungsregeln)
    ip = throttle.client_ip(request)
    if throttle.hit(
        "tile-ip", ip, limit=throttle.setting("INSIGHT_TILE_FETCHES_PER_IP_MINUTE"), window=throttle.MINUTE
    ) or throttle.hit(
        "tile-all", "alle", limit=throttle.setting("INSIGHT_TILE_FETCHES_PER_MINUTE"), window=throttle.MINUTE
    ):
        return HttpResponse(status=429, headers={"Retry-After": "60"})

    subdomain = ["a", "b", "c"][x % 3]
    tile_url = f"https://{subdomain}.tile.openstreetmap.org/{z}/{x}/{y}.png"

    try:
        with httpx.Client(
            timeout=10.0,
            headers={"User-Agent": "Mandari/1.0 (https://mandari.dev; contact@mandari.dev)"},
        ) as client:
            response = client.get(tile_url)

            if response.status_code == 200 and len(response.content) <= MAX_TILE_BYTES:
                # Im Cache speichern für zukünftige Requests (solange die Obergrenze nicht erreicht ist)
                if not _tile_cache_full():
                    TileCache.store_tile(z, x, y, response.content, "image/png", "openstreetmap")
                    _count_stored_tile()

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
            from django.http import HttpResponseNotFound

            return HttpResponseNotFound()
    except Exception as e:
        from django.http import HttpResponseServerError

        logging.getLogger(__name__).exception(f"Tile proxy error: {e}")
        return HttpResponseServerError("Tile proxy error")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from ._helpers import get_active_body

# =============================================================================
# Nachbarschaft (Neighborhood)
# =============================================================================


class NeighborhoodView(TemplateView):
    """Nachbarschaft-Seite: Vorgänge in der Nähe finden."""

    template_name = "pages/neighborhood.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = get_active_body(self.request)
        context["active_body"] = body

        if body:
            # Geodaten für initiale Kartenansicht
            if body.latitude and body.longitude:
                context["map_center"] = {
                    "lat": float(body.latitude),
                    "lng": float(body.longitude),
                }
            if body.bbox_north and body.bbox_south and body.bbox_east and body.bbox_west:
                context["map_bounds"] = {
                    "north": float(body.bbox_north),
                    "south": float(body.bbox_south),
                    "east": float(body.bbox_east),
                    "west": float(body.bbox_west),
                }

            # Stadtteile laden
            import json as json_mod
            import os

            data_path = os.path.join(os.path.dirname(__file__), "data", "stadtteile.json")
            if os.path.exists(data_path):
                with open(data_path, encoding="utf-8") as f:
                    all_districts = json_mod.load(f)
                slug = body.slug or ""
                context["districts"] = all_districts.get(slug, [])

        return context


@require_GET
def neighborhood_autocomplete(request):
    """Proxy zu Photon API für Adress-Autocomplete mit Body-Location-Bias."""
    import httpx as httpx_client

    query = request.GET.get("q", "").strip()
    if not query or len(query) < 2:
        return JsonResponse([], safe=False)

    body = get_active_body(request)

    params = {
        "q": query,
        "limit": 5,
        "lang": "de",
    }

    # Location-Bias auf Body-Zentrum
    if body and body.latitude and body.longitude:
        params["lat"] = str(body.latitude)
        params["lon"] = str(body.longitude)

    try:
        from django.conf import settings as django_settings

        photon_url = getattr(django_settings, "PHOTON_API_URL", "https://photon.komoot.io/api/")

        headers = {"User-Agent": "Mandari/2.0 (https://mandari.de)"}
        resp = httpx_client.get(photon_url, params=params, timeout=5.0, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            coords = feature.get("geometry", {}).get("coordinates", [])
            if len(coords) < 2:
                continue

            lon, lat = coords[0], coords[1]

            # BBox-Filter: Nur Ergebnisse innerhalb Body-Grenzen
            if body and body.bbox_north and body.bbox_south:
                if not (
                    float(body.bbox_south) <= lat <= float(body.bbox_north)
                    and float(body.bbox_west) <= lon <= float(body.bbox_east)
                ):
                    continue

            # Name zusammenbauen
            parts = []
            if props.get("name"):
                parts.append(props["name"])
            if props.get("street"):
                parts.append(props["street"])
            if props.get("housenumber"):
                parts[-1] = parts[-1] + " " + props["housenumber"] if parts else props["housenumber"]
            if props.get("city"):
                parts.append(props["city"])

            name = ", ".join(parts) if parts else props.get("name", query)

            results.append(
                {
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                }
            )

        return JsonResponse(results, safe=False)

    except Exception as e:
        logging.getLogger(__name__).warning(f"Photon autocomplete error: {e}")
        return JsonResponse([], safe=False)


@require_GET
def neighborhood_results(request):
    """HTMX Partial: Papers in der Nähe per Haversine-Query auf JSONB locations."""
    lat_str = request.GET.get("lat")
    lon_str = request.GET.get("lon")
    radius_str = request.GET.get("radius", "500")
    limit_str = request.GET.get("limit", "50")

    if not lat_str or not lon_str:
        return HttpResponse("")

    try:
        lat = float(lat_str)
        lon = float(lon_str)
        radius = int(radius_str)
        result_limit = min(int(limit_str), 50)
    except (ValueError, TypeError):
        return HttpResponse("")

    body = get_active_body(request)
    if not body:
        return HttpResponse("")

    # Haversine-Query auf JSONB locations
    from django.db import connection

    sql = """
        SELECT DISTINCT ON (p.id) p.id, p.name, p.reference, p.paper_type, p.date,
               d.dist AS distance,
               (loc->>'lat')::float AS loc_lat,
               (loc->>'lon')::float AS loc_lon
        FROM oparl_papers p,
             LATERAL jsonb_array_elements(p.locations) AS loc,
             LATERAL (
                 SELECT 6371000 * acos(
                     LEAST(1.0, GREATEST(-1.0,
                         cos(radians(%s)) * cos(radians((loc->>'lat')::float))
                         * cos(radians((loc->>'lon')::float) - radians(%s))
                         + sin(radians(%s)) * sin(radians((loc->>'lat')::float))
                     ))
                 ) AS dist
             ) d
        WHERE p.body_id = %s
          AND p.deleted = FALSE
          AND p.locations IS NOT NULL
          AND jsonb_array_length(p.locations) > 0
          AND d.dist <= %s
        ORDER BY p.id, d.dist
    """

    with connection.cursor() as cursor:
        cursor.execute(sql, [lat, lon, lat, str(body.id), radius])
        rows = cursor.fetchall()

    # Sortiere nach Entfernung und limitiere
    rows.sort(key=lambda r: r[5])
    rows = rows[:result_limit]

    results = []
    for row in rows:
        paper_id, name, reference, paper_type, date, distance, loc_lat, loc_lon = row
        dist_int = int(distance)
        results.append(
            {
                "id": str(paper_id),
                "name": name,
                "reference": reference,
                "paper_type": paper_type,
                "date": date,
                "distance": dist_int,
                "distance_km": f"{dist_int / 1000:.1f}",
                "lat": loc_lat,
                "lon": loc_lon,
                "url": f"/insight/vorgaenge/{paper_id}/",
            }
        )

    return render(
        request,
        "partials/neighborhood_results.html",
        {
            "results": results,
            "lat": lat,
            "lon": lon,
            "radius": radius,
        },
    )

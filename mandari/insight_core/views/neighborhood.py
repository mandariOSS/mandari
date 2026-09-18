# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from ._helpers import ActiveBodyRequiredMixin, get_active_body

# =============================================================================
# Nachbarschaft (Neighborhood)
# =============================================================================


class NeighborhoodView(ActiveBodyRequiredMixin, TemplateView):
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

        from ..seo import get_page_seo

        context["seo"] = get_page_seo(
            self.request,
            title="Nachbarschaft",
            description="Was passiert vor deiner Haustür? Vorgänge und Beschlüsse im Umkreis deiner Straße oder deines Stadtteils.",
            body=body,
        ).to_dict()

        return context


@require_GET
def neighborhood_autocomplete(request):
    """Adress-Autocomplete aus dem eigenen Straßen-/Adressverzeichnis; Photon nur als Fallback."""
    from ..services.neighborhood import autocomplete_places, photon_autocomplete

    query = request.GET.get("q", "").strip()
    if not query or len(query) < 2:
        return JsonResponse([], safe=False)

    body = get_active_body(request)
    if body is None:
        return JsonResponse(photon_autocomplete(None, query), safe=False)
    return JsonResponse(autocomplete_places(body, query), safe=False)


@require_GET
def neighborhood_results(request):
    """HTMX Partial: Vorgänge im Umkreis (Bounding-Box-Index + Haversine, siehe services.paper_locations)."""
    from ..services.paper_locations import nearby_papers

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

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or radius <= 0:
        return HttpResponse("")

    body = get_active_body(request)
    if not body:
        return HttpResponse("")

    results = nearby_papers(body, lat, lon, radius, limit=result_limit)

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

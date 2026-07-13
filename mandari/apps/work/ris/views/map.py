# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.http import JsonResponse
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from ._mixins import RISBodiesMixin


class RISMapView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS map view showing geolocalized papers."""

    template_name = "work/ris/map.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_overview"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        # Kartenzentrum: primäre Kommune
        body = context["body"]

        # Map center and bounds
        context["map_config"] = {
            "center_lat": float(body.latitude) if body.latitude else 51.5,
            "center_lng": float(body.longitude) if body.longitude else 7.5,
            "zoom": 13,
            "bbox": {
                "north": float(body.bbox_north) if body.bbox_north else None,
                "south": float(body.bbox_south) if body.bbox_south else None,
                "east": float(body.bbox_east) if body.bbox_east else None,
                "west": float(body.bbox_west) if body.bbox_west else None,
            }
            if body.bbox_north
            else None,
        }

        return context


class RISMapDataView(RISBodiesMixin, WorkViewMixin, View):
    """API endpoint for map data (GeoJSON)."""

    permission_required = "ris.view"

    def get(self, request, *args, **kwargs):
        bodies = self.get_bodies()
        if not bodies.exists():
            return JsonResponse({"type": "FeatureCollection", "features": []})

        from insight_core.models import OParlPaper

        # Get papers with locations
        papers = OParlPaper.objects.filter(body__in=bodies, locations__isnull=False).exclude(locations=[])[:500]

        features = []
        for paper in papers:
            if not paper.locations:
                continue

            for location in paper.locations:
                if not isinstance(location, dict):
                    continue

                lat = location.get("lat") or location.get("latitude")
                lng = location.get("lng") or location.get("lon") or location.get("longitude")

                if lat and lng:
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

        return JsonResponse({"type": "FeatureCollection", "features": features})

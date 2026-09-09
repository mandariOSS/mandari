# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.http import JsonResponse
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin

from .. import services
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
        context["map_config"] = services.map_config(context["body"])
        return context


class RISMapDataView(RISBodiesMixin, WorkViewMixin, View):
    """API endpoint for map data (GeoJSON)."""

    permission_required = "ris.view"

    def get(self, request, *args, **kwargs):
        bodies = self.get_bodies()
        if not bodies.exists():
            return JsonResponse({"type": "FeatureCollection", "features": []})
        return JsonResponse(services.geojson_features(bodies))

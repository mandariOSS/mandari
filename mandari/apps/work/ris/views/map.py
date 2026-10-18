# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Karte der Recherche in Work (Issue #853).

Die Seite zeichnet verortete Vorgänge der Kommunen der Organisation. Kacheln kommen über den Kachel-Proxy von
mandari (keine Anfrage an fremde Kartendienste), die Punkte lädt die Karte je Ausschnitt und Zeitraum nach
(``RISMapDataView``). Im neuen Erscheinungsbild (Schalter je Organisation, Issue #852) mit eigener Vorlage.
"""

from django.http import JsonResponse
from django.views.generic import TemplateView, View

from apps.common.mixins import WorkViewMixin
from apps.work.rahmen import neues_design

from .. import services
from ._mixins import RISBodiesMixin


class RISMapView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """Karte mit den verorteten Vorgängen, Zeitraum wählbar (Standard 12 Monate)."""

    template_name = "work/ris/map.html"
    permission_required = "ris.view"

    def get_template_names(self) -> list[str]:
        """Im neuen Erscheinungsbild die neue Seite; die bisherige bleibt für Organisationen ohne Schalter."""
        if neues_design(self.organization):
            return ["work/ris/neu/karte.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_overview"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        # Kartenzentrum der primären Kommune, Zeiträume und der gewählte (Standard 12 Monate)
        context.update(services.karte_seite(context["body"], self.request.GET))
        return context


class RISMapDataView(RISBodiesMixin, WorkViewMixin, View):
    """Punkte der Karte als GeoJSON, je Kartenausschnitt (``bbox``) und Zeitraum (``zeitraum``)."""

    permission_required = "ris.view"

    def get(self, request, *args, **kwargs):
        return JsonResponse(services.karte_daten(self.get_bodies(), request.GET))

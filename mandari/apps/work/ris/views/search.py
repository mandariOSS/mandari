# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin
from apps.work.rahmen import neues_design
from insight_core.services.search_filters import SearchQuery

from .. import fragen, selectors, services, suche
from ._mixins import RISBodiesMixin


class RISSearchView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """
    RIS search across all entities.

    Nutzt Elasticsearch (inkl. OCR-Volltexte der Dokumente) mit Filtern für
    Zeitraum, Gremium und Vorlagen-Art. Bei Organisationen mit mehreren
    Kommunen wird über alle body_ids gesucht (terms-Query) — optional per
    Kommunen-Dropdown auf eine Kommune eingeschränkt. Fällt auf Django-ORM
    zurück, wenn Elasticsearch nicht erreichbar ist.

    Im neuen Erscheinungsbild (Schalter je Organisation, Issue #852) zeigt die Seite die Treffer nach Vorgang
    gruppiert, mit Filterleiste wie im Bürgerportal und gewichtet nach dem Bezug der Fraktion (Issue #853,
    ``apps.work.ris.suche``). Live-Suche, Filter und Reiter fragen dieselbe Adresse per HTMX ab und bekommen nur den
    Ergebnisbereich (Seite 1) bzw. die nächsten Treffer (ab Seite 2). Der bisherige Rahmen behält die bisherige Seite.
    """

    template_name = "work/ris/search.html"
    permission_required = "ris.view"

    PAGE_SIZE = services.SEARCH_PAGE_SIZE

    def get_template_names(self) -> list[str]:
        if neues_design(self.organization):
            return ["work/ris/suche.html"]
        return [self.template_name]

    def render_to_response(self, context, **response_kwargs):
        if not neues_design(self.organization):
            return super().render_to_response(context, **response_kwargs)
        vorlage = suche.teilvorlage(self.request.headers, context)
        if vorlage is None:
            response = super().render_to_response(context, **response_kwargs)
        elif not vorlage:
            response = HttpResponse("")
        else:
            response = render(self.request, vorlage, context)
        # Dieselbe Adresse liefert Seite oder Ausschnitt: Caches müssen beides auseinanderhalten
        patch_vary_headers(response, ("HX-Request", "HX-History-Restore-Request"))
        return response

    def _parse_params(self, body_filter: str) -> services.SearchQuery:
        params = self.request.GET
        try:
            page = max(1, int(params.get("seite", "1")))
        except ValueError:
            page = 1
        return services.SearchQuery(
            query=params.get("q", "").strip(),
            date_from=params.get("von", "").strip(),
            date_to=params.get("bis", "").strip(),
            committee=params.get("gremium", "").strip(),
            paper_type=params.get("art", "").strip(),
            result_type=params.get("typ", "").strip(),
            body_filter=body_filter,
            page=page,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_search"
        context["suche_adresse"] = reverse("work:ris_search", kwargs={"org_slug": self.organization.slug})

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context
        if neues_design(self.organization):
            return self._neue_suche(context, bodies)

        # Kommunen-Filter (nur relevant bei mehreren Kommunen)
        search_body_ids, body_filter = services.resolve_search_bodies(
            bodies, self.request.GET.get("kommune", "").strip()
        )
        context["body_filter"] = body_filter

        # Filter-Optionen (immer anzeigen, auch ohne Query)
        context["committees"] = selectors.search_committees(bodies)
        context["paper_types"] = selectors.search_paper_types(bodies)

        query = self._parse_params(body_filter)
        context.update(
            {
                "search_query": query.query,
                "date_from": query.date_from,
                "date_to": query.date_to,
                "committee_filter": query.committee,
                "paper_type_filter": query.paper_type,
                "result_type_filter": query.result_type,
            }
        )
        if query.is_empty:
            return context

        result = services.search(query, search_body_ids)
        context["total_results"] = result.total
        context["search_backend"] = result.backend
        if result.backend == "elasticsearch":
            context["es_results"] = result.es_results
            context["page"] = result.page
            context["pages"] = result.pages
        else:
            context["results"] = result.orm_results
        return context

    def _neue_suche(self, context, bodies):
        """Neue Suche (Issue #853): gruppierte Treffer, Filterleiste, Gewichtung nach dem Bezug der Fraktion."""
        params = SearchQuery.from_get(self.request.GET)
        context |= {"params": params, "query": params.query, "page": params.page}
        if suche.ganze_seite(self.request.headers):
            # Das Fragefeld steht nur auf der ganzen Seite; Ausschnitte (Live-Suche je Tastendruck) brauchen weder
            # Schlüssel noch Kontingent
            context["fragen_verfuegbar"] = fragen.verfuegbar(self.organization)
        if suche.zu_kurz(params):
            context["suche_leer"] = True
            return context
        context |= suche.ergebnis(self.organization, self.membership, params, bodies)
        return context

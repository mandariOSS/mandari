# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors, services
from ._mixins import RISBodiesMixin


class RISSearchView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """
    RIS search across all entities.

    Nutzt Elasticsearch (inkl. OCR-Volltexte der Dokumente) mit Filtern für
    Zeitraum, Gremium und Vorlagen-Art. Bei Organisationen mit mehreren
    Kommunen wird über alle body_ids gesucht (terms-Query) — optional per
    Kommunen-Dropdown auf eine Kommune eingeschränkt. Fällt auf Django-ORM
    zurück, wenn Elasticsearch nicht erreichbar ist.
    """

    template_name = "work/ris/search.html"
    permission_required = "ris.view"

    PAGE_SIZE = services.SEARCH_PAGE_SIZE

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

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

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

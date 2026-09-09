# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import selectors
from ._mixins import RISBodiesMixin


class RISPapersView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS papers list with search and filtering."""

    template_name = "work/ris/papers.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_papers"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        params = self.request.GET
        search = params.get("q", "").strip()
        if search:
            context["search_query"] = search

        paper_type = params.get("type")
        if paper_type:
            context["selected_type"] = paper_type

        year = None
        raw_year = params.get("year")
        if raw_year:
            try:
                year = int(raw_year)
                context["selected_year"] = year
            except ValueError:
                pass

        papers = selectors.papers_queryset(bodies, search=search, paper_type=paper_type or "", year=year)
        context["paper_types"] = selectors.paper_types(bodies)
        context["years"] = selectors.paper_years(bodies)

        paginator = Paginator(papers, 25)
        context["papers"] = paginator.get_page(params.get("page", 1))
        context["paginator"] = paginator
        return context


class RISPaperDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS paper detail view."""

    template_name = "work/ris/paper_detail.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_papers"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        paper = get_object_or_404(selectors.papers_in_bodies(bodies), id=kwargs.get("paper_id"))
        context["paper"] = paper

        files, from_raw_json = selectors.paper_files(paper)
        context["files"] = files
        context["files_from_raw_json"] = from_raw_json

        # Beratungsfolge mit aufgelösten Sitzungen/TOPs
        context["consultations"] = selectors.enriched_consultations(paper)
        return context

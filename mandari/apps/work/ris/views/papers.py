# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.core.paginator import Paginator
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

from .. import neu, selectors, services
from ._mixins import RISBodiesMixin


class RISPapersView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS papers list with search and filtering."""

    template_name = "work/ris/papers.html"
    neue_vorlage = "work/ris/neu/vorgaenge.html"
    permission_required = "ris.view"

    def get(self, request, *args, **kwargs):
        # „Suchen“ mit einer Vorlagennummer und genau einem passenden Treffer öffnet den Vorgang (Issue #1102)
        paper = services.einziger_nummerntreffer(self.get_bodies(), request.GET)
        if paper is not None:
            return HttpResponseRedirect(
                reverse("work:ris_paper_detail", kwargs={"org_slug": self.organization.slug, "paper_id": paper.pk})
            )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_papers"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        filter_ = services.Vorgangsfilter.aus(self.request.GET)
        if filter_.search:
            context["search_query"] = filter_.search
            # Leerzustand bei einer Vorlagennummer: „Die Vorlage … liegt uns noch nicht vor.“ (Issue #1102)
            context["nummer_gesucht"] = filter_.nummer_gesucht
        if filter_.paper_type:
            context["selected_type"] = filter_.paper_type
        if filter_.year is not None:
            context["selected_year"] = filter_.year

        papers = selectors.papers_queryset(
            bodies, search=filter_.search, paper_type=filter_.paper_type, year=filter_.year
        )
        context["paper_types"] = selectors.paper_types(bodies)
        context["years"] = selectors.paper_years(bodies)

        paginator = Paginator(papers, 25)
        context["papers"] = paginator.get_page(self.request.GET.get("page", 1))
        context["paginator"] = paginator
        if self.neu:
            context |= neu.vorgaenge(self.organization, self.membership, context["papers"])
        return context


class RISPaperDetailView(RISBodiesMixin, WorkViewMixin, TemplateView):
    """RIS paper detail view."""

    template_name = "work/ris/paper_detail.html"
    neue_vorlage = "work/ris/neu/vorgang.html"
    permission_required = "ris.view"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_papers"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        paper = selectors.paper_in_bodies(bodies, kwargs["paper_id"])
        if paper is None:
            # Von Session zurückgenommen: Hinweis ohne Inhalt wie im Bürgerportal (410), sonst 404
            return self.zurueckgenommen(
                context, selectors.withdrawn_paper(bodies, kwargs["paper_id"]), "work:ris_papers"
            )
        context["paper"] = paper
        if self.neu:
            # Stand-Satz, Zeitstrahl, Dokumentzeilen und „Für die Fraktion“ (die alte Beratungsfolge braucht es nicht)
            context |= neu.vorgang(self.organization, self.membership, paper)
            return context

        files, from_raw_json = selectors.paper_files(paper)
        context["files"] = files
        context["files_from_raw_json"] = from_raw_json

        # Beratungsfolge mit aufgelösten Sitzungen/TOPs
        context["consultations"] = selectors.enriched_consultations(paper)
        return context

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RIS views for the Work module.

Provides wrapped versions of insight_core views with organization context,
giving users access to their municipality's council information system.
"""

from django.db.models import Q
from django.views.generic import TemplateView

from apps.common.mixins import WorkViewMixin

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

    PAGE_SIZE = 25

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "ris"
        context["active_subnav"] = "ris_search"

        bodies = self.setup_body_context(context)
        if bodies is None:
            return context

        all_bodies = list(bodies)
        all_body_ids = [str(b.id) for b in all_bodies]

        # Kommunen-Filter (nur relevant bei mehreren Kommunen)
        body_filter = self.request.GET.get("kommune", "").strip()
        if body_filter and body_filter in all_body_ids:
            search_body_ids = [body_filter]
        else:
            body_filter = ""
            search_body_ids = all_body_ids
        context["body_filter"] = body_filter

        # Filter-Optionen (immer anzeigen, auch ohne Query)
        from insight_core.models import OParlOrganization, OParlPaper

        context["committees"] = (
            OParlOrganization.objects.filter(body__in=bodies)
            .exclude(name="")
            .order_by("name")
            .values_list("name", flat=True)
        )
        context["paper_types"] = (
            OParlPaper.objects.filter(body__in=bodies)
            .exclude(paper_type__isnull=True)
            .exclude(paper_type="")
            .values_list("paper_type", flat=True)
            .distinct()
            .order_by("paper_type")
        )

        query = self.request.GET.get("q", "").strip()
        date_from = self.request.GET.get("von", "").strip()
        date_to = self.request.GET.get("bis", "").strip()
        committee = self.request.GET.get("gremium", "").strip()
        paper_type = self.request.GET.get("art", "").strip()
        result_type = self.request.GET.get("typ", "").strip()
        try:
            page = max(1, int(self.request.GET.get("seite", "1")))
        except ValueError:
            page = 1

        context.update(
            {
                "search_query": query,
                "date_from": date_from,
                "date_to": date_to,
                "committee_filter": committee,
                "paper_type_filter": paper_type,
                "result_type_filter": result_type,
            }
        )

        has_filters = any([date_from, date_to, committee, paper_type, body_filter])
        if not query and not has_filters:
            return context

        index_names = None
        if result_type in ("papers", "meetings", "persons", "organizations", "files"):
            index_names = [result_type]
        elif committee or paper_type:
            # Diese Filter wirken nur auf Dokument-/Sitzungs-Indexe
            index_names = ["papers", "meetings", "files"] if not paper_type else ["papers"]

        try:
            from insight_core.services.search_service import ElasticsearchService

            service = ElasticsearchService()
            result = service.search_all(
                query=query,
                body_ids=search_body_ids,
                page=page,
                page_size=self.PAGE_SIZE,
                index_names=index_names,
                date_from=date_from or None,
                date_to=date_to or None,
                organization_name=committee or None,
                paper_type=paper_type or None,
            )
            # Django-Templates erlauben keinen Zugriff auf _-Attribute
            for doc in result["results"]:
                doc["formatted"] = doc.get("_formatted", {})
            context["es_results"] = result["results"]
            context["total_results"] = result["total"]
            context["page"] = result["page"]
            context["pages"] = result["pages"]
            context["search_backend"] = "elasticsearch"
            return context
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(f"RIS-Suche: Elasticsearch nicht verfügbar, ORM-Fallback: {e}")

        # ---- Fallback: Django ORM (ohne OCR-Volltext, einfache Filter) ----
        from insight_core.models import OParlMeeting, OParlPerson

        papers = OParlPaper.objects.filter(body_id__in=search_body_ids)
        meetings = OParlMeeting.objects.filter(body_id__in=search_body_ids)
        if query:
            papers = papers.filter(Q(name__icontains=query) | Q(reference__icontains=query))
            meetings = meetings.filter(Q(name__icontains=query) | Q(location_name__icontains=query))
        if date_from:
            papers = papers.filter(date__gte=date_from)
            meetings = meetings.filter(start__gte=date_from)
        if date_to:
            papers = papers.filter(date__lte=date_to)
            meetings = meetings.filter(start__lte=date_to)
        if paper_type:
            papers = papers.filter(paper_type=paper_type)
        if committee:
            meetings = meetings.filter(organizations__name=committee)

        papers = papers.order_by("-date")[:10]
        meetings = meetings.prefetch_related("organizations").order_by("-start")[:10]

        organizations = (
            OParlOrganization.objects.filter(body_id__in=search_body_ids)
            .filter(Q(name__icontains=query) | Q(short_name__icontains=query))
            .order_by("name")[:10]
            if query
            else OParlOrganization.objects.none()
        )
        persons = (
            OParlPerson.objects.filter(body_id__in=search_body_ids)
            .filter(Q(name__icontains=query) | Q(family_name__icontains=query) | Q(given_name__icontains=query))
            .order_by("family_name")[:10]
            if query
            else OParlPerson.objects.none()
        )

        context["results"] = {
            "papers": papers,
            "meetings": meetings,
            "organizations": organizations,
            "persons": persons,
        }
        context["total_results"] = len(papers) + len(meetings) + organizations.count() + persons.count()
        context["search_backend"] = "orm"

        return context

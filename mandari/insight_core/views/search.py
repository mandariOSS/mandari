# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from ..models import (
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from ._helpers import get_active_body, is_all_bodies_mode

# =============================================================================
# Suche
# =============================================================================


class SearchView(TemplateView):
    """Suchseite mit erweiterter Filterung."""

    template_name = "pages/search.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("q", "")
        context["search_type"] = self.request.GET.get("type", "all")
        context["available_types"] = [
            ("all", "Alle"),
            ("paper", "Vorgänge"),
            ("meeting", "Sitzungen"),
            ("person", "Personen"),
            ("organization", "Gremien"),
            ("file", "Dokumente"),
        ]

        from ..seo import get_page_seo

        body = None if is_all_bodies_mode(self.request) else get_active_body(self.request)
        context["seo"] = get_page_seo(
            self.request,
            title="Suche",
            description="Volltextsuche über Vorgänge, Sitzungen, Personen, Gremien und Dokumente der Ratsinformationen.",
            body=body,
        ).to_dict()
        return context


@require_GET
def search_results(request):
    """
    HTMX Endpoint für Suchergebnisse.

    Nutzt Elasticsearch für Volltextsuche.
    """
    query = request.GET.get("q", "").strip()
    search_type = request.GET.get("type", "all")
    page = int(request.GET.get("page", 1))
    is_dropdown = request.GET.get("dropdown") == "1"
    # Im "Alle Kommunen"-Modus wird kommunenübergreifend gesucht (kein Body-Filter)
    body = None if is_all_bodies_mode(request) else get_active_body(request)

    if not query or len(query) < 2:
        return render(
            request,
            "partials/search_results.html",
            {
                "results": [],
                "query": query,
            },
        )

    # Elasticsearch verwenden
    try:
        from ..services.search_service import (
            INDEX_FILES,
            INDEX_MEETINGS,
            INDEX_ORGANIZATIONS,
            INDEX_PAPERS,
            INDEX_PERSONS,
            format_search_result,
            get_search_service,
        )

        search_service = get_search_service()

        # Body-ID für Filter
        body_id = str(body.id) if body else None

        # Index-Auswahl basierend auf Typ
        index_map = {
            "all": None,  # Alle Indexe
            "paper": [INDEX_PAPERS],
            "meeting": [INDEX_MEETINGS],
            "person": [INDEX_PERSONS],
            "organization": [INDEX_ORGANIZATIONS],
            "file": [INDEX_FILES],
        }
        index_names = index_map.get(search_type)

        # Suche ausführen
        page_size = 3 if is_dropdown else 20
        search_result = search_service.search_all(
            query=query,
            body_id=body_id,
            page=page,
            page_size=page_size,
            index_names=index_names,
        )

        # Ergebnisse formatieren
        results = [format_search_result(hit) for hit in search_result["results"]]

        return render(
            request,
            "partials/search_results.html",
            {
                "results": results,
                "query": query,
                "total": search_result["total"],
                "page": search_result["page"],
                "pages": search_result["pages"],
                "search_type": search_type,
                "is_dropdown": is_dropdown,
            },
        )

    except Exception as e:
        # Fallback auf Django-Suche bei Fehler
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(f"Elasticsearch-Suche fehlgeschlagen, Fallback auf Django: {e}")

        results = []

        # Optionaler Body-Filter: body=None bedeutet kommunenübergreifende Suche
        body_filter = {"body": body} if body else {}

        # Vorgänge
        papers = OParlPaper.objects.filter(deleted=False, **body_filter).filter(
            Q(name__icontains=query) | Q(reference__icontains=query)
        )[:10]
        for paper in papers:
            results.append(
                {
                    "type": "paper",
                    "title": paper.name or paper.reference,
                    "subtitle": paper.paper_type,
                    "url": f"/insight/vorgaenge/{paper.id}/",
                }
            )

        # Personen
        persons = OParlPerson.objects.filter(deleted=False, **body_filter).filter(
            Q(name__icontains=query) | Q(family_name__icontains=query) | Q(given_name__icontains=query)
        )[:10]
        for person in persons:
            results.append(
                {
                    "type": "person",
                    "title": person.display_name,
                    "subtitle": "Person",
                    "url": f"/insight/personen/{person.id}/",
                }
            )

        # Gremien
        orgs = OParlOrganization.objects.filter(deleted=False, **body_filter).filter(
            Q(name__icontains=query) | Q(short_name__icontains=query)
        )[:10]
        for org in orgs:
            results.append(
                {
                    "type": "organization",
                    "title": org.name,
                    "subtitle": org.organization_type,
                    "url": f"/insight/gremien/{org.id}/",
                }
            )

        # Sitzungen
        meetings = OParlMeeting.objects.filter(deleted=False, **body_filter).filter(
            Q(name__icontains=query) | Q(location_name__icontains=query)
        )[:10]
        for meeting in meetings:
            results.append(
                {
                    "type": "meeting",
                    "title": meeting.name or "Sitzung",
                    "subtitle": meeting.start.strftime("%d.%m.%Y") if meeting.start else None,
                    "url": f"/insight/termine/{meeting.id}/",
                }
            )

        if is_dropdown:
            results = results[:3]
        return render(
            request,
            "partials/search_results.html",
            {
                "results": results,
                "query": query,
                "total": len(results),
                "is_dropdown": is_dropdown,
            },
        )

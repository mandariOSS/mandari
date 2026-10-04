# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views für Mandari Insight Core.

Server-Side Rendering mit Django Templates + HTMX.
"""

from django.db.models import Q
from django.shortcuts import render
from django.utils.http import urlencode
from django.views.decorators.http import require_GET
from django.views.generic import TemplateView

from ..models import (
    OParlBody,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)
from ._helpers import get_active_body, is_all_bodies_mode, page_number

# =============================================================================
# Suche
# =============================================================================


def _searchable_body_ids() -> list[str]:
    """Kommunenübergreifende Suche: gelistete Kommunen, ohne vorübergehend abgeschaltete (Issue #618)."""
    from ..publication import paused_body_ids

    paused = paused_body_ids()
    return [str(pk) for pk in OParlBody.objects.listed().values_list("id", flat=True) if str(pk) not in paused]


class SearchView(TemplateView):
    """Suchseite (Konzept Insight-Suche, P0.8/P0.9): Ergebnisse serverseitig, ohne JavaScript nutzbar.

    Live-Suche, Filter und Reiter fragen dieselbe Adresse per HTMX ab (``hx-push-url``: Zurück, Teilen und Neuladen
    behalten die Suche). Dann kommt nur der Ergebnisbereich (Seite 1) bzw. die nächsten Einträge (ab Seite 2).
    """

    template_name = "pages/search.html"

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)
        if request.headers.get("HX-Request") == "true" and context.get("params") and context["params"].q:
            template = "partials/search_results_liste.html" if context["page"] > 1 else "partials/search_page.html"
            return render(request, template, context)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from django.utils import timezone

        from ..seo import get_page_seo
        from ..services.search_page import SearchParams, build_context

        params = SearchParams.from_get(self.request.GET)
        body = None if is_all_bodies_mode(self.request) else get_active_body(self.request)
        context["params"] = params
        context["query"] = params.q
        context["seo"] = get_page_seo(
            self.request,
            title=f"„{params.q}“ – Suche" if params.q else "Suche",
            description="Volltextsuche über Vorgänge, Sitzungen, Personen, Gremien und Dokumente der Ratsinformationen.",
            body=body,
        ).to_dict()
        if len(params.q) < 2:
            return context
        try:
            from ..services.search_service import get_search_service

            body_ids = None if body else _searchable_body_ids()
            context.update(build_context(get_search_service(), params, body, body_ids, timezone.localdate()))
        except Exception as e:  # Elasticsearch nicht erreichbar: einfache Datenbanksuche, Seite bleibt nutzbar
            import logging

            logging.getLogger(__name__).warning(f"Suche ohne Elasticsearch, Datenbanksuche: {e}")
            context.update(_fallback_context(params, body))
        context["page"] = context.get("page", 1)
        return context


def _fallback_context(params, body):
    """Datenbanksuche nach Titel und Aktenzeichen, wenn Elasticsearch fehlt (je Typ höchstens zehn)."""
    body_filter = {"body": body} if body else {"body_id__in": _searchable_body_ids()}
    query = params.q
    groups = []
    for paper in OParlPaper.objects.filter(deleted=False, **body_filter).filter(
        Q(name__icontains=query) | Q(reference__icontains=query)
    )[:10]:
        groups.append(
            {
                "kind": "vorgang",
                "url": f"/insight/vorgaenge/{paper.id}/",
                "title": paper.name or paper.reference,
                "context": [c for c in (paper.paper_type, paper.reference) if c],
            }
        )
    for meeting in OParlMeeting.objects.filter(deleted=False, **body_filter).filter(name__icontains=query)[:10]:
        groups.append(
            {
                "kind": "sitzung",
                "url": f"/insight/termine/{meeting.id}/",
                "title": meeting.name or "Sitzung",
                "context": [],
            }
        )
    return {"groups": groups, "page": 1, "has_more": False, "tabs": [], "fallback": True}


@require_GET
def search_results(request):
    """
    HTMX Endpoint für Suchergebnisse.

    Nutzt Elasticsearch für Volltextsuche.
    """
    query = request.GET.get("q", "").strip()
    search_type = request.GET.get("type", "all")
    page = page_number(request)
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

        # Body-ID für Filter; kommunenübergreifend nur gelistete Kommunen ohne vorübergehende Abschaltung
        body_id = str(body.id) if body else None
        body_ids = None if body else _searchable_body_ids()

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

        if not is_dropdown:
            return _grouped_results(
                request, search_service, query, body, body_id, body_ids, index_names, search_type, page
            )

        # Suche ausführen (Kopfzeilen-Vorschau: einzelne Treffer)
        page_size = 3
        search_result = search_service.search_all(
            query=query,
            body_id=body_id,
            body_ids=body_ids,
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
        # Fallback auf Django-Suche bei Fehler. Die Titel sind hier reine Texte aus der Quelle;
        # das Template maskiert sie (nur Titel aus dem Suchdienst sind vorab maskierte SafeStrings).
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(f"Elasticsearch-Suche fehlgeschlagen, Fallback auf Django: {e}")

        results = []

        # Optionaler Body-Filter: body=None bedeutet kommunenübergreifende Suche über gelistete Kommunen
        body_filter = {"body": body} if body else {"body_id__in": _searchable_body_ids()}

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
                {"results": results, "query": query, "total": len(results), "is_dropdown": True},
            )
        # Ganze Seite: dieselbe Liste wie mit Elasticsearch, nur ohne Kontext und Ausschnitt
        kinds = {"paper": "vorgang", "meeting": "sitzung", "person": "person", "organization": "gremium"}
        groups = [
            {
                "kind": kinds.get(r["type"], "dokument"),
                "url": r["url"],
                "title": r["title"],
                "context": [r["subtitle"]] if r.get("subtitle") else [],
            }
            for r in results
        ]
        return render(
            request,
            "partials/search_results.html",
            {"results": groups, "groups": groups, "query": query, "page": 1, "is_dropdown": False},
        )


#: Typen, die in „Alle“ nicht in der Liste stehen, sondern als Link mit Zahl (Konzept Insight-Suche, 3.2)
_OTHER_TYPES = (("persons", "person", "Person", "Personen"), ("organizations", "organization", "Gremium", "Gremien"))


def _grouped_results(request, search_service, query, body, body_id, body_ids, index_names, search_type, page):
    """Ganze Suchseite: Treffer nach Vorgang gruppiert, mit Kontextzeile, Stand-Satz und ehrlicher Zahl."""
    from ..services.search_presentation import count_sentence, present_groups

    grouped = search_service.search_grouped(
        query, body_id=body_id, body_ids=body_ids, page=page, page_size=20, index_names=index_names
    )
    groups = present_groups(grouped["groups"], body.slug if body else "")
    totals = grouped["totals_by_index"]
    other_types = []
    if index_names is None:
        for index, type_key, one, many in _OTHER_TYPES:
            n = int(totals.get(index) or 0)
            if n:
                other_types.append({"type": type_key, "label": f"{n} {one if n == 1 else many}"})
    return render(
        request,
        "partials/search_results.html",
        {
            "results": groups,
            "groups": groups,
            "query": query,
            "page": grouped["page"],
            "pages": grouped["pages"],
            "has_more": grouped["has_more"],
            "next_url": f"/insight/suche/?{urlencode({'q': query, 'type': search_type, 'page': grouped['page'] + 1})}",
            "count_sentence": count_sentence(grouped["counts"]),
            "other_types": other_types,
            "similar_spelling": grouped["similar_spelling"],
            "search_type": search_type,
            "is_dropdown": False,
        },
    )

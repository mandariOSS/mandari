# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Views der OParl-1.1-Aggregations-API (Issue #17).

Endpunkte (alle rein lesend, anonym, JSON, CORS offen):

- ``/oparl/v1/``                                  API-Übersicht
- ``/oparl/v1/system``                            OParl-System-Objekt
- ``/oparl/v1/bodies``                            externe Liste aller Kommunen
- ``/oparl/v1/body/<uuid>``                       einzelne Kommune
- ``/oparl/v1/body/<uuid>/organizations``         externe Listen je Kommune
- ``/oparl/v1/body/<uuid>/people``                (paginiert, filterbar mit
- ``/oparl/v1/body/<uuid>/meetings``              modified_since/-until und
- ``/oparl/v1/body/<uuid>/papers``                created_since/-until)
- ``/oparl/v1/body/<uuid>/locations``             (Vendor-Erweiterung)
- ``/oparl/v1/<typ>/<uuid>``                      Objekt-Endpunkte aller Typen

Performance:
- Listen laden nur die nötigen Relationen per prefetch (kein N+1, Deckel
  ~8 Queries pro Listen-Seite), sortiert nach modified (Coalesce aus
  oparl_modified/updated_at) — stabil für inkrementelle Clients.
- Ungefilterte Listen-Seiten werden 60 s im Django-Cache gehalten.
- Leichtes Rate-Limit je IP (Standard 120 req/min) gegen Scraper-Exzesse.
"""

from urllib.parse import urlencode

from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.db.models.functions import Coalesce

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)

from . import serializers as s
from .utils import (
    OParlBadRequestError,
    api_base,
    body_list_url,
    error_response,
    json_response,
    obj_url,
    oparl_endpoint,
    parse_client_datetime,
    sub_list_url,
    system_url,
)

# Query-Parameter -> ORM-Lookup (auf den Coalesce-Annotationen, damit die
# Filter exakt zu den ausgelieferten created/modified-Werten passen)
FILTER_LOOKUPS = {
    "created_since": "sort_created__gte",
    "created_until": "sort_created__lte",
    "modified_since": "sort_modified__gte",
    "modified_until": "sort_modified__lte",
}


def _prepare_meetings(qs):
    return qs.prefetch_related(
        Prefetch("organizations", queryset=OParlOrganization.objects.only("id")),
        "agenda_items",
        "files",
    )


def _prepare_papers(qs):
    return qs.prefetch_related("files", "consultations")


def _prepare_organizations(qs):
    return qs.prefetch_related(
        Prefetch("memberships", queryset=OParlMembership.objects.only("id", "organization_id")),
    )


def _prepare_persons(qs):
    return qs.prefetch_related("memberships")


def _prepare_bodies(qs):
    return qs.prefetch_related("legislative_terms")


# Externe Listen je Kommune: Segment -> (Modell, Serializer, prepare, RefContext-Factory)
BODY_LISTS = {
    "organizations": (OParlOrganization, s.serialize_organization, _prepare_organizations, s.RefContext.empty),
    "people": (OParlPerson, s.serialize_person, _prepare_persons, s.RefContext.empty),
    "meetings": (OParlMeeting, s.serialize_meeting, _prepare_meetings, s.RefContext.for_meetings),
    "papers": (OParlPaper, s.serialize_paper, _prepare_papers, s.RefContext.for_papers),
    # Vendor-Erweiterung (nicht Teil der Spec-Pflichtlisten, aber trivial und nützlich)
    "locations": (OParlLocation, s.serialize_location, None, s.RefContext.empty),
}

# Objekt-Endpunkte: Typ-Segment -> (Modell, Serializer, prepare, RefContext-Factory)
OBJECT_TYPES = {
    "body": (OParlBody, s.serialize_body, _prepare_bodies, s.RefContext.empty),
    "organization": (OParlOrganization, s.serialize_organization, _prepare_organizations, s.RefContext.empty),
    "person": (OParlPerson, s.serialize_person, _prepare_persons, s.RefContext.empty),
    "membership": (OParlMembership, s.serialize_membership, None, s.RefContext.empty),
    "meeting": (OParlMeeting, s.serialize_meeting, _prepare_meetings, s.RefContext.for_meetings),
    "agendaitem": (OParlAgendaItem, s.serialize_agenda_item, None, s.RefContext.for_agenda_items),
    "paper": (OParlPaper, s.serialize_paper, _prepare_papers, s.RefContext.for_papers),
    "consultation": (OParlConsultation, s.serialize_consultation, None, s.RefContext.for_consultations),
    "file": (
        OParlFile,
        lambda obj, ctx: s.serialize_file(obj, ctx, include_text=True),
        None,
        s.RefContext.empty,
    ),
    "location": (OParlLocation, s.serialize_location, None, s.RefContext.empty),
    "legislativeterm": (OParlLegislativeTerm, s.serialize_legislative_term, None, s.RefContext.empty),
}


# =============================================================================
# Pagination + Filter (OParl-Listen-Envelope)
# =============================================================================


def _page_number(request):
    raw = request.GET.get("page", "1")
    try:
        number = int(raw)
    except ValueError:
        raise OParlBadRequestError(f"Parameter 'page': '{raw}' ist keine gültige Seitennummer.") from None
    if number < 1:
        raise OParlBadRequestError("Parameter 'page': Seitennummern beginnen bei 1.")
    return number


def _paginated_response(request, base_url, queryset, serializer, ctx_factory):
    """Baut den OParl-Listen-Envelope (data/pagination/links) mit Link-Header."""
    filters = {name: request.GET[name] for name in FILTER_LOOKUPS if name in request.GET}
    for name, value in filters.items():
        queryset = queryset.filter(**{FILTER_LOOKUPS[name]: parse_client_datetime(value, name)})
    page_number = _page_number(request)

    # Ungefilterte Listen-Seiten 60 s cachen (inkl. Link-Header)
    cache_seconds = getattr(settings, "OPARL_API_CACHE_SECONDS", 60)
    cache_key = f"oparl_api:list:{base_url}:p{page_number}" if not filters and cache_seconds else None
    if cache_key:
        cached = cache.get(cache_key)
        if cached is not None:
            return json_response(cached["envelope"], headers=cached["headers"])

    page_size = getattr(settings, "OPARL_API_PAGE_SIZE", 100)
    paginator = Paginator(queryset, page_size)
    if page_number > paginator.num_pages:
        return error_response(404, f"Seite {page_number} existiert nicht (letzte Seite: {paginator.num_pages}).")
    page = paginator.page(page_number)
    objects = list(page.object_list)
    ctx = ctx_factory(objects)
    data = [serializer(obj, ctx) for obj in objects]

    def page_link(number):
        params = dict(filters)
        if number > 1:
            params["page"] = number
        return f"{base_url}?{urlencode(params)}" if params else base_url

    links = {"first": page_link(1), "self": page_link(page_number)}
    if page.has_previous():
        links["prev"] = page_link(page_number - 1)
    if page.has_next():
        links["next"] = page_link(page_number + 1)
    links["last"] = page_link(paginator.num_pages)

    envelope = {
        "data": data,
        "pagination": {
            "totalElements": paginator.count,
            "elementsPerPage": page_size,
            "currentPage": page_number,
            "totalPages": paginator.num_pages,
        },
        "links": links,
    }
    headers = {
        "Link": ", ".join(f'<{url}>; rel="{rel}"' for rel, url in links.items() if rel != "self"),
    }
    if cache_key:
        cache.set(cache_key, {"envelope": envelope, "headers": headers}, cache_seconds)
    return json_response(envelope, headers=headers)


def _annotated(queryset):
    """Sortierung/Filter auf Coalesce(oparl_*, eigene Zeitstempel) — konsistent zur Ausgabe."""
    return queryset.annotate(
        sort_created=Coalesce("oparl_created", "created_at"),
        sort_modified=Coalesce("oparl_modified", "updated_at"),
    ).order_by("sort_modified", "id")


# =============================================================================
# Endpunkte
# =============================================================================


@oparl_endpoint
def root_view(request):
    """Kleine JSON-Übersicht über die API (kein OParl-Objekt)."""
    return json_response(
        {
            "name": "mandari — aggregierte OParl-Datenquelle",
            "description": (
                "OParl-1.1-konforme, lesende API über die von mandari gespiegelten "
                "Ratsinformationen aller angebundenen Kommunen. Einstieg über das System-Objekt."
            ),
            "oparlVersion": "https://schema.oparl.org/1.1/",
            "system": system_url(),
            "bodies": body_list_url(),
            "documentation": "https://github.com/mandariOSS/mandari/blob/main/docs/OPARL_API.md",
            "specification": "https://oparl.org/spezifikation/",
            "objectEndpoints": {kind: f"{api_base()}/v1/{kind}/<uuid>" for kind in OBJECT_TYPES},
        }
    )


@oparl_endpoint
def system_view(request):
    return json_response(s.serialize_system())


@oparl_endpoint
def bodies_view(request):
    queryset = _annotated(_prepare_bodies(OParlBody.objects.all()))
    return _paginated_response(request, body_list_url(), queryset, s.serialize_body, s.RefContext.empty)


@oparl_endpoint
def body_sub_list(request, pk, segment):
    spec = BODY_LISTS.get(segment)
    if spec is None:
        return error_response(404, f"Unbekannte Liste '{segment}'. Verfügbar: {', '.join(sorted(BODY_LISTS))}.")
    model, serializer, prepare, ctx_factory = spec
    base_url = sub_list_url(pk, segment)

    # Filter validieren, bevor der Cache greift (400 auch bei Cache-Hit korrekt)
    queryset = model.objects.filter(body_id=pk)
    if prepare:
        queryset = prepare(queryset)
    queryset = _annotated(queryset)

    # Body-Existenz nur auf dem ungecachten Pfad prüfen
    has_filters = any(name in request.GET for name in FILTER_LOOKUPS)
    cache_seconds = getattr(settings, "OPARL_API_CACHE_SECONDS", 60)
    cached_path = not has_filters and cache_seconds and cache.get(f"oparl_api:list:{base_url}:p{_page_number(request)}")
    if not cached_path and not OParlBody.objects.filter(pk=pk).exists():
        return error_response(404, "Kommune (Body) nicht gefunden.")

    return _paginated_response(request, base_url, queryset, serializer, ctx_factory)


@oparl_endpoint
def object_view(request, kind, pk):
    kind = kind.lower()
    entry = OBJECT_TYPES.get(kind)
    if entry is None:
        return error_response(404, f"Unbekannter Objekttyp '{kind}'. Verfügbar: {', '.join(sorted(OBJECT_TYPES))}.")
    model, serializer, prepare, ctx_factory = entry
    queryset = model.objects.all()
    if prepare:
        queryset = prepare(queryset)
    try:
        obj = queryset.get(pk=pk)
    except model.DoesNotExist:
        return error_response(404, f"{obj_url(kind, pk)} nicht gefunden.")
    ctx = ctx_factory([obj])
    return json_response(serializer(obj, ctx))

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Spec-konforme OParl-1.1-API je Session-Mandant (Issue #35).

Nach dem Muster des Aggregators ``oparl_api/`` (Issue #17), aber auf den
Session-Modellen: Jeder aktive SessionTenant erhält unter
``/session/<slug>/api/oparl/`` einen vollwertigen OParl-System-Endpoint – sobald
die Verwaltung die Schnittstelle freigeschaltet hat (Issue #319,
``SessionTenant.oparl_public_since``); vorher antwortet jeder Endpunkt mit 404.

Dieses Modul ist die Schnittstelle: Endpunkte, Sichtbarkeit, Blättern, gelöschte Objekte. Wie ein
Session-Objekt als OParl-Objekt aussieht, legt allein ``hub.ris.mapping.session`` fest (die eine
Abbildung der Session-Objekte auf das kanonische Modell); ``_mapping`` reicht ihr, was sie aus Session
braucht.

- **Auflösbare JSON-Objekt-Endpunkte** für alle Objekttypen (System, Body,
  Organization, Person, Membership, Meeting, AgendaItem, Paper, File,
  Consultation, LegislativeTerm, Location) — IDs zeigen auf JSON, nie auf HTML.
- **Sitzungsort** als eingebettetes Location-Objekt (``Meeting.location``); es gehört zur Sitzung und
  trägt deren Kennung. Die älteren Felder ``mandari:location*`` bleiben vorerst zusätzlich erhalten.
- **Bedingte Anfragen**: jede Antwort trägt einen ``ETag``, ``If-None-Match`` ergibt 304 (oparl_api.utils).
- **Echte Pagination** (``links.next``, konfigurierbare Seitengröße über
  ``OPARL_API_PAGE_SIZE``) und ``modified_since``/``created_since``-Filter
  (Zeitzonen-Pflicht, naive Zeitstempel -> HTTP 400).
- **Tombstones** (OParl 1.1 §2.8): gelöschte oder auf NÖ gestellte Objekte
  bleiben als gekürzte Objekte abrufbar und erscheinen in
  ``modified_since``-Listen (SessionOParlTombstone, oparl_publication.py).
- **NUR öffentliche Daten**: Sichtbarkeit strikt über die Querysets in
  oparl_publication.py (is_public auf Sitzung/TOP/Vorlage/Datei, Anlagen
  nur mit öffentlichem Elternobjekt).
- **Öffentliche Niederschrift** (Issue #318): ``Meeting.resultsProtocol`` verweist auf die beim
  Veröffentlichen erzeugte Datei (nur öffentlicher Teil, nur öffentliche Sitzungen). Personen ohne geschützte Daten —
  verschlüsselte Felder (Telefon, Adresse, Bankdaten) werden nie gelesen, die E-Mail nur mit Einwilligung (Issue #319).
- Anonym, lesend, CORS offen, Rate-Limit wie der Aggregator.
"""

from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db.models import Prefetch

from apps.session import oparl_publication as pub
from apps.session.models import (
    SessionConsultation,
    SessionFile,
    SessionOParlTombstone,
    SessionTenant,
)
from apps.session.services import file_service, meeting_format_service
from apps.session.services.insight_service import oparl_system_url
from hub.ris.mapping.session import SessionMapping, SessionSource
from oparl_api.utils import (
    error_response,
    json_response,
    list_envelope,
    oparl_endpoint,
    page_number,
    page_size,
    parse_client_datetime,
)


class TenantNotFoundError(Exception):
    """Mandant existiert nicht, ist inaktiv oder hat die Schnittstelle nicht freigeschaltet (JSON-404)."""


# Query-Parameter -> ORM-Lookup (auf den Session-Zeitstempeln created_at/updated_at)
FILTER_LOOKUPS = {
    "created_since": "created_at__gte",
    "created_until": "created_at__lte",
    "modified_since": "updated_at__gte",
    "modified_until": "updated_at__lte",
}

# Tombstone-Lookups analog (deleted_at entspricht modified)
TOMBSTONE_LOOKUPS = {
    "created_since": "object_created_at__gte",
    "created_until": "object_created_at__lte",
    "modified_since": "deleted_at__gte",
    "modified_until": "deleted_at__lte",
}


def _results_protocol(meeting):
    """
    Öffentliche Fassung der Niederschrift (Issue #318): nur veröffentlicht, nur öffentliche Sitzung,
    nur die öffentliche Datei an genau dieser Sitzung.
    """
    try:
        protocol = meeting.protocol
    except ObjectDoesNotExist:
        return None
    if protocol is None or protocol.status != "published" or not meeting.is_public:
        return None
    file_obj = protocol.public_file
    if file_obj is None or not file_obj.is_public or file_obj.meeting_id != meeting.pk:
        return None
    return file_obj


#: Was die Abbildung aus Session braucht (die Drehscheibe importiert das Fachmodul nicht)
SOURCE = SessionSource(
    is_published=pub._is_published,
    download_name=file_service.download_name,
    mime_type=file_service.mime_type_for_name,
    # Ohne Prüfungen: Die Ausgabe nennt das Format, nicht die Hinweise für die Sitzungsvorbereitung
    meeting_format=lambda meeting: meeting_format_service.describe(meeting, checks=False),
    results_protocol=_results_protocol,
)


def _mapping(tenant):
    """
    Abbildung für einen Mandanten. Basis aller IDs ist die öffentliche Adresse der Installation
    (``SITE_URL``), nicht der Host der Anfrage: Die IDs sind die kanonischen URIs der Session-Objekte,
    aus denen der RIS-Bestand seine Kennungen ableitet (ADR docs/adr/20260929-kanonisches-modell.md).
    """
    return SessionMapping(tenant, oparl_system_url(tenant), SOURCE)


# =============================================================================
# Querysets je Objekttyp (mit Prefetch gegen N+1)
# =============================================================================


def _public_files_qs():
    return SessionFile.objects.filter(is_public=True).select_related("paper", "meeting", "agenda_item__meeting")


def _prepare_meetings(qs, tenant):
    # tenant__state_profile: Sitzungsformat (Erweiterung der Abbildung) ohne Abfrage je Sitzung
    return qs.select_related("protocol__public_file__meeting", "tenant__state_profile").prefetch_related(
        "joint_organizations",
        Prefetch(
            "agenda_items",
            queryset=pub.visible_agenda_items(tenant).select_related("consultation__paper").order_by("order", "number"),
        ),
        Prefetch("agenda_items__files", queryset=_public_files_qs()),
        "agenda_items__votes__person",
        Prefetch("files", queryset=_public_files_qs()),
    )


def _prepare_papers(qs, tenant):
    return qs.prefetch_related(
        Prefetch("files", queryset=_public_files_qs()),
        Prefetch(
            "consultations",
            queryset=SessionConsultation.objects.select_related("meeting", "agenda_item__meeting"),
        ),
    )


def _prepare_persons(qs, tenant):
    return qs.prefetch_related("memberships")


def _prepare_organizations(qs, tenant):
    return qs.prefetch_related("memberships")


def _prepare_agenda_items(qs, tenant):
    return qs.select_related("meeting", "consultation__paper").prefetch_related(
        Prefetch("files", queryset=_public_files_qs()),
        "votes__person",
    )


def _prepare_consultations(qs, tenant):
    return qs.select_related("paper", "meeting", "agenda_item__meeting")


def _prepare_files(qs, tenant):
    return qs.select_related("paper", "meeting", "agenda_item__meeting")


# Segment -> (Queryset-Funktion, prepare, Abbildung, Objekttyp)
LIST_SPECS = {
    "organizations": (pub.visible_organizations, _prepare_organizations, SessionMapping.organization, "organization"),
    "people": (pub.visible_persons, _prepare_persons, SessionMapping.person, "person"),
    "memberships": (pub.visible_memberships, None, SessionMapping.membership, "membership"),
    "meetings": (pub.visible_meetings, _prepare_meetings, SessionMapping.meeting, "meeting"),
    "agendaitems": (pub.visible_agenda_items, _prepare_agenda_items, SessionMapping.agenda_item, "agendaitem"),
    "papers": (pub.visible_papers, _prepare_papers, SessionMapping.paper, "paper"),
    "consultations": (pub.visible_consultations, _prepare_consultations, SessionMapping.consultation, "consultation"),
    "files": (pub.visible_files, _prepare_files, SessionMapping.file, "file"),
    "legislativeterms": (pub.visible_legislative_terms, None, SessionMapping.legislative_term, "legislativeterm"),
}

# Objekttyp -> (Queryset-Funktion, prepare, Abbildung)
OBJECT_SPECS = {
    "organization": (pub.visible_organizations, _prepare_organizations, SessionMapping.organization),
    "person": (pub.visible_persons, _prepare_persons, SessionMapping.person),
    "membership": (pub.visible_memberships, None, SessionMapping.membership),
    "meeting": (pub.visible_meetings, _prepare_meetings, SessionMapping.meeting),
    "agendaitem": (pub.visible_agenda_items, _prepare_agenda_items, SessionMapping.agenda_item),
    "paper": (pub.visible_papers, _prepare_papers, SessionMapping.paper),
    "consultation": (pub.visible_consultations, _prepare_consultations, SessionMapping.consultation),
    # Der erkannte Text nur am Objekt-Endpunkt, nicht in Listen und Einbettungen
    "file": (pub.visible_files, _prepare_files, SessionMapping.file_with_text),
    "legislativeterm": (pub.visible_legislative_terms, None, SessionMapping.legislative_term),
}


def _get_tenant(tenant_slug):
    # Nicht freigeschaltet (Issue #319): nach außen wie ein unbekannter Mandant, ohne Hinweis auf die Existenz
    tenant = SessionTenant.objects.filter(slug=tenant_slug, is_active=True, oparl_public_since__isnull=False).first()
    if tenant is None:
        raise TenantNotFoundError(tenant_slug)
    return tenant


def session_oparl_endpoint(view):
    """oparl_endpoint + JSON-404 für unbekannte Mandanten."""

    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except TenantNotFoundError:
            return error_response(404, "Mandant nicht gefunden.")

    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return oparl_endpoint(wrapper)


# =============================================================================
# Pagination (echte links.next, Tombstone-Merge bei modified_since)
# =============================================================================


class _MergedEntries:
    """
    Objekte und Tombstones als eine nach ``(modified, Quelle, id)`` sortierte Folge für den Paginator –
    ohne eine der Tabellen ganz zu laden.

    Für eine Seite ``[start:stop]`` liest jede Quelle nur Zeitstempel und Kennung ihrer ersten ``stop``
    Einträge (in derselben Sortierung wie hier), die Seite entsteht durch Zusammenführen; vollständig
    geladen werden nur die Objekte der Seite – mit Select/Prefetch des Querysets.
    """

    def __init__(self, objects, tombstones):
        self.objects = objects.order_by("updated_at", "pk")
        self.tombstones = tombstones.order_by("deleted_at", "pk")

    def count(self):
        return self.objects.count() + self.tombstones.count()

    def __len__(self):
        return self.count()

    def __getitem__(self, index):
        if not isinstance(index, slice):
            raise TypeError("Nur Ausschnitte (Seiten) werden unterstützt.")
        start, stop = index.start or 0, index.stop
        keys = [(stamp, 0, pk) for stamp, pk in self.objects.values_list("updated_at", "pk")[:stop]]
        keys += [(stamp, 1, pk) for stamp, pk in self.tombstones.values_list("deleted_at", "pk")[:stop]]
        keys.sort()
        window = keys[start:stop]
        objects = {obj.pk: obj for obj in self.objects.filter(pk__in=[pk for _, src, pk in window if src == 0])}
        tombs = {t.pk: t for t in self.tombstones.filter(pk__in=[pk for _, src, pk in window if src == 1])}
        return [("obj", objects[pk]) if src == 0 else ("tomb", tombs[pk]) for _, src, pk in window]


def _tombstone(mapping, entry):
    """Gekürztes Objekt für gelöschte/entöffentlichte Einträge (OParl 1.1 §2.8)."""
    return mapping.tombstone(entry.oparl_type, entry.object_id, entry.object_created_at, entry.deleted_at)


def _paginated_response(mapping, request, base_url, queryset, serializer, kind):
    """
    OParl-Listen-Envelope (data/pagination/links) mit Link-Header.

    Tombstones erscheinen NUR in Listen mit ``modified_since``-Filter —
    inkrementelle Clients bekommen Löschungen mit, Voll-Listen bleiben
    frei von Grabsteinen.
    """
    filters = {name: request.GET[name] for name in FILTER_LOOKUPS if name in request.GET}
    parsed = {name: parse_client_datetime(value, name) for name, value in filters.items()}
    for name, value in parsed.items():
        queryset = queryset.filter(**{FILTER_LOOKUPS[name]: value})
    queryset = queryset.order_by("updated_at", "id")
    number = page_number(request)

    if "modified_since" in parsed:
        # Inkrementelle Abfrage: Objekte + Tombstones nach modified sortiert
        tomb_qs = SessionOParlTombstone.objects.filter(tenant=mapping.tenant, oparl_type=kind)
        for name, value in parsed.items():
            tomb_qs = tomb_qs.filter(**{TOMBSTONE_LOOKUPS[name]: value})
        paginator = Paginator(_MergedEntries(queryset, tomb_qs), page_size())
    else:
        paginator = Paginator(queryset, page_size())

    if number > paginator.num_pages:
        return error_response(404, f"Seite {number} existiert nicht (letzte Seite: {paginator.num_pages}).")
    page = paginator.page(number)

    data = []
    for entry in page.object_list:
        if isinstance(entry, tuple):
            entry_type, obj = entry
            data.append(_tombstone(mapping, obj) if entry_type == "tomb" else serializer(mapping, obj))
        else:
            data.append(serializer(mapping, entry))

    envelope, headers = list_envelope(base_url, filters, paginator, page, data)
    return json_response(envelope, headers=headers)


# =============================================================================
# Endpunkte
# =============================================================================


@session_oparl_endpoint
def system_view(request, tenant_slug):
    tenant = _get_tenant(tenant_slug)
    return json_response(_mapping(tenant).system())


@session_oparl_endpoint
def bodies_view(request, tenant_slug):
    mapping = _mapping(_get_tenant(tenant_slug))
    url = mapping.uris.bodies()
    return json_response(
        {
            "data": [mapping.body()],
            "pagination": {
                "totalElements": 1,
                "elementsPerPage": page_size(),
                "currentPage": 1,
                "totalPages": 1,
            },
            "links": {"first": url, "self": url, "last": url},
        }
    )


@session_oparl_endpoint
def body_view(request, tenant_slug):
    tenant = _get_tenant(tenant_slug)
    return json_response(_mapping(tenant).body())


@session_oparl_endpoint
def list_view(request, tenant_slug, segment):
    tenant = _get_tenant(tenant_slug)
    spec = LIST_SPECS.get(segment.lower())
    if spec is None:
        return error_response(404, f"Unbekannte Liste '{segment}'. Verfügbar: {', '.join(sorted(LIST_SPECS))}.")
    qs_fn, prepare, serializer, kind = spec
    mapping = _mapping(tenant)
    queryset = qs_fn(tenant)
    if prepare:
        queryset = prepare(queryset, tenant)
    return _paginated_response(mapping, request, mapping.uris.list(segment.lower()), queryset, serializer, kind)


@session_oparl_endpoint
def object_view(request, tenant_slug, kind, pk):
    tenant = _get_tenant(tenant_slug)
    kind = kind.lower()
    mapping = _mapping(tenant)
    if kind == "location":
        return _location_response(mapping, pk)
    spec = OBJECT_SPECS.get(kind)
    if spec is None:
        kinds = ", ".join(sorted([*OBJECT_SPECS, "location"]))
        return error_response(404, f"Unbekannter Objekttyp '{kind}'. Verfügbar: {kinds}.")
    qs_fn, prepare, serializer = spec
    queryset = qs_fn(tenant)
    if prepare:
        queryset = prepare(queryset, tenant)
    obj = queryset.filter(pk=pk).first()
    if obj is not None:
        return json_response(serializer(mapping, obj))
    # OParl 1.1 §2.8: einmal veröffentlichte, dann gelöschte/entöffentlichte
    # Objekte bleiben als Tombstone (HTTP 200) abrufbar. NÖ-Objekte, die nie
    # veröffentlicht waren, liefern 404 — sie existieren nach außen nicht.
    tombstone = SessionOParlTombstone.objects.filter(tenant=tenant, oparl_type=kind, object_id=pk).first()
    if tombstone is not None:
        return json_response(_tombstone(mapping, tombstone))
    return error_response(404, f"{mapping.uris.obj(kind, pk)} nicht gefunden.")


def _location_response(mapping, pk):
    """
    Sitzungsort unter der Kennung seiner Sitzung (``SessionMapping.location``).

    Nur für öffentliche Sitzungen. Hat die Sitzung keine Ortsangabe (mehr) oder ist sie gelöscht bzw.
    nicht mehr öffentlich, bleibt die Adresse als gekürztes Objekt mit ``"deleted": true`` abrufbar
    (OParl 1.1 §2.8) – ohne Inhalte. Sitzungen, die nie öffentlich waren, ergeben 404.
    """
    meeting = pub.visible_meetings(mapping.tenant).filter(pk=pk).first()
    if meeting is not None:
        gone = mapping.tombstone("location", pk, meeting.created_at, meeting.updated_at)
        return json_response(mapping.location(meeting) or gone)
    tombstone = SessionOParlTombstone.objects.filter(tenant=mapping.tenant, oparl_type="meeting", object_id=pk).first()
    if tombstone is not None:
        return json_response(mapping.tombstone("location", pk, tombstone.object_created_at, tombstone.deleted_at))
    return error_response(404, f"{mapping.uris.obj('location', pk)} nicht gefunden.")


@session_oparl_endpoint
def file_download_view(request, tenant_slug, pk):
    """Anonymer Datei-Abruf — ausschließlich öffentlich sichtbare Anlagen."""
    tenant = _get_tenant(tenant_slug)
    file_obj = pub.visible_files(tenant).filter(pk=pk).first()
    if file_obj is None or not file_obj.file:
        return error_response(404, "Datei nicht gefunden.")
    # Im Browser nur PDF und Rasterbilder; HTML, SVG und alles andere als Download (file_service)
    response = file_service.file_response(
        file_obj.file.open("rb"),
        file_service.download_name(file_obj),
        inline="download" not in request.GET,
    )
    response["Access-Control-Allow-Origin"] = "*"
    return response

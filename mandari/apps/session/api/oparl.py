# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Spec-konforme OParl-1.1-API je Session-Mandant (Issue #35).

Die zweite Ausgabe der offenen Schnittstelle neben dem Aggregator (``hub.api``), auf den
Session-Modellen: Jeder aktive SessionTenant erhält unter
``/session/<slug>/api/oparl/`` einen vollwertigen OParl-System-Endpoint – sobald
die Verwaltung die Schnittstelle freigeschaltet hat (Issue #319,
``SessionTenant.oparl_public_since``); vorher antwortet jeder Endpunkt mit 404.

Dieses Modul legt fest, was eines Mandanten sichtbar ist und unter welchen Adressen: Endpunkte,
Sichtbarkeit, gelöschte Objekte. Wie ein Session-Objekt als OParl-Objekt aussieht, legt allein
``hub.ris.mapping.session`` fest (die eine Abbildung der Session-Objekte auf das kanonische Modell;
``_mapping`` reicht ihr, was sie aus Session braucht). Wie daraus Antworten werden – Zeitfilter,
Blättern, Listen-Hülle, ETag, Fehler –, steht in ``hub.api`` und gilt für beide Ausgaben gleich.

- **Auflösbare JSON-Objekt-Endpunkte** für alle Objekttypen (System, Body,
  Organization, Person, Membership, Meeting, AgendaItem, Paper, File,
  Consultation, LegislativeTerm, Location) — IDs zeigen auf JSON, nie auf HTML.
- **Sitzungsort** als eingebettetes Location-Objekt (``Meeting.location``); es gehört zur Sitzung und
  trägt deren Kennung. Die älteren Felder ``mandari:location*`` bleiben vorerst zusätzlich erhalten.
- **Bedingte Anfragen**: jede Antwort trägt einen ``ETag``, ``If-None-Match`` ergibt 304 (``hub.api.http``).
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
- **Änderungsfeed** (``…/api/oparl/body/changes/``, wenn in der Installation eingeschaltet): dieselbe
  Ausgabe wie beim Aggregator (``hub.api.changes``). Einträge gibt es nur für Objekte, die öffentlich
  sind oder es waren (``_addresses``).
- **Snapshot** (``…/api/oparl/body/snapshot/``): Gesamtstand als NDJSON mit dem Cursor, ab dem der Feed
  fortsetzt (``hub.api.snapshot``) – dieselben Objekte wie die Listen.
- Anonym, lesend, CORS offen, Rate-Limit wie der Aggregator.
"""

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch
from django.http import Http404
from mandari_oparl.ids import canonical_id

from apps.session import oparl_publication as pub
from apps.session.models import (
    SessionConsultation,
    SessionFile,
    SessionOParlTombstone,
    SessionTenant,
)
from apps.session.services import file_service, meeting_format_service
from apps.session.services.insight_service import oparl_system_url
from hub.api import changes, snapshot
from hub.api.http import endpoint, error_response, json_response
from hub.api.serialization import Gone, MergedEntries, TimeFilters, list_response, single_page
from hub.ris.mapping.session import SessionMapping, SessionSource


class TenantNotFoundError(Exception):
    """Mandant existiert nicht, ist inaktiv oder hat die Schnittstelle nicht freigeschaltet (JSON-404)."""


# Zeitfilter -> Vergleich auf den Session-Zeitstempeln created_at/updated_at
FILTER_LOOKUPS = {
    "created_since": "created_at__gte",
    "created_until": "created_at__lte",
    "modified_since": "updated_at__gte",
    "modified_until": "updated_at__lte",
}

# Dieselben Filter auf den Einträgen für Gelöschtes (deleted_at entspricht modified)
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
    return SessionMapping(tenant, oparl_system_url(tenant), SOURCE, changes=changes.enabled())


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
    """``hub.api.http.endpoint`` + JSON-404 für unbekannte Mandanten."""

    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except TenantNotFoundError:
            return error_response(404, "Mandant nicht gefunden.")

    wrapper.__name__ = view.__name__
    wrapper.__doc__ = view.__doc__
    return endpoint(wrapper)


# =============================================================================
# Externe Listen (Gelöschtes nur in der inkrementellen Liste)
# =============================================================================


def _tombstone(mapping, entry):
    """Gekürztes Objekt für gelöschte/entöffentlichte Einträge (OParl 1.1 §2.8)."""
    return mapping.tombstone(entry.oparl_type, entry.object_id, entry.object_created_at, entry.deleted_at)


def _list_response(mapping, request, base_url, queryset, serializer, kind):
    """
    Externe Liste über ``hub.api.serialization`` ausgeben.

    Tombstones erscheinen NUR in Listen mit ``modified_since``-Filter —
    inkrementelle Clients bekommen Löschungen mit, Voll-Listen bleiben
    frei von Grabsteinen.
    """
    filters = TimeFilters.from_request(request)
    entries = filters.apply(queryset, FILTER_LOOKUPS).order_by("updated_at", "id")
    if filters.incremental:
        # Inkrementelle Abfrage: Objekte + Tombstones nach modified sortiert
        tombstones = SessionOParlTombstone.objects.filter(tenant=mapping.tenant, oparl_type=kind)
        entries = MergedEntries(entries, filters.apply(tombstones, TOMBSTONE_LOOKUPS))

    def render(page):
        return [
            _tombstone(mapping, entry.record) if isinstance(entry, Gone) else serializer(mapping, entry)
            for entry in page
        ]

    return list_response(request, base_url, entries, filters, render)


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
    return json_response(single_page(mapping.uris.bodies(), [mapping.body()]))


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
    return _list_response(mapping, request, mapping.uris.list(segment.lower()), queryset, serializer, kind)


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


# =============================================================================
# Änderungsfeed
# =============================================================================

#: So viele Kennungen liest die Suche nach Adressen je Schritt
_ADDRESS_CHUNK = 2000


def _addresses(mapping):
    """
    Adressen der Objekte eines Mandanten für den Änderungsfeed (``hub.api.changes.Feed.addresses``).

    Ereignisse nennen die kanonische Kennung eines Objekts: ``uuid5`` über seine Adresse in dieser
    Schnittstelle (ADR docs/adr/20260929-kanonisches-modell.md). Die Adresse enthält die Kennung des
    Session-Objekts und lässt sich aus der kanonischen Kennung nicht zurückrechnen. Gesucht wird deshalb
    unter den Objekten, die öffentlich sind (``visible_*``) oder es waren (``SessionOParlTombstone``) –
    zuletzt Geändertes zuerst, denn davon handeln die jüngsten Ereignisse.

    Was nie öffentlich war, hat hier keine Adresse und bekommt im Feed keinen Eintrag, auch wenn ein
    Ereignis es fälschlich als öffentlich meldet.

    Eine Anfrage fragt mehrmals (je gelesenem Abschnitt des Journals). Die Suche setzt deshalb dort fort,
    wo sie zuletzt aufgehört hat, und merkt sich die gesehenen Kennungen: Jeder Typ wird je Anfrage
    höchstens einmal durchlaufen, auch wenn Kennungen unauffindbar sind.
    """
    tenant = mapping.tenant
    #: je Typ: gesehene kanonische Kennungen -> Kennung des Session-Objekts, und die offene Suche
    seen = {}
    searches = {}

    def candidates(kind):
        """Kennungen öffentlicher und ehemals öffentlicher Objekte eines Typs, jüngste zuerst."""
        # Der Ort gehört zur Sitzung und trägt deren Kennung
        source = "meeting" if kind == "location" else kind
        spec = OBJECT_SPECS.get(source)
        if spec is None:
            return
        yield from spec[0](tenant).order_by("-updated_at").values_list("pk", flat=True).iterator(_ADDRESS_CHUNK)
        tombstones = SessionOParlTombstone.objects.filter(tenant=tenant, oparl_type=source)
        yield from tombstones.order_by("-deleted_at").values_list("object_id", flat=True).iterator(_ADDRESS_CHUNK)

    def addresses(kind, ids):
        wanted = set(ids)
        if kind == "body":
            url = mapping.uris.body()
            return {canonical_id(url): url} if canonical_id(url) in wanted else {}
        known = seen.setdefault(kind, {})
        found = {key: mapping.uris.obj(kind, known[key]) for key in wanted if key in known}
        wanted -= found.keys()
        if not wanted:
            return found
        search = searches.get(kind)
        if search is None:
            search = searches[kind] = candidates(kind)
        for pk in search:
            key = canonical_id(mapping.uris.obj(kind, pk))
            known.setdefault(key, pk)
            if key in wanted:
                found[key] = mapping.uris.obj(kind, pk)
                wanted.discard(key)
                if not wanted:
                    break
        return found

    return addresses


def _feed(tenant_slug):
    """Feed des Mandanten samt Abbildung; ausgeschaltet gibt es die Adressen nicht."""
    if not changes.enabled():
        raise Http404("Diese Adresse gibt es nicht.")
    mapping = _mapping(_get_tenant(tenant_slug))
    feed = changes.Feed(
        # Kommune im Journal: die kanonische Kennung des Body dieser Schnittstelle
        body_id=canonical_id(mapping.uris.body()),
        url=mapping.uris.changes(),
        snapshot_url=mapping.uris.snapshot(),
        addresses=_addresses(mapping),
    )
    return mapping, feed


@session_oparl_endpoint
def changes_view(request, tenant_slug):
    """Änderungsfeed des Mandanten (``hub.api.changes``)."""
    _, feed = _feed(tenant_slug)
    return changes.changes_response(request, feed)


#: Objektarten des Snapshots: die Listen, deren Objekte alle übrigen einbetten (Mitgliedschaften in
#: Personen, Tagesordnungspunkte und Anlagen in Sitzungen, Beratungen und Anlagen in Vorlagen)
SNAPSHOT_SEGMENTS = ("organizations", "people", "meetings", "papers")


@session_oparl_endpoint
def snapshot_view(request, tenant_slug):
    """Snapshot des Mandanten mit Cursor-Übergabe (``hub.api.snapshot``): dieselben Objekte wie die Listen."""
    mapping, feed = _feed(tenant_slug)
    tenant = mapping.tenant

    def section(segment):
        qs_fn, prepare, serializer, _ = LIST_SPECS[segment]
        queryset = qs_fn(tenant)
        if prepare:
            queryset = prepare(queryset, tenant)
        return snapshot.Section(queryset=queryset, render=lambda page: [serializer(mapping, obj) for obj in page])

    sections = [section(segment) for segment in SNAPSHOT_SEGMENTS]
    return snapshot.snapshot_response(request, snapshot.Snapshot(feed, mapping.body, sections))


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

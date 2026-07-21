# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Spec-konforme OParl-1.1-API je Session-Mandant (Issue #35).

Nach dem Muster des Aggregators ``oparl_api/`` (Issue #17), aber auf den
Session-Modellen: Jeder aktive SessionTenant erhält unter
``/session/<slug>/api/oparl/`` einen vollwertigen OParl-System-Endpoint.

- **Auflösbare JSON-Objekt-Endpunkte** für alle Objekttypen (System, Body,
  Organization, Person, Membership, Meeting, AgendaItem, Paper, File,
  Consultation, LegislativeTerm) — IDs zeigen auf JSON, nie auf HTML.
- **Echte Pagination** (``links.next``, konfigurierbare Seitengröße über
  ``OPARL_API_PAGE_SIZE``) und ``modified_since``/``created_since``-Filter
  (Zeitzonen-Pflicht, naive Zeitstempel -> HTTP 400).
- **Tombstones** (OParl 1.1 §2.8): gelöschte oder auf NÖ gestellte Objekte
  bleiben als gekürzte Objekte abrufbar und erscheinen in
  ``modified_since``-Listen (SessionOParlTombstone, oparl_publication.py).
- **NUR öffentliche Daten**: Sichtbarkeit strikt über die Querysets in
  oparl_publication.py (is_public auf Sitzung/TOP/Vorlage/Datei, Anlagen
  nur mit öffentlichem Elternobjekt). Personen ohne geschützte Daten —
  verschlüsselte Felder (Telefon, Adresse, Bankdaten) werden nie gelesen.
- Anonym, lesend, CORS offen, Rate-Limit wie der Aggregator.
"""

import os
from urllib.parse import urlencode

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.http import FileResponse
from django.urls import reverse

from apps.session import oparl_publication as pub
from apps.session.models import (
    SessionConsultation,
    SessionFile,
    SessionOParlTombstone,
    SessionTenant,
)
from oparl_api.utils import (
    OParlBadRequestError,
    error_response,
    iso,
    iso_date,
    json_response,
    oparl_endpoint,
    parse_client_datetime,
    schema_type,
)


class TenantNotFoundError(Exception):
    """Mandant existiert nicht oder ist inaktiv (JSON-404)."""


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


def _clean(data):
    """Entfernt leere optionale Felder (None, leere Listen/Strings)."""
    return {k: v for k, v in data.items() if v is not None and v != [] and v != ""}


def _as_list(value):
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return value
    return [value]


class TenantApi:
    """URL-Bau je Mandant — alle IDs zeigen auf diese API (JSON), nie auf HTML."""

    def __init__(self, request, tenant):
        self.request = request
        self.tenant = tenant
        path = reverse("session:oparl_system", kwargs={"tenant_slug": tenant.slug})
        self.base = request.build_absolute_uri(path)  # endet mit "/"

    def system_url(self):
        return self.base

    def body_url(self):
        return f"{self.base}body/"

    def bodies_url(self):
        return f"{self.base}bodies/"

    def list_url(self, segment):
        return f"{self.base}{segment}/"

    def obj_url(self, kind, pk):
        return f"{self.base}{kind}/{pk}/"

    def file_download_url(self, pk):
        return f"{self.base}file/{pk}/download/"


def _timestamps(obj):
    return {"created": iso(obj.created_at), "modified": iso(obj.updated_at)}


# =============================================================================
# Serialisierung (NUR öffentliche Felder)
# =============================================================================


def serialize_system(api):
    tenant = api.tenant
    return _clean(
        {
            "id": api.system_url(),
            "type": schema_type("system"),
            "oparlVersion": "https://schema.oparl.org/1.1/",
            "body": api.bodies_url(),
            "name": f"Sitzungsdienst {tenant.name}",
            "contactEmail": tenant.contact_email,
            "contactName": tenant.name,
            "website": tenant.website,
            "vendor": "https://mandari.de",
            "product": "https://github.com/mandariOSS/mandari",
            **_timestamps(tenant),
        }
    )


def serialize_body(api, tenant=None):
    tenant = tenant or api.tenant
    terms = [serialize_legislative_term(api, term) for term in tenant.legislative_terms.all()]
    return _clean(
        {
            "id": api.body_url(),
            "type": schema_type("body"),
            "system": api.system_url(),
            "name": tenant.name,
            "shortName": tenant.short_name,
            "website": tenant.website,
            "contactEmail": tenant.contact_email,
            "classification": "Kommune",
            "organization": api.list_url("organizations"),
            "person": api.list_url("people"),
            "meeting": api.list_url("meetings"),
            "paper": api.list_url("papers"),
            "membership": api.list_url("memberships"),
            "agendaItem": api.list_url("agendaitems"),
            "consultation": api.list_url("consultations"),
            "file": api.list_url("files"),
            "legislativeTermList": api.list_url("legislativeterms"),
            "legislativeTerm": terms,
            **_timestamps(tenant),
        }
    )


def serialize_organization(api, org):
    return _clean(
        {
            "id": api.obj_url("organization", org.id),
            "type": schema_type("organization"),
            "body": api.body_url(),
            "name": org.name,
            "shortName": org.short_name,
            "organizationType": org.organization_type,
            "classification": org.get_organization_type_display(),
            "startDate": iso_date(org.start_date),
            "endDate": iso_date(org.end_date),
            "subOrganizationOf": api.obj_url("organization", org.parent_id) if org.parent_id else None,
            "membership": [api.obj_url("membership", m.id) for m in org.memberships.all()],
            **_timestamps(org),
        }
    )


def serialize_person(api, person):
    """
    Person OHNE geschützte Daten: Verschlüsselte Felder (Telefon, Adresse,
    Bankdaten) werden hier bewusst NIE gelesen. Die E-Mail ist laut
    Datenmodell (SessionPerson) als öffentliches OParl-Feld vorgesehen.
    """
    return _clean(
        {
            "id": api.obj_url("person", person.id),
            "type": schema_type("person"),
            "body": api.body_url(),
            "name": person.display_name,
            "familyName": person.family_name,
            "givenName": person.given_name,
            "formOfAddress": person.form_of_address,
            "title": _as_list(person.title),
            "email": _as_list(person.email),
            # OParl 1.1 bettet Memberships in Person ein
            "membership": [serialize_membership(api, m) for m in person.memberships.all()],
            **_timestamps(person),
        }
    )


def serialize_membership(api, membership):
    return _clean(
        {
            "id": api.obj_url("membership", membership.id),
            "type": schema_type("membership"),
            "person": api.obj_url("person", membership.person_id),
            "organization": api.obj_url("organization", membership.organization_id),
            "role": membership.get_role_display(),
            "votingRight": membership.has_voting_rights,
            "startDate": iso_date(membership.start_date),
            "endDate": iso_date(membership.end_date),
            **_timestamps(membership),
        }
    )


def _visible_consultation(item):
    """Öffentlich sichtbare Beratungsstation eines TOP (oder None)."""
    try:
        consultation = item.consultation
    except SessionConsultation.DoesNotExist:
        return None
    if consultation is None or not consultation.paper.is_public:
        return None
    return consultation


def serialize_meeting(api, meeting):
    files = [f for f in meeting.files.all() if f.is_public]
    items = [i for i in meeting.agenda_items.all() if i.is_public]
    items.sort(key=lambda i: (i.order, i.number))
    return _clean(
        {
            "id": api.obj_url("meeting", meeting.id),
            "type": schema_type("meeting"),
            "name": meeting.name,
            "meetingState": meeting.get_meeting_state_display(),
            "cancelled": meeting.cancelled,
            "start": iso(meeting.start),
            "end": iso(meeting.end),
            "organization": [api.obj_url("organization", meeting.organization_id)],
            "auxiliaryFile": [serialize_file(api, f) for f in files],
            # OParl 1.1 bettet Tagesordnungspunkte in Meeting ein (nur Ö-Teil!)
            "agendaItem": [serialize_agenda_item(api, item) for item in items],
            **_timestamps(meeting),
            "mandari:locationName": meeting.location or None,
            "mandari:locationRoom": meeting.room or None,
            "mandari:locationAddress": ", ".join(
                part for part in (meeting.street_address, f"{meeting.postal_code} {meeting.locality}".strip()) if part
            )
            or None,
        }
    )


def serialize_agenda_item(api, item):
    consultation = _visible_consultation(item)
    files = [f for f in item.files.all() if f.is_public]
    return _clean(
        {
            "id": api.obj_url("agendaitem", item.id),
            "type": schema_type("agendaitem"),
            "meeting": api.obj_url("meeting", item.meeting_id),
            "number": item.number,
            "order": item.order,
            "name": item.name,
            "public": True,  # NÖ-TOPs werden nie ausgeliefert
            "consultation": api.obj_url("consultation", consultation.id) if consultation else None,
            "result": item.get_vote_result_display() if item.vote_result != "pending" else None,
            # Nur der ÖFFENTLICHE Beschlusstext — resolution_text_encrypted nie
            "resolutionText": item.resolution_text or None,
            "auxiliaryFile": [serialize_file(api, f) for f in files],
            **_timestamps(item),
            "mandari:resolutionNumber": item.resolution_number or None,
        }
    )


def serialize_paper(api, paper):
    files = sorted((f for f in paper.files.all() if f.is_public), key=lambda f: f.created_at)
    main_file = files[0] if files else None
    auxiliary = files[1:]
    consultations = [c for c in paper.consultations.all()]
    consultations.sort(key=lambda c: (c.order, c.created_at))
    return _clean(
        {
            "id": api.obj_url("paper", paper.id),
            "type": schema_type("paper"),
            "body": api.body_url(),
            "name": paper.name,
            "reference": paper.reference,
            "date": iso_date(paper.date),
            "paperType": paper.get_paper_type_display(),
            "mainFile": serialize_file(api, main_file) if main_file else None,
            "auxiliaryFile": [serialize_file(api, f) for f in auxiliary],
            # OParl 1.1 bettet Consultations in Paper ein
            "consultation": [serialize_consultation(api, c) for c in consultations],
            "originatorPerson": [api.obj_url("person", paper.originator_person_id)]
            if paper.originator_person_id
            else None,
            "originatorOrganization": [api.obj_url("organization", paper.originator_organization_id)]
            if paper.originator_organization_id
            else None,
            "underDirectionOf": [api.obj_url("organization", paper.main_organization_id)]
            if paper.main_organization_id
            else None,
            **_timestamps(paper),
        }
    )


def serialize_consultation(api, consultation):
    meeting = consultation.meeting
    item = consultation.agenda_item
    # Ö/NÖ: Referenzen auf NÖ-Sitzungen/-TOPs werden ausgelassen
    meeting_visible = meeting is not None and meeting.is_public
    item_visible = item is not None and item.is_public and item.meeting.is_public
    return _clean(
        {
            "id": api.obj_url("consultation", consultation.id),
            "type": schema_type("consultation"),
            "paper": api.obj_url("paper", consultation.paper_id),
            "organization": [api.obj_url("organization", consultation.organization_id)],
            "meeting": api.obj_url("meeting", meeting.id) if meeting_visible else None,
            "agendaItem": api.obj_url("agendaitem", item.id) if item_visible else None,
            "authoritative": consultation.authoritative,
            "role": consultation.get_role_display(),
            **_timestamps(consultation),
        }
    )


def serialize_file(api, file_obj, include_text=False):
    download = api.file_download_url(file_obj.id)
    refs = {}
    if file_obj.paper_id and file_obj.paper.is_public:
        refs["paper"] = [api.obj_url("paper", file_obj.paper_id)]
    if file_obj.meeting_id and file_obj.meeting.is_public:
        refs["meeting"] = [api.obj_url("meeting", file_obj.meeting_id)]
    if file_obj.agenda_item_id and file_obj.agenda_item.is_public and file_obj.agenda_item.meeting.is_public:
        refs["agendaItem"] = [api.obj_url("agendaitem", file_obj.agenda_item_id)]
    return _clean(
        {
            "id": api.obj_url("file", file_obj.id),
            "type": schema_type("file"),
            "name": file_obj.name,
            "fileName": os.path.basename(file_obj.file.name) if file_obj.file else None,
            "mimeType": file_obj.mime_type,
            "size": file_obj.size,
            "date": iso(file_obj.created_at),
            "accessUrl": download,
            "downloadUrl": f"{download}?download=1",
            "text": (file_obj.text_content or None) if include_text else None,
            **refs,
            **_timestamps(file_obj),
            "mandari:version": file_obj.version,
        }
    )


def serialize_legislative_term(api, term):
    return _clean(
        {
            "id": api.obj_url("legislativeterm", term.id),
            "type": schema_type("legislativeterm"),
            "body": api.body_url(),
            "name": term.name,
            "startDate": iso_date(term.start_date),
            "endDate": iso_date(term.end_date),
            **_timestamps(term),
        }
    )


def serialize_tombstone(api, tombstone):
    """Gekürztes Objekt für gelöschte/entöffentlichte Einträge (OParl 1.1 §2.8)."""
    return {
        "id": api.obj_url(tombstone.oparl_type, tombstone.object_id),
        "type": schema_type(tombstone.oparl_type),
        "created": iso(tombstone.object_created_at),
        "modified": iso(tombstone.deleted_at),
        "deleted": True,
    }


# =============================================================================
# Querysets je Objekttyp (mit Prefetch gegen N+1)
# =============================================================================


def _public_files_qs():
    return SessionFile.objects.filter(is_public=True).select_related("paper", "meeting", "agenda_item__meeting")


def _prepare_meetings(qs, tenant):
    return qs.prefetch_related(
        Prefetch(
            "agenda_items",
            queryset=pub.visible_agenda_items(tenant).select_related("consultation__paper").order_by("order", "number"),
        ),
        Prefetch("agenda_items__files", queryset=_public_files_qs()),
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
    )


def _prepare_consultations(qs, tenant):
    return qs.select_related("paper", "meeting", "agenda_item__meeting")


def _prepare_files(qs, tenant):
    return qs.select_related("paper", "meeting", "agenda_item__meeting")


# Segment -> (Queryset-Funktion, prepare, Serializer, Objekttyp)
LIST_SPECS = {
    "organizations": (pub.visible_organizations, _prepare_organizations, serialize_organization, "organization"),
    "people": (pub.visible_persons, _prepare_persons, serialize_person, "person"),
    "memberships": (pub.visible_memberships, None, serialize_membership, "membership"),
    "meetings": (pub.visible_meetings, _prepare_meetings, serialize_meeting, "meeting"),
    "agendaitems": (pub.visible_agenda_items, _prepare_agenda_items, serialize_agenda_item, "agendaitem"),
    "papers": (pub.visible_papers, _prepare_papers, serialize_paper, "paper"),
    "consultations": (pub.visible_consultations, _prepare_consultations, serialize_consultation, "consultation"),
    "files": (pub.visible_files, _prepare_files, serialize_file, "file"),
    "legislativeterms": (pub.visible_legislative_terms, None, serialize_legislative_term, "legislativeterm"),
}

# Objekttyp -> (Queryset-Funktion, prepare, Serializer)
OBJECT_SPECS = {
    "organization": (pub.visible_organizations, _prepare_organizations, serialize_organization),
    "person": (pub.visible_persons, _prepare_persons, serialize_person),
    "membership": (pub.visible_memberships, None, serialize_membership),
    "meeting": (pub.visible_meetings, _prepare_meetings, serialize_meeting),
    "agendaitem": (pub.visible_agenda_items, _prepare_agenda_items, serialize_agenda_item),
    "paper": (pub.visible_papers, _prepare_papers, serialize_paper),
    "consultation": (pub.visible_consultations, _prepare_consultations, serialize_consultation),
    "file": (pub.visible_files, _prepare_files, lambda api, obj: serialize_file(api, obj, include_text=True)),
    "legislativeterm": (pub.visible_legislative_terms, None, serialize_legislative_term),
}


def _get_tenant(tenant_slug):
    tenant = SessionTenant.objects.filter(slug=tenant_slug, is_active=True).first()
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


def _page_number(request):
    raw = request.GET.get("page", "1")
    try:
        number = int(raw)
    except ValueError:
        raise OParlBadRequestError(f"Parameter 'page': '{raw}' ist keine gültige Seitennummer.") from None
    if number < 1:
        raise OParlBadRequestError("Parameter 'page': Seitennummern beginnen bei 1.")
    return number


def _paginated_response(api, request, base_url, queryset, serializer, kind):
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
    page_number = _page_number(request)
    page_size = getattr(settings, "OPARL_API_PAGE_SIZE", 100)

    if "modified_since" in parsed:
        # Inkrementelle Abfrage: Objekte + Tombstones nach modified sortiert
        tomb_qs = SessionOParlTombstone.objects.filter(tenant=api.tenant, oparl_type=kind)
        for name, value in parsed.items():
            tomb_qs = tomb_qs.filter(**{TOMBSTONE_LOOKUPS[name]: value})
        entries = [(obj.updated_at, "obj", obj) for obj in queryset]
        entries += [(t.deleted_at, "tomb", t) for t in tomb_qs]
        entries.sort(key=lambda e: (e[0], str(e[2].pk)))
        paginator = Paginator(entries, page_size)
    else:
        paginator = Paginator(queryset, page_size)

    if page_number > paginator.num_pages:
        return error_response(404, f"Seite {page_number} existiert nicht (letzte Seite: {paginator.num_pages}).")
    page = paginator.page(page_number)

    data = []
    for entry in page.object_list:
        if isinstance(entry, tuple):
            _, entry_type, obj = entry
            data.append(serialize_tombstone(api, obj) if entry_type == "tomb" else serializer(api, obj))
        else:
            data.append(serializer(api, entry))

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
    headers = {"Link": ", ".join(f'<{url}>; rel="{rel}"' for rel, url in links.items() if rel != "self")}
    return json_response(envelope, headers=headers)


# =============================================================================
# Endpunkte
# =============================================================================


@session_oparl_endpoint
def system_view(request, tenant_slug):
    tenant = _get_tenant(tenant_slug)
    return json_response(serialize_system(TenantApi(request, tenant)))


@session_oparl_endpoint
def bodies_view(request, tenant_slug):
    tenant = _get_tenant(tenant_slug)
    api = TenantApi(request, tenant)
    body = serialize_body(api)
    return json_response(
        {
            "data": [body],
            "pagination": {
                "totalElements": 1,
                "elementsPerPage": getattr(settings, "OPARL_API_PAGE_SIZE", 100),
                "currentPage": 1,
                "totalPages": 1,
            },
            "links": {"first": api.bodies_url(), "self": api.bodies_url(), "last": api.bodies_url()},
        }
    )


@session_oparl_endpoint
def body_view(request, tenant_slug):
    tenant = _get_tenant(tenant_slug)
    return json_response(serialize_body(TenantApi(request, tenant)))


@session_oparl_endpoint
def list_view(request, tenant_slug, segment):
    tenant = _get_tenant(tenant_slug)
    spec = LIST_SPECS.get(segment.lower())
    if spec is None:
        return error_response(404, f"Unbekannte Liste '{segment}'. Verfügbar: {', '.join(sorted(LIST_SPECS))}.")
    qs_fn, prepare, serializer, kind = spec
    api = TenantApi(request, tenant)
    queryset = qs_fn(tenant)
    if prepare:
        queryset = prepare(queryset, tenant)
    return _paginated_response(api, request, api.list_url(segment.lower()), queryset, serializer, kind)


@session_oparl_endpoint
def object_view(request, tenant_slug, kind, pk):
    tenant = _get_tenant(tenant_slug)
    kind = kind.lower()
    spec = OBJECT_SPECS.get(kind)
    if spec is None:
        return error_response(404, f"Unbekannter Objekttyp '{kind}'. Verfügbar: {', '.join(sorted(OBJECT_SPECS))}.")
    qs_fn, prepare, serializer = spec
    api = TenantApi(request, tenant)
    queryset = qs_fn(tenant)
    if prepare:
        queryset = prepare(queryset, tenant)
    obj = queryset.filter(pk=pk).first()
    if obj is not None:
        return json_response(serializer(api, obj))
    # OParl 1.1 §2.8: einmal veröffentlichte, dann gelöschte/entöffentlichte
    # Objekte bleiben als Tombstone (HTTP 200) abrufbar. NÖ-Objekte, die nie
    # veröffentlicht waren, liefern 404 — sie existieren nach außen nicht.
    tombstone = SessionOParlTombstone.objects.filter(tenant=tenant, oparl_type=kind, object_id=pk).first()
    if tombstone is not None:
        return json_response(serialize_tombstone(api, tombstone))
    return error_response(404, f"{api.obj_url(kind, pk)} nicht gefunden.")


@session_oparl_endpoint
def file_download_view(request, tenant_slug, pk):
    """Anonymer Datei-Abruf — ausschließlich öffentlich sichtbare Anlagen."""
    tenant = _get_tenant(tenant_slug)
    file_obj = pub.visible_files(tenant).filter(pk=pk).first()
    if file_obj is None or not file_obj.file:
        return error_response(404, "Datei nicht gefunden.")
    response = FileResponse(
        file_obj.file.open("rb"),
        as_attachment="download" in request.GET,
        filename=os.path.basename(file_obj.file.name),
        content_type=file_obj.mime_type or "application/octet-stream",
    )
    response["Access-Control-Allow-Origin"] = "*"
    return response

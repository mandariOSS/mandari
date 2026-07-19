# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Serialisierung der insight_core-Modelle in OParl-1.1-Objekte.

Grundsätze:
- ``id`` und ALLE Objekt-Referenzen zeigen auf unsere eigene API — niemals
  auf die Original-URLs der Quellsysteme. Referenzen werden bevorzugt über
  die vorhandenen Fremdschlüssel aufgelöst; wo nur external_ids existieren
  (Consultation → Meeting/AgendaItem, Meeting → Location, AgendaItem →
  Consultation), löst ein Batch-Resolver (``RefContext``) ohne N+1 auf.
- ``created``/``modified`` kommen aus oparl_created/oparl_modified, mit
  Fallback auf unsere eigenen Zeitstempel (immer gesetzt).
- Die Original-URL im Quellsystem bleibt als Vendor-Attribut
  ``mandari:originalId`` erhalten (spec-konform mit Namespace-Präfix).
- ``web`` verweist auf die passende Insight-Detailseite.
- Datei-URLs (accessUrl/downloadUrl) zeigen auf unseren File-Proxy —
  Clients laden Dokumente über mandari, nicht direkt vom Quellserver.

Tombstones (OParl 1.1 §2.8, "Umgang mit gelöschten Objekten"):
- In der Quelle gelöschte Objekte werden bei uns nur markiert
  (``deleted=True``) und als gekürzte Tombstones ausgeliefert:
  ``{"id", "type", "created", "modified", "deleted": true}`` — ``modified``
  ist der Löschzeitpunkt. Eingebettete Referenzen auf gelöschte Objekte
  werden ausgelassen (Prefetch-/Resolver-Filter in views.py/RefContext).

Bekannte Einschränkungen (v1, siehe docs/OPARL_API.md):
- Reine Personen-/Organisations-Querverweise ohne Fremdschlüssel im Modell
  (participant, originatorPerson, subOrganizationOf, …) werden ausgelassen,
  statt Original-URLs durchzureichen.
"""

from insight_core.models import (
    OParlAgendaItem,
    OParlConsultation,
    OParlLocation,
    OParlMeeting,
)

from .utils import body_list_url, iso, iso_date, obj_url, schema_type, site_url, sub_list_url, system_url


def _clean(data):
    """Entfernt leere optionale Felder (None, leere Listen/Strings)."""
    return {k: v for k, v in data.items() if v is not None and v != [] and v != ""}


def _as_list(value):
    """Hüllt Skalar-Werte in eine Liste (OParl erwartet z. B. email/phone als Array)."""
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return value
    return [value]


def _timestamps(obj):
    """created/modified aus OParl-Zeitstempeln mit Fallback auf unsere eigenen."""
    return {
        "created": iso(obj.oparl_created or obj.created_at),
        "modified": iso(obj.oparl_modified or obj.updated_at),
    }


def _location_ext(raw_json):
    """External-ID der Location aus raw_json (String-Referenz oder eingebettetes Objekt)."""
    location = (raw_json or {}).get("location")
    if isinstance(location, dict):
        return location.get("id")
    if isinstance(location, str):
        return location
    return None


def _file_ext(ref):
    """External-ID einer Datei-Referenz aus raw_json (String oder eingebettetes Objekt)."""
    if isinstance(ref, dict):
        return ref.get("id")
    if isinstance(ref, str):
        return ref
    return None


# =============================================================================
# Batch-Resolver: external_id -> interne Objekte (verhindert N+1-Queries)
# =============================================================================


class RefContext:
    """Löst external_id-Referenzen einer ganzen Ergebnisseite in EINER Query je Typ auf."""

    def __init__(self):
        # external_id -> UUID
        self.meeting_by_ext = {}
        self.agenda_item_by_ext = {}
        # external_id -> OParlLocation-Instanz (wird eingebettet)
        self.location_by_ext = {}
        # external_id des TOP -> [Consultation-UUIDs]
        self.consultations_by_agenda_ext = {}

    @classmethod
    def empty(cls, objects):
        return cls()

    @classmethod
    def for_meetings(cls, meetings):
        """Für Meetings: Sitzungsort + Consultation-Referenzen der eingebetteten TOPs."""
        ctx = cls()
        location_exts = set()
        agenda_exts = set()
        for meeting in meetings:
            ext = _location_ext(meeting.raw_json)
            if ext:
                location_exts.add(ext)
            for item in meeting.agenda_items.all():
                agenda_exts.add(item.external_id)
        if location_exts:
            ctx.location_by_ext = {
                loc.external_id: loc
                for loc in OParlLocation.objects.filter(external_id__in=location_exts, deleted=False)
            }
        ctx._load_consultations(agenda_exts)
        return ctx

    @classmethod
    def for_agenda_items(cls, items):
        ctx = cls()
        ctx._load_consultations({item.external_id for item in items})
        return ctx

    @classmethod
    def for_consultations(cls, consultations):
        """Für Consultations: meeting/agendaItem sind nur als external_id gespeichert."""
        ctx = cls()
        meeting_exts = {c.meeting_external_id for c in consultations if c.meeting_external_id}
        agenda_exts = {c.agenda_item_external_id for c in consultations if c.agenda_item_external_id}
        if meeting_exts:
            ctx.meeting_by_ext = dict(
                OParlMeeting.objects.filter(external_id__in=meeting_exts, deleted=False).values_list(
                    "external_id", "id"
                )
            )
        if agenda_exts:
            ctx.agenda_item_by_ext = dict(
                OParlAgendaItem.objects.filter(external_id__in=agenda_exts, deleted=False).values_list(
                    "external_id", "id"
                )
            )
        return ctx

    @classmethod
    def for_papers(cls, papers):
        """Für Papers: eingebettete Consultations brauchen Meeting-/TOP-Auflösung."""
        consultations = [c for paper in papers for c in paper.consultations.all()]
        return cls.for_consultations(consultations)

    def _load_consultations(self, agenda_exts):
        if not agenda_exts:
            return
        pairs = OParlConsultation.objects.filter(agenda_item_external_id__in=agenda_exts, deleted=False).values_list(
            "agenda_item_external_id", "id"
        )
        for agenda_ext, consultation_id in pairs:
            self.consultations_by_agenda_ext.setdefault(agenda_ext, []).append(consultation_id)


# =============================================================================
# Serializer je Objekttyp
# =============================================================================


def serialize_tombstone(obj, kind):
    """Gekürztes Objekt für in der Quelle gelöschte Einträge (OParl 1.1 §2.8).

    Pflichtfelder: id, type, created, modified, deleted — alle weiteren
    Attribute entfallen. ``modified`` entspricht dem Löschzeitpunkt.
    """
    return {
        "id": obj_url(kind, obj.id),
        "type": schema_type(kind),
        "created": iso(obj.oparl_created or obj.created_at),
        "modified": iso(obj.oparl_modified or obj.deleted_at or obj.updated_at),
        "deleted": True,
    }


def serialize_system():
    return _clean(
        {
            "id": system_url(),
            "type": schema_type("system"),
            "oparlVersion": "https://schema.oparl.org/1.1/",
            "body": body_list_url(),
            "name": "mandari — aggregierte Ratsinformationen",
            "contactEmail": "hello@mandari.de",
            "website": "https://mandari.de",
            "vendor": "https://mandari.de",
            "product": "https://github.com/mandariOSS/mandari",
        }
    )


def serialize_body(body, ctx=None):
    raw = body.raw_json or {}
    return _clean(
        {
            "id": obj_url("body", body.id),
            "type": schema_type("body"),
            "system": system_url(),
            "name": body.name,
            "shortName": body.short_name,
            "website": body.website,
            "license": body.license,
            "licenseValidSince": iso(body.license_valid_since),
            "oparlSince": raw.get("oparlSince"),
            "ags": raw.get("ags"),
            "rgs": raw.get("rgs"),
            "equivalent": raw.get("equivalent"),
            "contactEmail": raw.get("contactEmail"),
            "contactName": raw.get("contactName"),
            "classification": body.classification,
            "organization": sub_list_url(body.id, "organizations"),
            "person": sub_list_url(body.id, "people"),
            "meeting": sub_list_url(body.id, "meetings"),
            "paper": sub_list_url(body.id, "papers"),
            "legislativeTerm": [serialize_legislative_term(term) for term in body.legislative_terms.all()],
            "web": f"{site_url()}/insight/",
            **_timestamps(body),
            "mandari:originalId": body.external_id,
            "mandari:slug": body.slug,
            "mandari:displayName": body.get_display_name(),
            "mandari:locationList": sub_list_url(body.id, "locations"),
        }
    )


def serialize_organization(organization, ctx=None):
    raw = organization.raw_json or {}
    return _clean(
        {
            "id": obj_url("organization", organization.id),
            "type": schema_type("organization"),
            "body": obj_url("body", organization.body_id),
            "name": organization.name,
            "shortName": organization.short_name,
            "organizationType": organization.organization_type,
            "classification": organization.classification,
            "post": raw.get("post"),
            "startDate": iso_date(organization.start_date),
            "endDate": iso_date(organization.end_date),
            "website": organization.website,
            "membership": [obj_url("membership", m.id) for m in organization.memberships.all()],
            "web": f"{site_url()}/insight/gremien/{organization.id}/",
            **_timestamps(organization),
            "mandari:originalId": organization.external_id,
        }
    )


def serialize_person(person, ctx=None):
    raw = person.raw_json or {}
    return _clean(
        {
            "id": obj_url("person", person.id),
            "type": schema_type("person"),
            "body": obj_url("body", person.body_id),
            "name": person.name or person.display_name,
            "familyName": person.family_name,
            "givenName": person.given_name,
            "formOfAddress": raw.get("formOfAddress"),
            "affix": raw.get("affix"),
            "title": _as_list(raw.get("title") or person.title),
            "gender": person.gender,
            "email": _as_list(raw.get("email") or person.email),
            "phone": _as_list(raw.get("phone") or person.phone),
            "status": _as_list(raw.get("status")),
            "life": raw.get("life"),
            "lifeSource": raw.get("lifeSource"),
            # OParl 1.1 bettet Memberships in Person ein
            "membership": [serialize_membership(m) for m in person.memberships.all()],
            "web": f"{site_url()}/insight/personen/{person.id}/",
            **_timestamps(person),
            "mandari:originalId": person.external_id,
        }
    )


def serialize_membership(membership, ctx=None):
    return _clean(
        {
            "id": obj_url("membership", membership.id),
            "type": schema_type("membership"),
            "person": obj_url("person", membership.person_id),
            "organization": obj_url("organization", membership.organization_id),
            "role": membership.role,
            "votingRight": membership.voting_right,
            "startDate": iso_date(membership.start_date),
            "endDate": iso_date(membership.end_date),
            **_timestamps(membership),
            "mandari:originalId": membership.external_id,
        }
    )


def serialize_meeting(meeting, ctx):
    raw = meeting.raw_json or {}
    files = list(meeting.files.all())
    files_by_ext = {f.external_id: f for f in files}

    invitation = files_by_ext.get(_file_ext(raw.get("invitation")))
    results_protocol = files_by_ext.get(_file_ext(raw.get("resultsProtocol")))
    verbatim_protocol = files_by_ext.get(_file_ext(raw.get("verbatimProtocol")))
    special = {f.pk for f in (invitation, results_protocol, verbatim_protocol) if f is not None}
    auxiliary = [f for f in files if f.pk not in special]

    location = ctx.location_by_ext.get(_location_ext(raw))

    return _clean(
        {
            "id": obj_url("meeting", meeting.id),
            "type": schema_type("meeting"),
            "name": meeting.name,
            "meetingState": meeting.meeting_state,
            "cancelled": meeting.cancelled,
            "start": iso(meeting.start),
            "end": iso(meeting.end),
            "location": serialize_location(location) if location else None,
            "organization": [obj_url("organization", org.id) for org in meeting.organizations.all()],
            "invitation": serialize_file(invitation) if invitation else None,
            "resultsProtocol": serialize_file(results_protocol) if results_protocol else None,
            "verbatimProtocol": serialize_file(verbatim_protocol) if verbatim_protocol else None,
            "auxiliaryFile": [serialize_file(f) for f in auxiliary],
            # OParl 1.1 bettet Tagesordnungspunkte in Meeting ein
            "agendaItem": [serialize_agenda_item(item, ctx) for item in meeting.agenda_items.all()],
            "web": f"{site_url()}/insight/termine/{meeting.id}/",
            **_timestamps(meeting),
            "mandari:originalId": meeting.external_id,
            "mandari:locationName": meeting.location_name if not location else None,
            "mandari:locationAddress": meeting.location_address if not location else None,
        }
    )


def serialize_agenda_item(item, ctx):
    consultation_ids = ctx.consultations_by_agenda_ext.get(item.external_id, [])
    return _clean(
        {
            "id": obj_url("agendaitem", item.id),
            "type": schema_type("agendaitem"),
            "meeting": obj_url("meeting", item.meeting_id),
            "number": item.number,
            "order": item.order,
            "name": item.name,
            "public": item.public,
            "consultation": obj_url("consultation", consultation_ids[0]) if consultation_ids else None,
            "result": item.result,
            "resolutionText": item.resolution_text,
            **_timestamps(item),
            "mandari:originalId": item.external_id,
        }
    )


def serialize_paper(paper, ctx):
    raw = paper.raw_json or {}
    files = list(paper.files.all())
    files_by_ext = {f.external_id: f for f in files}
    main_file = files_by_ext.get(_file_ext(raw.get("mainFile")))
    auxiliary = [f for f in files if main_file is None or f.pk != main_file.pk]

    return _clean(
        {
            "id": obj_url("paper", paper.id),
            "type": schema_type("paper"),
            "body": obj_url("body", paper.body_id),
            "name": paper.name,
            "reference": paper.reference,
            "date": iso_date(paper.date),
            "paperType": paper.paper_type,
            "mainFile": serialize_file(main_file) if main_file else None,
            "auxiliaryFile": [serialize_file(f) for f in auxiliary],
            # OParl 1.1 bettet Consultations in Paper ein
            "consultation": [serialize_consultation(c, ctx) for c in paper.consultations.all()],
            "web": f"{site_url()}/insight/vorgaenge/{paper.id}/",
            **_timestamps(paper),
            "mandari:originalId": paper.external_id,
            "mandari:summary": paper.summary,
        }
    )


def serialize_consultation(consultation, ctx):
    meeting_id = ctx.meeting_by_ext.get(consultation.meeting_external_id)
    agenda_item_id = ctx.agenda_item_by_ext.get(consultation.agenda_item_external_id)
    return _clean(
        {
            "id": obj_url("consultation", consultation.id),
            "type": schema_type("consultation"),
            "paper": obj_url("paper", consultation.paper_id) if consultation.paper_id else None,
            "meeting": obj_url("meeting", meeting_id) if meeting_id else None,
            "agendaItem": obj_url("agendaitem", agenda_item_id) if agenda_item_id else None,
            "authoritative": consultation.authoritative,
            "role": consultation.role,
            **_timestamps(consultation),
            "mandari:originalId": consultation.external_id,
        }
    )


def serialize_file(file_obj, ctx=None, include_text=False):
    proxy_url = f"{site_url()}/insight/dokumente/{file_obj.id}/preview/"
    return _clean(
        {
            "id": obj_url("file", file_obj.id),
            "type": schema_type("file"),
            "name": file_obj.name,
            "fileName": file_obj.file_name,
            "mimeType": file_obj.mime_type,
            "size": file_obj.size,
            "date": iso(file_obj.file_date),
            # Dateien werden über unseren Proxy ausgeliefert (DSGVO-konform,
            # stabil auch wenn der Quellserver offline ist)
            "accessUrl": proxy_url,
            "downloadUrl": f"{proxy_url}?download=1",
            "text": (file_obj.text_content or None) if include_text else None,
            "paper": [obj_url("paper", file_obj.paper_id)] if file_obj.paper_id else None,
            "meeting": [obj_url("meeting", file_obj.meeting_id)] if file_obj.meeting_id else None,
            **_timestamps(file_obj),
            "mandari:originalId": file_obj.external_id,
            "mandari:originalAccessUrl": file_obj.access_url,
            "mandari:sha256": file_obj.sha256_hash,
            "mandari:pageCount": file_obj.page_count,
        }
    )


def serialize_location(location, ctx=None):
    return _clean(
        {
            "id": obj_url("location", location.id),
            "type": schema_type("location"),
            "description": location.description,
            "streetAddress": location.street_address,
            "room": location.room,
            "postalCode": location.postal_code,
            "locality": location.locality,
            "geojson": location.geojson,
            "bodies": [obj_url("body", location.body_id)] if location.body_id else None,
            **_timestamps(location),
            "mandari:originalId": location.external_id,
        }
    )


def serialize_legislative_term(term, ctx=None):
    return _clean(
        {
            "id": obj_url("legislativeterm", term.id),
            "type": schema_type("legislativeterm"),
            "body": obj_url("body", term.body_id) if term.body_id else None,
            "name": term.name,
            "startDate": iso_date(term.start_date),
            "endDate": iso_date(term.end_date),
            **_timestamps(term),
            "mandari:originalId": term.external_id,
        }
    )

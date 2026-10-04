# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abbildung der Objekte des RIS-Bestands auf das kanonische RIS-Modell (OParl 1.1).

Der RIS-Bestand (``insight_core``, Tabellen ``oparl_*``) hält die Ratsinformationen aller Kommunen:
aus fremden Ratsinformationssystemen geerntet und von Session-Mandanten gespiegelt. Diese Abbildung
ist die einzige Stelle, die seine Zeilen in kanonische Objekte übersetzt; der Aggregator
(``hub.api.aggregator``) gibt genau sie aus.

Grundsätze:

- **Kennungen und alle Verweise zeigen auf die eigene Schnittstelle** (``BestandUris``), nie auf die
  Adressen der Quellsysteme. Verweise werden bevorzugt über die Fremdschlüssel aufgelöst; wo der
  Bestand nur die Kennung der Quelle kennt (Beratung → Sitzung/Tagesordnungspunkt, Sitzung → Ort,
  Tagesordnungspunkt → Beratung), löst ``RefContext`` je Seite in einer Abfrage je Typ auf.
- ``created``/``modified`` kommen aus den Zeitstempeln der Quelle, ersatzweise aus den eigenen.
- Die Adresse im Quellsystem bleibt als Erweiterung ``mandari:originalId`` erhalten; ``web`` verweist
  auf die Seite im Bürgerportal.
- Dateien werden über den eigenen Abruf ausgeliefert (``accessUrl``/``downloadUrl``), nicht vom
  Server der Quelle.
- **Gelöschtes** (OParl 1.1 §2.8): In der Quelle gelöschte oder zurückgenommene Objekte sind im Bestand
  nur markiert und erscheinen als gekürzte Objekte (``tombstone``). Eingebettet werden sie nie: Die
  Vorlade-Funktionen (``prepare_*``) und ``RefContext`` lassen sie aus.
- **Keine Abfragen je Objekt.** Wer Listen abbildet, lädt die Beziehungen mit ``prepare_*`` vor und
  baut je Seite einen ``RefContext``.

Bekannte Einschränkung: Verweise ohne Fremdschlüssel im Bestand (``participant``, ``originatorPerson``,
``subOrganizationOf`` …) entfallen, statt Adressen der Quelle durchzureichen.

``VERSION`` steigt, wenn sich für dasselbe Objekt des Bestands die Abbildung ändert.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any, Final

from django.db.models import Prefetch, QuerySet

from hub.ris.canonical import (
    ORGANIZATION_TYPES,
    Objekt,
    as_list,
    clean,
    iso,
    iso_date,
    iso_day,
    schema_type,
    tombstone,
)
from hub.ris.mapping.session import ORGANIZATION_TYPES as SESSION_ORGANIZATION_TYPES
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
from insight_core.services.file_reconcile import is_blocked

#: Version der Abbildung
VERSION: Final = 1

# Verbreitete Angaben fremder Quellen, die keiner der sieben Werte sind, aber eindeutig dazugehören.
# Manche RIS ordnen nach dem Kommunalrecht: Hauptorgan (Rat, Kreistag) und Hilfsorgan (Ausschüsse,
# Beiräte) sind Gremien; Amt, Dienststelle und Organisationseinheit gehören zur Verwaltung.
_ORGANIZATION_TYPE_SYNONYMS: Final[dict[str, str]] = {
    "ausschuss": "Gremium",
    "ausschüsse": "Gremium",
    "rat": "Gremium",
    "beirat": "Gremium",
    "beiräte": "Gremium",
    "kommission": "Gremium",
    "kommissionen": "Gremium",
    "gremien": "Gremium",
    "hauptorgan": "Gremium",
    "hauptorgane": "Gremium",
    "hilfsorgan": "Gremium",
    "hilfsorgane": "Gremium",
    "fraktionen": "Fraktion",
    "parteien": "Partei",
    "institutionen": "Institution",
    "amt": "Verwaltungsbereich",
    "ämter": "Verwaltungsbereich",
    "fachbereich": "Verwaltungsbereich",
    "fachbereiche": "Verwaltungsbereich",
    "dezernat": "Verwaltungsbereich",
    "dezernate": "Verwaltungsbereich",
    "dienststelle": "Verwaltungsbereich",
    "dienststellen": "Verwaltungsbereich",
    "organisationseinheit": "Verwaltungsbereich",
    "organisationseinheiten": "Verwaltungsbereich",
    "verwaltung": "Verwaltungsbereich",
}

_ORGANIZATION_TYPE_LOOKUP: Final[dict[str, str]] = {
    **_ORGANIZATION_TYPE_SYNONYMS,
    **SESSION_ORGANIZATION_TYPES,
    **{value.casefold(): value for value in ORGANIZATION_TYPES},
}


def organization_type(value: object) -> str | None:
    """
    ``organizationType`` als Wert der Spezifikation (oder ``None`` ohne Angabe).

    Werte der Spezifikation bleiben (Schreibweise vereinheitlicht), Schlüssel des Session-RIS und
    verbreitete Angaben fremder Quellen werden zugeordnet, alles andere gilt als „Sonstiges“.
    """
    text = str(value or "").strip()
    if not text:
        return None
    return _ORGANIZATION_TYPE_LOOKUP.get(text.casefold(), "Sonstiges")


# =============================================================================
# Adressen
# =============================================================================


class BestandUris:
    """
    Adressen der Objekte des RIS-Bestands in der eigenen Schnittstelle.

    ``base`` ist die öffentliche Adresse des Aggregators (``OPARL_BASE_URL``), ``site`` die der
    Installation (``SITE_URL``, für ``web`` und den Datei-Abruf) – beide unabhängig vom Host einer
    Anfrage.
    """

    def __init__(self, base: str, site: str) -> None:
        self.base = base.rstrip("/")
        self.site = site.rstrip("/")

    def system(self) -> str:
        return f"{self.base}/v1/system"

    def bodies(self) -> str:
        return f"{self.base}/v1/bodies"

    def obj(self, kind: str, pk: Any) -> str:
        return f"{self.base}/v1/{kind}/{pk}"

    def list(self, body_id: Any, segment: str) -> str:
        """Externe Objektliste einer Kommune."""
        return f"{self.base}/v1/body/{body_id}/{segment}"

    def changes(self, body_id: Any) -> str:
        """Änderungsfeed einer Kommune (kompatible Erweiterung von OParl 1.1)."""
        return self.list(body_id, "changes")

    def snapshot(self, body_id: Any) -> str:
        """Snapshot einer Kommune: Einstieg in den Änderungsfeed."""
        return self.list(body_id, "snapshot")

    def web(self, path: str) -> str:
        """Seite im Bürgerportal."""
        return f"{self.site}/insight/{path}"


# =============================================================================
# Vorladen: Beziehungen, die die Abbildung liest (ohne Gelöschtes)
# =============================================================================


def prepare_meetings(queryset: QuerySet[OParlMeeting]) -> QuerySet[OParlMeeting]:
    return queryset.prefetch_related(
        Prefetch("organizations", queryset=OParlOrganization.objects.filter(deleted=False).only("id")),
        Prefetch("agenda_items", queryset=OParlAgendaItem.objects.filter(deleted=False)),
        Prefetch("files", queryset=OParlFile.objects.filter(deleted=False)),
    )


def prepare_papers(queryset: QuerySet[OParlPaper]) -> QuerySet[OParlPaper]:
    return queryset.prefetch_related(
        Prefetch("files", queryset=OParlFile.objects.filter(deleted=False)),
        Prefetch("consultations", queryset=OParlConsultation.objects.filter(deleted=False)),
    )


def prepare_organizations(queryset: QuerySet[OParlOrganization]) -> QuerySet[OParlOrganization]:
    return queryset.prefetch_related(
        Prefetch(
            "memberships",
            queryset=OParlMembership.objects.filter(deleted=False).only("id", "organization_id"),
        ),
    )


def prepare_persons(queryset: QuerySet[OParlPerson]) -> QuerySet[OParlPerson]:
    return queryset.prefetch_related(
        Prefetch("memberships", queryset=OParlMembership.objects.filter(deleted=False)),
    )


def prepare_bodies(queryset: QuerySet[OParlBody]) -> QuerySet[OParlBody]:
    return queryset.prefetch_related(
        Prefetch("legislative_terms", queryset=OParlLegislativeTerm.objects.filter(deleted=False)),
    )


# =============================================================================
# Verweise über Kennungen der Quelle: je Seite eine Abfrage je Typ
# =============================================================================


def _reference(value: object) -> str | None:
    """Kennung eines Verweises aus den Rohdaten (Adresse oder eingebettetes Objekt)."""
    if isinstance(value, dict):
        found = value.get("id")
        return found if isinstance(found, str) else None
    if isinstance(value, str):
        return value
    return None


def _location_ext(raw_json: Any) -> str | None:
    """Kennung des Ortes aus den Rohdaten einer Sitzung."""
    return _reference((raw_json or {}).get("location"))


class RefContext:
    """Löst Verweise über Kennungen der Quelle für eine ganze Seite in einer Abfrage je Typ auf."""

    def __init__(self) -> None:
        # Kennung der Quelle -> Kennung im Bestand
        self.meeting_by_ext: dict[str, Any] = {}
        self.agenda_item_by_ext: dict[str, Any] = {}
        # Kennung der Quelle -> Ort (wird eingebettet)
        self.location_by_ext: dict[str, OParlLocation] = {}
        # Kennung des Tagesordnungspunkts in der Quelle -> Kennungen seiner Beratungen
        self.consultations_by_agenda_ext: dict[str, list[Any]] = {}

    @classmethod
    def empty(cls, objects: Iterable[Any]) -> RefContext:
        return cls()

    @classmethod
    def for_meetings(cls, meetings: Iterable[OParlMeeting]) -> RefContext:
        """Für Sitzungen: Ort und die Beratungen der eingebetteten Tagesordnungspunkte."""
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
    def for_agenda_items(cls, items: Iterable[OParlAgendaItem]) -> RefContext:
        ctx = cls()
        ctx._load_consultations({item.external_id for item in items})
        return ctx

    @classmethod
    def for_consultations(cls, consultations: Iterable[OParlConsultation]) -> RefContext:
        """Für Beratungen: Sitzung und Tagesordnungspunkt kennt der Bestand nur über die Kennung der Quelle."""
        ctx = cls()
        consultations = list(consultations)
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
    def for_papers(cls, papers: Iterable[OParlPaper]) -> RefContext:
        """Für Vorlagen: Die eingebetteten Beratungen brauchen Sitzung und Tagesordnungspunkt."""
        return cls.for_consultations([c for paper in papers for c in paper.consultations.all()])

    def _load_consultations(self, agenda_exts: set[str]) -> None:
        if not agenda_exts:
            return
        pairs = OParlConsultation.objects.filter(agenda_item_external_id__in=agenda_exts, deleted=False).values_list(
            "agenda_item_external_id", "id"
        )
        for agenda_ext, consultation_id in pairs:
            if agenda_ext is not None:
                self.consultations_by_agenda_ext.setdefault(agenda_ext, []).append(consultation_id)


# =============================================================================
# Abbildung je Objekttyp
# =============================================================================


def _timestamps(obj: Any) -> Objekt:
    """``created``/``modified`` aus den Zeitstempeln der Quelle, ersatzweise aus den eigenen."""
    return {
        "created": iso(obj.oparl_created or obj.created_at),
        "modified": iso(obj.oparl_modified or obj.updated_at),
    }


def _file_day(file_obj: OParlFile) -> str | None:
    """
    ``File.date`` als Datum ``yyyy-mm-dd``: der Tag, den die Quelle nennt.

    Nennt die Quelle ein reines Datum, gilt es unverändert. Nennt sie einen Zeitpunkt (oder fehlen die
    Rohdaten), gilt der Tag des gespeicherten Zeitpunkts in der Zeitzone der Installation: Mitternacht
    UTC – so speichert der Ingestor ein reines Datum – bleibt dort derselbe Tag, und ein Zeitpunkt mit
    lokalem Versatz (``2026-03-05T00:00:00+01:00``) rutscht nicht auf den Vortag.
    """
    stated = (file_obj.raw_json or {}).get("date")
    if isinstance(stated, str) and len(stated) == 10:
        try:
            return date.fromisoformat(stated).isoformat()
        except ValueError:
            pass
    return iso_day(file_obj.file_date)


class BestandMapping:
    """Abbildung der Objekte des RIS-Bestands; je Objekttyp eine Methode, Ergebnis ist ein OParl-Objekt."""

    def __init__(self, base: str, site: str, *, license_url: str = "", changes: bool = False) -> None:
        self.uris = BestandUris(base, site)
        self.license_url = license_url
        #: Die Ausgabe bietet Änderungsfeed und Snapshot an; der Body nennt dann deren Adressen
        self.changes = changes

    # -- System, Körperschaft, Wahlperiode -------------------------------------------------------

    def system(self) -> Objekt:
        return clean(
            {
                "id": self.uris.system(),
                "type": schema_type("system"),
                "oparlVersion": "https://schema.oparl.org/1.1/",
                # Übergreifende Lizenz nur, wenn der Betreiber eine festlegt (OPARL_LICENSE_URL); sonst gilt
                # die Angabe der jeweiligen Kommune am Body
                "license": self.license_url or None,
                "body": self.uris.bodies(),
                "name": "mandari — aggregierte Ratsinformationen",
                "contactEmail": "hello@mandari.de",
                "website": "https://mandari.de",
                "vendor": "https://mandari.de",
                "product": "https://github.com/mandariOSS/mandari",
            }
        )

    def body(self, body: OParlBody, ctx: RefContext | None = None) -> Objekt:
        raw = body.raw_json or {}
        # Feed und Snapshot gibt es nur für gelistete Kommunen (``hub.api.aggregator``)
        offers_feed = self.changes and body.is_listed
        data = clean(
            {
                "id": self.uris.obj("body", body.id),
                "type": schema_type("body"),
                "system": self.uris.system(),
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
                "organization": self.uris.list(body.id, "organizations"),
                "person": self.uris.list(body.id, "people"),
                "meeting": self.uris.list(body.id, "meetings"),
                "paper": self.uris.list(body.id, "papers"),
                "locationList": self.uris.list(body.id, "locations"),
                "web": self.uris.web(""),
                **_timestamps(body),
                "mandari:originalId": body.external_id,
                "mandari:slug": body.slug,
                "mandari:displayName": body.get_display_name(),
                # Abgekündigt: dieselbe URL steht im Standardfeld ``locationList``
                "mandari:locationList": self.uris.list(body.id, "locations"),
                "mandari:changes": self.uris.changes(body.id) if offers_feed else None,
                "mandari:snapshot": self.uris.snapshot(body.id) if offers_feed else None,
            }
        )
        # Pflichtfeld in OParl 1.1: auch ohne Wahlperiode vorhanden (leere Liste)
        data["legislativeTerm"] = [self.legislative_term(term) for term in body.legislative_terms.all()]
        return data

    def legislative_term(self, term: OParlLegislativeTerm, ctx: RefContext | None = None) -> Objekt:
        return clean(
            {
                "id": self.uris.obj("legislativeterm", term.id),
                "type": schema_type("legislativeterm"),
                "body": self.uris.obj("body", term.body_id) if term.body_id else None,
                "name": term.name,
                "startDate": iso_date(term.start_date),
                "endDate": iso_date(term.end_date),
                **_timestamps(term),
                "mandari:originalId": term.external_id,
            }
        )

    # -- Gremien, Personen, Mitgliedschaften -----------------------------------------------------

    def organization(self, organization: OParlOrganization, ctx: RefContext | None = None) -> Objekt:
        raw = organization.raw_json or {}
        kind = organization_type(organization.organization_type)
        return clean(
            {
                "id": self.uris.obj("organization", organization.id),
                "type": schema_type("organization"),
                "body": self.uris.obj("body", organization.body_id),
                "name": organization.name,
                "shortName": organization.short_name,
                "organizationType": kind,
                "classification": organization.classification,
                "post": raw.get("post"),
                "startDate": iso_date(organization.start_date),
                "endDate": iso_date(organization.end_date),
                "website": organization.website,
                "membership": [self.uris.obj("membership", m.id) for m in organization.memberships.all()],
                "web": self.uris.web(f"gremien/{organization.id}/"),
                **_timestamps(organization),
                "mandari:originalId": organization.external_id,
                # Angabe der Quelle, wenn sie keiner der Werte der Spezifikation ist
                "mandari:originalOrganizationType": organization.organization_type
                if organization.organization_type != kind
                else None,
            }
        )

    def person(self, person: OParlPerson, ctx: RefContext | None = None) -> Objekt:
        raw = person.raw_json or {}
        return clean(
            {
                "id": self.uris.obj("person", person.id),
                "type": schema_type("person"),
                "body": self.uris.obj("body", person.body_id),
                "name": person.name or person.display_name,
                "familyName": person.family_name,
                "givenName": person.given_name,
                "formOfAddress": raw.get("formOfAddress"),
                "affix": raw.get("affix"),
                "title": as_list(raw.get("title") or person.title),
                "gender": person.gender,
                "email": as_list(raw.get("email") or person.email),
                "phone": as_list(raw.get("phone") or person.phone),
                "status": as_list(raw.get("status")),
                "life": raw.get("life"),
                "lifeSource": raw.get("lifeSource"),
                # OParl 1.1 bettet Memberships in Person ein
                "membership": [self.membership(m) for m in person.memberships.all()],
                "web": self.uris.web(f"personen/{person.id}/"),
                **_timestamps(person),
                "mandari:originalId": person.external_id,
            }
        )

    def membership(self, membership: OParlMembership, ctx: RefContext | None = None) -> Objekt:
        return clean(
            {
                "id": self.uris.obj("membership", membership.id),
                "type": schema_type("membership"),
                "person": self.uris.obj("person", membership.person_id),
                "organization": self.uris.obj("organization", membership.organization_id),
                "role": membership.role,
                "votingRight": membership.voting_right,
                "startDate": iso_date(membership.start_date),
                "endDate": iso_date(membership.end_date),
                **_timestamps(membership),
                "mandari:originalId": membership.external_id,
            }
        )

    # -- Sitzung, Ort, Tagesordnung ------------------------------------------------------------------

    def meeting(self, meeting: OParlMeeting, ctx: RefContext) -> Objekt:
        raw = meeting.raw_json or {}
        files = list(meeting.files.all())
        files_by_ext = {f.external_id: f for f in files}

        invitation = files_by_ext.get(_reference(raw.get("invitation")) or "")
        results_protocol = files_by_ext.get(_reference(raw.get("resultsProtocol")) or "")
        verbatim_protocol = files_by_ext.get(_reference(raw.get("verbatimProtocol")) or "")
        special = {f.pk for f in (invitation, results_protocol, verbatim_protocol) if f is not None}
        auxiliary = [f for f in files if f.pk not in special]

        location = ctx.location_by_ext.get(_location_ext(raw) or "")
        location_data = self.location(location) if location else self.meeting_location(meeting)

        return clean(
            {
                "id": self.uris.obj("meeting", meeting.id),
                "type": schema_type("meeting"),
                "name": meeting.name,
                "meetingState": meeting.meeting_state,
                "cancelled": meeting.cancelled,
                "start": iso(meeting.start),
                "end": iso(meeting.end),
                "location": location_data,
                "organization": [self.uris.obj("organization", org.id) for org in meeting.organizations.all()],
                "invitation": self.file(invitation) if invitation else None,
                "resultsProtocol": self.file(results_protocol) if results_protocol else None,
                "verbatimProtocol": self.file(verbatim_protocol) if verbatim_protocol else None,
                "auxiliaryFile": [self.file(f) for f in auxiliary],
                # OParl 1.1 bettet Tagesordnungspunkte in Meeting ein
                "agendaItem": [self.agenda_item(item, ctx) for item in meeting.agenda_items.all()],
                "web": self.uris.web(f"termine/{meeting.id}/"),
                **_timestamps(meeting),
                "mandari:originalId": meeting.external_id,
                # Abgekündigt: Der Ort steht als Location-Objekt in ``location``
                "mandari:locationName": meeting.location_name if not location else None,
                "mandari:locationAddress": meeting.location_address if not location else None,
            }
        )

    def location(self, location: OParlLocation, ctx: RefContext | None = None) -> Objekt:
        return clean(
            {
                "id": self.uris.obj("location", location.id),
                "type": schema_type("location"),
                "description": location.description,
                "streetAddress": location.street_address,
                "room": location.room,
                "postalCode": location.postal_code,
                "locality": location.locality,
                "geojson": location.geojson,
                "bodies": [self.uris.obj("body", location.body_id)] if location.body_id else None,
                **_timestamps(location),
                "mandari:originalId": location.external_id,
            }
        )

    def meeting_location(self, meeting: OParlMeeting, ctx: RefContext | None = None) -> Objekt | None:
        """
        Sitzungsort als Location-Objekt, wenn die Quelle nur Text geliefert hat (kein eigenes Location-Objekt).

        Das Objekt gehört zur Sitzung und trägt deren Kennung: ``…/location/<Kennung der Sitzung>``.
        ``None`` ohne Ortsangabe.
        """
        parts = [part for part in (meeting.location_name, meeting.location_address) if part]
        if not parts:
            return None
        return clean(
            {
                "id": self.uris.obj("location", meeting.id),
                "type": schema_type("location"),
                "description": ", ".join(parts),
                "bodies": [self.uris.obj("body", meeting.body_id)] if meeting.body_id else None,
                "meetings": [self.uris.obj("meeting", meeting.id)],
                **_timestamps(meeting),
            }
        )

    def agenda_item(self, item: OParlAgendaItem, ctx: RefContext) -> Objekt:
        consultation_ids = ctx.consultations_by_agenda_ext.get(item.external_id, [])
        raw = item.raw_json or {}
        return clean(
            {
                "id": self.uris.obj("agendaitem", item.id),
                "type": schema_type("agendaitem"),
                "meeting": self.uris.obj("meeting", item.meeting_id),
                "number": item.number,
                "order": item.order,
                "name": item.name,
                "public": item.public,
                "consultation": self.uris.obj("consultation", consultation_ids[0]) if consultation_ids else None,
                "result": item.result,
                "resolutionText": item.resolution_text,
                **_timestamps(item),
                "mandari:originalId": item.external_id,
                # Abstimmungsergebnis aus dem Quell-RIS (mandari Session): Summen immer, Einzelstimmen nur,
                # wenn die Quelle sie bei namentlicher Abstimmung liefert
                "mandari:vote": raw.get("mandari:vote") or None,
                "mandari:rollCall": raw.get("mandari:rollCall") or None,
            }
        )

    # -- Vorlage, Beratung, Datei -----------------------------------------------------------------

    def paper(self, paper: OParlPaper, ctx: RefContext) -> Objekt:
        raw = paper.raw_json or {}
        files = list(paper.files.all())
        files_by_ext = {f.external_id: f for f in files}
        main_file = files_by_ext.get(_reference(raw.get("mainFile")) or "")
        auxiliary = [f for f in files if main_file is None or f.pk != main_file.pk]

        return clean(
            {
                "id": self.uris.obj("paper", paper.id),
                "type": schema_type("paper"),
                "body": self.uris.obj("body", paper.body_id),
                "name": paper.name,
                "reference": paper.reference,
                "date": iso_date(paper.date),
                "paperType": paper.paper_type,
                "mainFile": self.file(main_file) if main_file else None,
                "auxiliaryFile": [self.file(f) for f in auxiliary],
                # OParl 1.1 bettet Consultations in Paper ein
                "consultation": [self.consultation(c, ctx) for c in paper.consultations.all()],
                "web": self.uris.web(f"vorgaenge/{paper.id}/"),
                **_timestamps(paper),
                "mandari:originalId": paper.external_id,
                "mandari:summary": paper.summary,
            }
        )

    def consultation(self, consultation: OParlConsultation, ctx: RefContext) -> Objekt:
        meeting_id = ctx.meeting_by_ext.get(consultation.meeting_external_id or "")
        agenda_item_id = ctx.agenda_item_by_ext.get(consultation.agenda_item_external_id or "")
        return clean(
            {
                "id": self.uris.obj("consultation", consultation.id),
                "type": schema_type("consultation"),
                "paper": self.uris.obj("paper", consultation.paper_id) if consultation.paper_id else None,
                "meeting": self.uris.obj("meeting", meeting_id) if meeting_id else None,
                "agendaItem": self.uris.obj("agendaitem", agenda_item_id) if agenda_item_id else None,
                "authoritative": consultation.authoritative,
                "role": consultation.role,
                **_timestamps(consultation),
                "mandari:originalId": consultation.external_id,
            }
        )

    def file(self, file_obj: OParlFile, ctx: RefContext | None = None, *, include_text: bool = False) -> Objekt:
        proxy_url = self.uris.web(f"dokumente/{file_obj.id}/preview/")
        return clean(
            {
                "id": self.uris.obj("file", file_obj.id),
                "type": schema_type("file"),
                "name": file_obj.name,
                "fileName": file_obj.file_name,
                "mimeType": file_obj.mime_type,
                "size": file_obj.size,
                # OParl 1.1: Datum (yyyy-mm-dd), kein Zeitpunkt
                "date": _file_day(file_obj),
                # Dateien werden über den eigenen Abruf ausgeliefert (stabil, auch wenn der Server der
                # Quelle nicht erreichbar ist; der Abnehmer verbindet sich nicht mit dem RIS der Kommune)
                "accessUrl": proxy_url,
                "downloadUrl": f"{proxy_url}?download=1",
                # Gesperrte Dokumente (in der Quelle gelöscht oder nicht mehr abrufbar, #787): kein Text
                "text": (file_obj.text_content or None) if include_text and not is_blocked(file_obj) else None,
                "paper": [self.uris.obj("paper", file_obj.paper_id)] if file_obj.paper_id else None,
                "meeting": [self.uris.obj("meeting", file_obj.meeting_id)] if file_obj.meeting_id else None,
                **_timestamps(file_obj),
                "mandari:originalId": file_obj.external_id,
                "mandari:originalAccessUrl": file_obj.access_url,
                "mandari:sha256": file_obj.sha256_hash,
                "mandari:pageCount": file_obj.page_count,
            }
        )

    def file_with_text(self, file_obj: OParlFile, ctx: RefContext | None = None) -> Objekt:
        """Datei samt erkanntem Text (Objekt-Endpunkt; in Listen und Einbettungen entfällt der Text)."""
        return self.file(file_obj, ctx, include_text=True)

    # -- Gelöschtes --------------------------------------------------------------------------------

    def tombstone(self, kind: str, obj: Any) -> Objekt:
        """
        Gekürztes Objekt für in der Quelle Gelöschtes oder Zurückgenommenes (OParl 1.1 §2.8).

        ``modified`` ist der Zeitpunkt der Löschung. ``kind`` kann vom Typ der Zeile abweichen: Der Ort
        einer Sitzung ohne eigenes Location-Objekt trägt die Kennung und die Zeitstempel der Sitzung.
        """
        return tombstone(
            self.uris.obj(kind, obj.id),
            kind,
            obj.oparl_created or obj.created_at,
            obj.oparl_modified or obj.deleted_at or obj.updated_at,
        )

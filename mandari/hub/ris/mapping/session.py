# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abbildung der Objekte von mandari Session auf das kanonische RIS-Modell (OParl 1.1).

Die einzige Stelle, die Session-Objekte in das Modell übersetzt: Die Session-OParl-Schnittstelle
(``apps/session/api/oparl.py``) gibt genau diese Abbildung aus, weitere Abnehmer der Drehscheibe
bauen darauf auf. Eigene Erweiterungen tragen den Namensraum ``mandari:``.

Grundsätze:

- **Nur Öffentliches.** Abgebildet werden die öffentlichen Felder öffentlicher Objekte.
  Verschlüsselte Felder (Telefon, Adresse, Bankdaten, nichtöffentliche Beschlusstexte) liest die
  Abbildung nie; die E-Mail einer Person nur über ``published_email`` (Einwilligung). Verweise auf
  nichtöffentliche Sitzungen, Tagesordnungspunkte und Vorlagen entfallen.
- **Vorbedingung: Der Aufrufer reicht nur Öffentliches herein.** Welche Objekte eines Mandanten
  öffentlich sind, entscheidet das Fachmodul (``apps/session/oparl_publication.py``, Querysets
  ``visible_*``); die Abbildung wählt nicht aus. Sie verlässt sich aber nicht darauf: Eine
  nichtöffentliche Sitzung (samt Ort), ein nichtöffentlicher Tagesordnungspunkt, eine nicht
  veröffentlichte Vorlage oder Beratung und eine Datei ohne öffentliches Bezugsobjekt werden nicht
  abgebildet – die Methode bricht mit ``NotPublicError`` ab, statt Inhalte auszugeben.
- **IDs** (OParl-``id``) sind die öffentlichen Adressen der Objekte (``SessionUris``): aus der Adresse der
  Installation (``SITE_URL``), unabhängig vom Host einer Anfrage. Die kanonische Kennung eines Objekts im
  RIS-Bestand bildet sich aus derselben Adresse auf der festgeschriebenen Basis der Installation
  (``SessionUris.canonical_id``); ein Domainwechsel ändert die Adressen, nicht die Kennungen (Issue #733).
- **Keine Abhängigkeit zum Fachmodul.** Die Drehscheibe importiert Session nicht. Was die Abbildung
  von dort braucht – die Veröffentlichungsregel, Anzeigename und Typ einer Datei, das Sitzungsformat,
  die öffentliche Fassung der Niederschrift –, reicht der Aufrufer als ``SessionSource`` herein.
  Die Objekte selbst kommen als Instanzen der Session-Modelle; die Abbildung liest nur Attribute.
- **Keine Abfragen je Objekt.** Wer Listen abbildet, lädt die Beziehungen vorher (``select_related``,
  ``prefetch_related``); die Abbildung greift auf ``.all()`` der vorgeladenen Beziehungen zu.

``VERSION`` steigt, wenn sich für dasselbe Session-Objekt die Abbildung ändert.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from mandari_oparl.ids import canonical_id, canonical_uri

from hub.ris.canonical import Objekt, as_list, clean, iso, iso_date, iso_day, schema_type, tombstone

#: Version der Abbildung
VERSION: Final = 1

#: Art des Gremiums in Session (``SessionOrganization.organization_type``) -> ``organizationType``.
#: OParl 1.1 kennt sieben Werte; die feinere Art (Ausschuss, Rat, Beirat …) steht in ``classification``.
ORGANIZATION_TYPES: Final[dict[str, str]] = {
    "committee": "Gremium",
    "council": "Gremium",
    "advisory": "Gremium",
    "commission": "Gremium",
    "faction": "Fraktion",
    "department": "Verwaltungsbereich",
    "other": "Sonstiges",
}

#: Reihenfolge der Einzelstimmen: Ja, Nein, Enthaltung, dann Befangenheit
_VOTES: Final = ("yes", "no", "abstain", "excluded")


class NotPublicError(ValueError):
    """
    Ein nichtöffentliches Objekt wurde zur Abbildung gereicht.

    Das ist ein Fehler des Aufrufers (er wählt aus, was öffentlich ist) und nie ein Zustand der Daten:
    Die Abbildung gibt in diesem Fall nichts aus. Die Meldung nennt nur Art und Kennung, keine Inhalte.
    """


def _require_public(kind: str, obj: Any, public: bool) -> None:
    if not public:
        raise NotPublicError(f"{kind} {obj.pk}: nicht öffentlich, wird nicht abgebildet.")


@dataclass(frozen=True)
class SessionSource:
    """
    Was die Abbildung vom Fachmodul Session braucht, ohne es zu importieren.

    - ``is_published(obj)``: Ist das Objekt (Sitzung, TOP, Vorlage, Datei, Beratung) öffentlich sichtbar?
    - ``download_name(file)``: Anzeigename der Anlage (nie der Speichername)
    - ``mime_type(name)``: Typ, mit dem der Download ausgeliefert wird
    - ``meeting_format(meeting)``: Sitzungsformat mit Hinweis für die Öffentlichkeit
    - ``results_protocol(meeting)``: öffentliche Fassung der Niederschrift als Datei oder ``None``
    """

    is_published: Callable[[Any], bool]
    download_name: Callable[[Any], str]
    mime_type: Callable[[str], str]
    meeting_format: Callable[[Any], Any]
    results_protocol: Callable[[Any], Any]


class SessionUris:
    """
    Adressen und kanonische Kennungen der Objekte eines Mandanten.

    ``base`` ist die öffentliche Adresse seiner OParl-Schnittstelle und endet mit ``/``. Sie baut auf
    der aktuellen Adresse der Installation auf (``SITE_URL``), nicht auf dem Host einer Anfrage.

    ``id_base`` ist dieselbe Schnittstelle auf der festgeschriebenen Basis der Kennungen (Issue #733,
    ``apps.common.identifiers``); ohne Angabe gleich ``base``. Die kanonische Kennung eines Objekts ist
    ``uuid5`` über seine Adresse auf dieser Basis (``canonical_id``, ADR
    ``docs/adr/20260929-kanonisches-modell.md``): Adressen folgen der Domain, Kennungen nicht.
    """

    def __init__(self, base: str, id_base: str | None = None) -> None:
        self.base = base if base.endswith("/") else f"{base}/"
        self.id_base = (id_base if id_base.endswith("/") else f"{id_base}/") if id_base else self.base

    def canonical_id(self, uri: str) -> UUID:
        """Kanonische Kennung des Objekts unter der Adresse ``uri`` dieser Schnittstelle."""
        return canonical_id(canonical_uri(uri, self.base, self.id_base))

    def system(self) -> str:
        return self.base

    def body(self) -> str:
        return f"{self.base}body/"

    def bodies(self) -> str:
        return f"{self.base}bodies/"

    def list(self, segment: str) -> str:
        return f"{self.base}{segment}/"

    def obj(self, kind: str, pk: Any) -> str:
        return f"{self.base}{kind}/{pk}/"

    def file_download(self, pk: Any) -> str:
        return f"{self.base}file/{pk}/download/"

    def changes(self) -> str:
        """Änderungsfeed des Mandanten (kompatible Erweiterung von OParl 1.1)."""
        return f"{self.base}body/changes/"

    def snapshot(self) -> str:
        """Snapshot des Mandanten: Einstieg in den Änderungsfeed."""
        return f"{self.base}body/snapshot/"


def _timestamps(obj: Any) -> Objekt:
    return {"created": iso(obj.created_at), "modified": iso(obj.updated_at)}


class SessionMapping:
    """Abbildung der Objekte eines Mandanten; je Objekttyp eine Methode, Ergebnis ist ein OParl-Objekt."""

    def __init__(
        self, tenant: Any, base: str, source: SessionSource, *, changes: bool = False, id_base: str | None = None
    ) -> None:
        self.tenant = tenant
        #: Adressen aus ``base``, kanonische Kennungen aus ``id_base`` (``SessionUris``)
        self.uris = SessionUris(base, id_base)
        self.source = source
        #: Die Ausgabe bietet Änderungsfeed und Snapshot an; der Body nennt dann deren Adressen
        self.changes = changes

    # -- System, Körperschaft, Wahlperiode -------------------------------------------------------

    def system(self) -> Objekt:
        tenant = self.tenant
        return clean(
            {
                "id": self.uris.system(),
                "type": schema_type("system"),
                "oparlVersion": "https://schema.oparl.org/1.1/",
                # Lizenz der offenen Daten, sofern die Verwaltung eine festgelegt hat (Einstellungen)
                "license": tenant.oparl_license or None,
                "body": self.uris.bodies(),
                "name": f"Sitzungsdienst {tenant.name}",
                "contactEmail": tenant.contact_email,
                "contactName": tenant.name,
                "website": tenant.website,
                "vendor": "https://mandari.de",
                "product": "https://github.com/mandariOSS/mandari",
                **_timestamps(tenant),
            }
        )

    def body(self) -> Objekt:
        tenant = self.tenant
        body = clean(
            {
                "id": self.uris.body(),
                "type": schema_type("body"),
                "system": self.uris.system(),
                "name": tenant.name,
                "shortName": tenant.short_name,
                "website": tenant.website,
                "license": tenant.oparl_license or None,
                "licenseValidSince": iso(tenant.oparl_license_valid_since) if tenant.oparl_license else None,
                "contactEmail": tenant.contact_email,
                # Körperschaftstyp und AGS aus dem Anlegen des Mandanten (Issue #317); ohne Angabe wie bisher
                "classification": tenant.get_body_type_display() if tenant.body_type else "Kommune",
                "ags": tenant.ags or None,
                "organization": self.uris.list("organizations"),
                "person": self.uris.list("people"),
                "meeting": self.uris.list("meetings"),
                "paper": self.uris.list("papers"),
                "membership": self.uris.list("memberships"),
                "agendaItem": self.uris.list("agendaitems"),
                "consultation": self.uris.list("consultations"),
                "file": self.uris.list("files"),
                "legislativeTermList": self.uris.list("legislativeterms"),
                **_timestamps(tenant),
                "mandari:changes": self.uris.changes() if self.changes else None,
                "mandari:snapshot": self.uris.snapshot() if self.changes else None,
            }
        )
        # Pflichtfeld in OParl 1.1: auch ohne Wahlperiode vorhanden (leere Liste)
        body["legislativeTerm"] = [self.legislative_term(term) for term in tenant.legislative_terms.all()]
        return body

    def legislative_term(self, term: Any) -> Objekt:
        return clean(
            {
                "id": self.uris.obj("legislativeterm", term.id),
                "type": schema_type("legislativeterm"),
                "body": self.uris.body(),
                "name": term.name,
                "startDate": iso_date(term.start_date),
                "endDate": iso_date(term.end_date),
                **_timestamps(term),
            }
        )

    # -- Gremien, Personen, Mitgliedschaften -----------------------------------------------------

    def organization(self, org: Any) -> Objekt:
        return clean(
            {
                "id": self.uris.obj("organization", org.id),
                "type": schema_type("organization"),
                "body": self.uris.body(),
                "name": org.name,
                "shortName": org.short_name,
                "organizationType": ORGANIZATION_TYPES.get(org.organization_type, "Sonstiges"),
                "classification": org.get_organization_type_display(),
                "startDate": iso_date(org.start_date),
                "endDate": iso_date(org.end_date),
                "subOrganizationOf": self.uris.obj("organization", org.parent_id) if org.parent_id else None,
                "membership": [self.uris.obj("membership", m.id) for m in org.memberships.all()],
                **_timestamps(org),
            }
        )

    def person(self, person: Any) -> Objekt:
        """
        Person ohne geschützte Daten: Verschlüsselte Felder (Telefon, Adresse, Bankdaten) werden hier
        nie gelesen. Die E-Mail erscheint nur mit Kennzeichen „Kontaktdaten veröffentlichen“
        (Einwilligung, Issue #319); Datum und Nachweis der Einwilligung bleiben intern.
        """
        return clean(
            {
                "id": self.uris.obj("person", person.id),
                "type": schema_type("person"),
                "body": self.uris.body(),
                "name": person.display_name,
                "familyName": person.family_name,
                "givenName": person.given_name,
                "formOfAddress": person.form_of_address,
                "title": as_list(person.title),
                "email": as_list(person.published_email),
                # OParl 1.1 bettet Memberships in Person ein
                "membership": [self.membership(m) for m in person.memberships.all()],
                **_timestamps(person),
            }
        )

    def membership(self, membership: Any) -> Objekt:
        return clean(
            {
                "id": self.uris.obj("membership", membership.id),
                "type": schema_type("membership"),
                "person": self.uris.obj("person", membership.person_id),
                "organization": self.uris.obj("organization", membership.organization_id),
                "role": membership.get_role_display(),
                "votingRight": membership.has_voting_rights,
                "startDate": iso_date(membership.start_date),
                "endDate": iso_date(membership.end_date),
                **_timestamps(membership),
            }
        )

    # -- Sitzung, Ort, Tagesordnung ------------------------------------------------------------------

    def meeting(self, meeting: Any) -> Objekt:
        _require_public("meeting", meeting, meeting.is_public)
        protocol_file = self.source.results_protocol(meeting)
        protocol_file_id = protocol_file.pk if protocol_file is not None else None
        files = [f for f in meeting.files.all() if f.is_public and f.pk != protocol_file_id]
        items = [i for i in meeting.agenda_items.all() if i.is_public]
        items.sort(key=lambda i: (i.order, i.number))
        return clean(
            {
                "id": self.uris.obj("meeting", meeting.id),
                "type": schema_type("meeting"),
                "name": meeting.name,
                "meetingState": meeting.get_meeting_state_display(),
                "cancelled": meeting.cancelled,
                "start": iso(meeting.start),
                "end": iso(meeting.end),
                "location": self.location(meeting),
                # Gemeinsame Sitzung (Issue #317): federführendes Gremium zuerst, dann die weiteren Gremien
                "organization": [
                    self.uris.obj("organization", org_id) for org_id in meeting.participating_organization_ids
                ],
                # Ergebnisprotokoll: öffentliche Fassung der Niederschrift (Issue #318)
                "resultsProtocol": self.file(protocol_file) if protocol_file is not None else None,
                "auxiliaryFile": [self.file(f) for f in files],
                # OParl 1.1 bettet Tagesordnungspunkte in Meeting ein (nur der öffentliche Teil)
                "agendaItem": [self.agenda_item(item) for item in items],
                **_timestamps(meeting),
                # Abgekündigt: Der Ort steht als Location-Objekt in ``location`` (docs/SESSION_OPARL_API.md)
                "mandari:locationName": meeting.location or None,
                "mandari:locationRoom": meeting.room or None,
                "mandari:locationAddress": ", ".join(
                    part
                    for part in (meeting.street_address, f"{meeting.postal_code} {meeting.locality}".strip())
                    if part
                )
                or None,
                # Sitzungsformat (Issue #138): nie der Zugangsweg der Zugeschalteten
                **self._format_extension(meeting),
            }
        )

    def location(self, meeting: Any) -> Objekt | None:
        """
        Sitzungsort als OParl-Location (eingebettet in ``Meeting.location``), ``None`` ohne Ortsangabe.

        Session führt den Ort an der Sitzung. Das Location-Objekt gehört deshalb zur Sitzung und trägt
        deren Kennung (``…/location/<Kennung der Sitzung>/``). Abgebildet wird es nur für öffentliche
        Sitzungen.
        """
        _require_public("location", meeting, meeting.is_public)
        fields = {
            "description": meeting.location,
            "room": meeting.room,
            "streetAddress": meeting.street_address,
            "postalCode": meeting.postal_code,
            "locality": meeting.locality,
        }
        if not any(fields.values()):
            return None
        return clean(
            {
                "id": self.uris.obj("location", meeting.id),
                "type": schema_type("location"),
                **fields,
                "bodies": [self.uris.body()],
                "meetings": [self.uris.obj("meeting", meeting.id)],
                **_timestamps(meeting),
            }
        )

    def _format_extension(self, meeting: Any) -> Objekt:
        """
        Sitzungsformat und Hinweis für die Öffentlichkeit (Übertragung, Anmeldung) als Erweiterung.

        Präsenzsitzungen ohne Übertragung bleiben unverändert. Der Zugangsweg für zugeschaltete
        Mitglieder wird nie abgebildet.
        """
        if meeting.format == meeting.FORMAT_PRESENCE and not (meeting.public_access_url or meeting.public_access_note):
            return {}
        info = self.source.meeting_format(meeting)
        registration = info.public_registration_required and info.format == "digital"
        public = clean(
            {
                "url": info.public_url or None,
                "note": info.public_note or None,
                "hint": info.public_hint or None,
                "registrationRequired": True if registration else None,
                "registrationDays": info.public_registration_days if registration else None,
            }
        )
        return {
            "mandari:meetingFormat": info.format,
            "mandari:meetingFormatLabel": info.label,
            "mandari:publicAccess": public or None,
        }

    def agenda_item(self, item: Any) -> Objekt:
        # Nur der öffentliche Teil öffentlicher Sitzungen
        _require_public("agendaitem", item, item.is_public and item.meeting.is_public)
        consultation = self._visible_consultation(item)
        files = [f for f in item.files.all() if f.is_public]
        return clean(
            {
                "id": self.uris.obj("agendaitem", item.id),
                "type": schema_type("agendaitem"),
                "meeting": self.uris.obj("meeting", item.meeting_id),
                "number": item.number,
                "order": item.order,
                "name": item.name,
                "public": True,  # nichtöffentliche TOPs werden nie abgebildet
                "consultation": self.uris.obj("consultation", consultation.id) if consultation else None,
                "result": item.get_vote_result_display() if item.vote_result != "pending" else None,
                # Nur der öffentliche Beschlusstext – das verschlüsselte Feld nie
                "resolutionText": item.resolution_text or None,
                "auxiliaryFile": [self.file(f) for f in files],
                **_timestamps(item),
                "mandari:resolutionNumber": item.resolution_number or None,
                "mandari:vote": self._vote(item),
                "mandari:rollCall": self._roll_call(item),
            }
        )

    def _visible_consultation(self, item: Any) -> Any:
        """Öffentlich sichtbare Beratungsstation eines TOP (oder ``None``)."""
        try:
            consultation = item.consultation
        except ObjectDoesNotExist:
            return None
        if consultation is None or not self.source.is_published(consultation.paper):
            return None
        return consultation

    @staticmethod
    def _vote(item: Any) -> Objekt | None:
        """Abstimmungsergebnis als Summen (Issue #41). Nie für offene TOPs ohne Ergebnis."""
        if item.vote_result == "pending":
            return None
        return clean(
            {
                "method": item.voting_method,
                "methodLabel": item.get_voting_method_display(),
                "result": item.vote_result,
                "resultLabel": item.get_vote_result_display(),
                "yes": item.votes_yes,
                "no": item.votes_no,
                "abstain": item.votes_abstain,
            }
        )

    @staticmethod
    def _roll_call(item: Any) -> list[Objekt] | None:
        """
        Einzelstimmen ausschließlich bei namentlicher Abstimmung (Issue #41).

        Offen erfasste, geheime oder nur summierte Abstimmungen liefern nie Namen; Befangenheit
        (Mitwirkungsverbot) wird wie in der Niederschrift ausgewiesen.
        """
        if item.voting_method != "roll_call" or item.vote_result == "pending":
            return None
        entries = [
            {"name": vote.person.display_name, "vote": vote.vote, "voteLabel": vote.get_vote_display()}
            for vote in item.votes.all()
            if vote.vote in _VOTES
        ]
        entries.sort(key=lambda e: (e["vote"] != "yes", e["vote"] != "no", e["vote"] != "abstain", e["name"]))
        return entries or None

    # -- Vorlage, Beratung, Datei -----------------------------------------------------------------

    def paper(self, paper: Any) -> Objekt:
        _require_public("paper", paper, self.source.is_published(paper))
        files = sorted((f for f in paper.files.all() if f.is_public), key=lambda f: f.created_at)
        main_file = files[0] if files else None
        consultations = sorted(paper.consultations.all(), key=lambda c: (c.order, c.created_at))
        return clean(
            {
                "id": self.uris.obj("paper", paper.id),
                "type": schema_type("paper"),
                "body": self.uris.body(),
                "name": paper.name,
                "reference": paper.reference,
                "date": iso_date(paper.date),
                "paperType": paper.get_paper_type_display(),
                "mainFile": self.file(main_file) if main_file else None,
                "auxiliaryFile": [self.file(f) for f in files[1:]],
                # OParl 1.1 bettet Consultations in Paper ein
                "consultation": [self.consultation(c) for c in consultations],
                "originatorPerson": [self.uris.obj("person", paper.originator_person_id)]
                if paper.originator_person_id
                else None,
                "originatorOrganization": [self.uris.obj("organization", paper.originator_organization_id)]
                if paper.originator_organization_id
                else None,
                "underDirectionOf": [self.uris.obj("organization", paper.main_organization_id)]
                if paper.main_organization_id
                else None,
                **_timestamps(paper),
            }
        )

    def consultation(self, consultation: Any) -> Objekt:
        # Eine Beratung ist so öffentlich wie ihre Vorlage
        _require_public("consultation", consultation, self.source.is_published(consultation.paper))
        meeting = consultation.meeting
        item = consultation.agenda_item
        # Verweise auf nichtöffentliche Sitzungen und TOPs entfallen
        meeting_visible = meeting is not None and meeting.is_public
        item_visible = item is not None and item.is_public and item.meeting.is_public
        # Eine Station in einer nichtöffentlichen Sitzung bzw. auf einem nichtöffentlichen TOP nennt auch
        # Gremium und Rolle nicht – öffentlich bleibt nur, dass die Vorlage dort beraten wird
        non_public = (meeting is not None and not meeting_visible) or (item is not None and not item_visible)
        return clean(
            {
                "id": self.uris.obj("consultation", consultation.id),
                "type": schema_type("consultation"),
                "paper": self.uris.obj("paper", consultation.paper_id),
                "organization": None if non_public else [self.uris.obj("organization", consultation.organization_id)],
                "meeting": self.uris.obj("meeting", meeting.id) if meeting_visible else None,
                "agendaItem": self.uris.obj("agendaitem", item.id) if item_visible else None,
                "authoritative": None if non_public else consultation.authoritative,
                "role": None if non_public else consultation.get_role_display(),
                **_timestamps(consultation),
            }
        )

    def file(self, file_obj: Any, *, include_text: bool = False) -> Objekt:
        download = self.uris.file_download(file_obj.id)
        file_name = self.source.download_name(file_obj)
        refs: Objekt = {}
        if file_obj.paper_id and self.source.is_published(file_obj.paper):
            refs["paper"] = [self.uris.obj("paper", file_obj.paper_id)]
        if file_obj.meeting_id and file_obj.meeting.is_public:
            refs["meeting"] = [self.uris.obj("meeting", file_obj.meeting_id)]
        if file_obj.agenda_item_id and file_obj.agenda_item.is_public and file_obj.agenda_item.meeting.is_public:
            refs["agendaItem"] = [self.uris.obj("agendaitem", file_obj.agenda_item_id)]
        # Öffentlich ist eine Datei nur mit mindestens einem öffentlichen Bezugsobjekt (Vorlage, Sitzung, TOP)
        _require_public("file", file_obj, bool(file_obj.is_public and refs))
        return clean(
            {
                "id": self.uris.obj("file", file_obj.id),
                "type": schema_type("file"),
                "name": file_obj.name,
                # Anzeigename statt Speichername: gleiche Inhalte teilen sich eine Datei (Issue #226)
                "fileName": file_name if file_obj.file else None,
                # Der Typ, mit dem der Download ausgeliefert wird (aus der Endung, nicht aus dem Upload)
                "mimeType": self.source.mime_type(file_name),
                "size": file_obj.size,
                # OParl 1.1: Datum (yyyy-mm-dd), kein Zeitpunkt
                "date": iso_day(file_obj.created_at),
                "accessUrl": download,
                "downloadUrl": f"{download}?download=1",
                "text": (file_obj.text_content or None) if include_text else None,
                **refs,
                **_timestamps(file_obj),
                "mandari:version": file_obj.version,
            }
        )

    def file_with_text(self, file_obj: Any) -> Objekt:
        """Datei samt erkanntem Text (Objekt-Endpunkt; in Listen und Einbettungen entfällt der Text)."""
        return self.file(file_obj, include_text=True)

    # -- Gelöschtes --------------------------------------------------------------------------------

    def tombstone(self, kind: str, object_id: Any, created: datetime | None, deleted: datetime | None) -> Objekt:
        """Gekürztes Objekt für gelöschte oder nicht mehr öffentliche Einträge (OParl 1.1 §2.8)."""
        return tombstone(self.uris.obj(kind, object_id), kind, created, deleted)

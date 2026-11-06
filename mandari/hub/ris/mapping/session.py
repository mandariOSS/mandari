# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abbildung der Objekte von mandari Session auf das kanonische RIS-Modell (OParl 1.1).

Die einzige Stelle, die Session-Objekte in das Modell übersetzt: Die Session-OParl-Schnittstelle
(``apps/session/api/oparl.py``) gibt genau diese Abbildung aus, weitere Abnehmer der Drehscheibe
bauen darauf auf. Eigene Erweiterungen tragen den Namensraum ``mandari:``.

Grundsätze:

- **Nur Öffentliches – außer in der nichtöffentlichen Ausgabe.** Abgebildet werden die öffentlichen Felder
  öffentlicher Objekte. Nur mit einer ``SessionSicht`` (Profil Nichtöffentliches von kommParl, Issue #565) bildet die
  Abbildung zusätzlich ab, was die Sicht zeigt; welche Person oder welcher Dienst was sieht, entscheidet das
  Fachmodul. Ohne Sicht ist jede Ausgabe unverändert.
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

**Nichtöffentliche Ausgabe** (``SessionSicht``): eigene Adressen unter ``base`` (die Adresse der nichtöffentlichen
Ausgabe), alle Verweise zeigen dorthin. Nichtöffentliche Objekte tragen ``kommparl:nonPublic``, Objekte, die auch
öffentlich abrufbar sind, in ``kommparl:publicId`` ihre öffentliche Adresse (``SessionSicht.public``). Nichtöffentliche
Tagesordnungspunkte stehen mit ``public: false`` in ``agendaItem`` ihrer Sitzung, nichtöffentliche Anlagen in
``auxiliaryFile``; der nichtöffentliche Teil eines Beschlusstexts steht in ``kommparl:nonPublicResolutionText``.

``VERSION`` steigt, wenn sich für dasselbe Session-Objekt die Abbildung ändert.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Any, Final
from uuid import UUID

from django.core.exceptions import ObjectDoesNotExist
from mandari_oparl.ids import canonical_id, canonical_uri

from hub.ris.canonical import Objekt, as_list, clean, file_text, iso, iso_date, iso_day, schema_type, tombstone

#: Version der Abbildung (2: Umsetzungsstand und Genehmigung der Niederschrift, Issue #525;
#: 3: ``kommparl:changes``/``kommparl:snapshot`` am Body und ``kommparl:conformsTo`` am System, Issue #1216;
#: 4: Adressen des Profils Einreichung an Body und Sitzung, wenn die Ausgabe es anbietet, Issue #1228;
#: 5: nichtöffentliche Ausgabe mit ``SessionSicht`` und ``kommparl:nonPublicSystem`` am System, Issue #565)
VERSION: Final = 5

#: Einbettungen dieser Abbildung: eingebetteter Typ -> Typen, die ihn intern ausgeben (OParl 1.1, Kapitel „Schema“).
#: Der Änderungsfeed nennt danach auch das einbettende Objekt (``hub.api.changes``, Issue #1229); ein Test prüft die
#: Tabelle an den abgebildeten Objekten. Kommune, Gremium und Vorlage geben keinen Ort aus; der Sitzungsort gehört zur
#: Sitzung und trägt ihre Kennung (``SessionMapping.location``).
EMBEDDINGS: Final[dict[str, frozenset[str]]] = {
    "legislativeterm": frozenset({"body"}),
    "membership": frozenset({"person"}),
    "agendaitem": frozenset({"meeting"}),
    "consultation": frozenset({"paper"}),
    "file": frozenset({"meeting", "agendaitem", "paper"}),
    "location": frozenset({"meeting"}),
}

#: Genehmigungsweg der Niederschrift (``mandari:protocolApproval``, wie ``ris.protocol.approved``)
FOLLOW_UP: Final = "follow_up"
DIRECT: Final = "direct"

#: Art des Gremiums in Session (``SessionOrganization.organization_type``) -> ``organizationType``.
#: OParl 1.1 kennt sieben Werte; die feinere Art (Ausschuss, Rat, Beirat …) steht in ``classification``.
ORGANIZATION_TYPES: Final[dict[str, str]] = {
    "committee": "Gremium",
    "council": "Gremium",
    "advisory": "Gremium",
    "commission": "Gremium",
    "faction": "Fraktion",
    # Gruppe nach § 57 NKomVG: Zusammenschluss wie eine Fraktion (Issue #757)
    "group": "Fraktion",
    "local_council": "Gremium",
    "youth_council": "Gremium",
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
    - ``implementation_public(item)``: Ist der Umsetzungsstand des Beschlusses öffentlich (Freigabe der
      Verwaltung, wie die Beschlussseiten im Bürgerportal)? Ohne Angabe nie.
    """

    is_published: Callable[[Any], bool]
    download_name: Callable[[Any], str]
    mime_type: Callable[[str], str]
    meeting_format: Callable[[Any], Any]
    results_protocol: Callable[[Any], Any]
    implementation_public: Callable[[Any], bool] | None = None


@dataclass(frozen=True)
class SessionSicht:
    """
    Nichtöffentliche Ausgabe (Profil Nichtöffentliches, Issue #565): was über das Öffentliche hinaus abgebildet wird.

    Wer was sieht, legt das Fachmodul fest (Rechte der Person mit Geltungsbereich, Mitwirkungsverbot, Bindung eines
    Dienstes); die Abbildung fragt nur. Der Aufrufer reicht nur Objekte herein, die die Sicht zeigt (Querysets); die
    Abbildung verlässt sich darauf nicht und prüft eingebettete Objekte und Verweise selbst.

    - ``meeting(sitzung)``: Sitzung vollständig, auch wenn sie nichtöffentlich ist (Ort, Tagesordnung, Anlagen)
    - ``agenda_item(top)``: Tagesordnungspunkt, auch nichtöffentlich, samt nichtöffentlichem Beschlusstext
    - ``paper(vorlage)``: Vorlage, auch nichtöffentlich
    - ``file(anlage)``: Anlage, auch nichtöffentlich; ein sichtbares Bezugsobjekt braucht sie zusätzlich
    - ``nonpublic_resolution_text(top)``: nichtöffentlicher Teil des Beschlusstexts (leer: keiner)
    - ``public``: Adressen der öffentlichen Ausgabe desselben Mandanten (``kommparl:publicId``); ``None``, wenn der
      Mandant keine öffentliche Ausgabe hat
    """

    meeting: Callable[[Any], bool]
    agenda_item: Callable[[Any], bool]
    paper: Callable[[Any], bool]
    file: Callable[[Any], bool]
    nonpublic_resolution_text: Callable[[Any], str]
    public: SessionUris | None


class SessionUris:
    """
    Adressen und kanonische Kennungen der Objekte eines Mandanten.

    ``base`` ist die öffentliche Adresse seiner OParl-Schnittstelle und endet mit ``/``. Sie baut auf
    der aktuellen Adresse der Installation auf (``SITE_URL``), nicht auf dem Host einer Anfrage.

    ``id_base`` ist dieselbe Schnittstelle auf der festgeschriebenen Basis der Kennungen (Issue #733,
    ``apps.common.identifiers``); ohne Angabe gleich ``base``. Die kanonische Kennung eines Objekts ist
    ``uuid5`` über seine Adresse auf dieser Basis (``canonical_id``, ADR
    ``docs/adr/20260929-kanonisches-modell.md``): Adressen folgen der Domain, Kennungen nicht. Als Funktion
    übergeben, wird ``id_base`` erst beim ersten Bedarf ermittelt – Listen und Objekte brauchen sie nicht.
    """

    def __init__(self, base: str, id_base: str | Callable[[], str] | None = None) -> None:
        self.base = base if base.endswith("/") else f"{base}/"
        self._id_base = id_base

    @cached_property
    def id_base(self) -> str:
        """Schnittstelle auf der festgeschriebenen Basis der Kennungen, endet mit ``/``."""
        value = self._id_base() if callable(self._id_base) else self._id_base
        if not value:
            return self.base
        return value if value.endswith("/") else f"{value}/"

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
        """Änderungsfeed des Mandanten in der älteren Form (``mandari:changes``, abgekündigt)."""
        return f"{self.base}body/changes/"

    def snapshot(self) -> str:
        """Snapshot des Mandanten in der älteren Form (``mandari:snapshot``, abgekündigt)."""
        return f"{self.base}body/snapshot/"

    def kommparl_changes(self) -> str:
        """Änderungsfeed des Mandanten nach dem Profil von kommParl (``kommparl:changes``)."""
        return f"{self.base}body/kommparl/changes/"

    def kommparl_snapshot(self) -> str:
        """Snapshot des Mandanten nach dem Profil von kommParl (``kommparl:snapshot``)."""
        return f"{self.base}body/kommparl/snapshot/"

    # -- Profil Einreichung (Issue #1228) --------------------------------------------------------------

    def submissions(self) -> str:
        """Einreichungen des Mandanten (``kommparl:submission``)."""
        return f"{self.base}body/kommparl/submissions/"

    def submission(self, pk: Any) -> str:
        return f"{self.submissions()}{pk}/"

    def submission_withdraw(self, pk: Any) -> str:
        return f"{self.submission(pk)}withdraw/"

    def submission_uploads(self) -> str:
        """Anlagen hochladen (``kommparl:submissionUpload``)."""
        return f"{self.base}body/kommparl/uploads/"

    def submission_upload(self, pk: Any) -> str:
        return f"{self.submission_uploads()}{pk}/"

    def submission_rules(self) -> str:
        """Einreichungsregeln (``kommparl:submissionRules``)."""
        return f"{self.base}body/kommparl/submission-rules/"

    def attendance_response(self, meeting_pk: Any) -> str:
        """Rückmeldung zur Ladung (``kommparl:attendanceResponse``)."""
        return f"{self.obj('meeting', meeting_pk)}kommparl/attendance/"

    def invitation_receipt(self, meeting_pk: Any) -> str:
        """Empfangsbestätigung der Ladung (``kommparl:invitationReceipt``)."""
        return f"{self.obj('meeting', meeting_pk)}kommparl/invitation-receipt/"


def _timestamps(obj: Any) -> Objekt:
    return {"created": iso(obj.created_at), "modified": iso(obj.updated_at)}


class SessionMapping:
    """Abbildung der Objekte eines Mandanten; je Objekttyp eine Methode, Ergebnis ist ein OParl-Objekt."""

    def __init__(
        self,
        tenant: Any,
        base: str,
        source: SessionSource,
        *,
        changes: bool = False,
        id_base: str | Callable[[], str] | None = None,
        conforms_to: Sequence[str] = (),
        submission: bool = False,
        nonpublic_system: str | None = None,
        sicht: SessionSicht | None = None,
    ) -> None:
        self.tenant = tenant
        #: Adressen aus ``base``, kanonische Kennungen aus ``id_base`` (``SessionUris``)
        self.uris = SessionUris(base, id_base)
        self.source = source
        #: Die Ausgabe bietet Änderungsfeed und Snapshot an; der Body nennt dann deren Adressen
        self.changes = changes
        #: Die Ausgabe bietet das Profil Einreichung an; Body und Sitzungen nennen dann dessen Adressen
        self.submission = submission
        #: Kennungen aus kommParl, die die Ausgabe vollständig erfüllt (``System``, ``kommparl:conformsTo``)
        self.conforms_to = list(conforms_to)
        #: Öffentliche Ausgabe: Adresse der nichtöffentlichen (``kommparl:nonPublicSystem``), wenn sie angeboten wird
        self.nonpublic_system = nonpublic_system
        #: Nichtöffentliche Ausgabe (#565): was über das Öffentliche hinaus abgebildet wird; ``None``: nur Öffentliches
        self.sicht = sicht
        #: Nur in der Sicht: Kennungen nichtöffentlicher Objekte, auf die ein nicht gekennzeichnetes, öffentliches Objekt
        #: dieser Abbildung verweist (etwa ein öffentlicher TOP auf die Beratung einer nichtöffentlichen Vorlage). Das
        #: Prüfprotokoll nennt sie zusätzlich zu den gekennzeichneten Objekten.
        self.nonpublic_refs: list[str] = []

    # -- Sichtbarkeit: öffentlich oder in der nichtöffentlichen Sicht ----------------------------------------------

    def _meeting_full(self, meeting: Any) -> bool:
        """Sitzung vollständig abbilden (öffentlich oder in der Sicht), nicht nur den Termin?"""
        return bool(meeting.is_public) or (self.sicht is not None and self.sicht.meeting(meeting))

    @staticmethod
    def _item_public(item: Any) -> bool:
        return bool(item.is_public and item.meeting.is_public)

    def _item_visible(self, item: Any) -> bool:
        return self._item_public(item) or (self.sicht is not None and self.sicht.agenda_item(item))

    def _paper_visible(self, paper: Any) -> bool:
        return bool(self.source.is_published(paper)) or (self.sicht is not None and self.sicht.paper(paper))

    def _file_flag_visible(self, file_obj: Any) -> bool:
        """Anlage an einem abgebildeten Objekt zeigen? Öffentlich oder in der Sicht (Bezugsobjekt prüft ``file``)."""
        return bool(file_obj.is_public) or (self.sicht is not None and self.sicht.file(file_obj))

    def _public_url(self, kind: str, pk: Any = None) -> str | None:
        """Adresse desselben Objekts in der öffentlichen Ausgabe (``kommparl:publicId``), nur in der Sicht."""
        public = self.sicht.public if self.sicht is not None else None
        if public is None:
            return None
        if kind == "system":
            return public.system()
        if kind == "body":
            return public.body()
        return public.obj(kind, pk)

    def _nonpublic_ref(self, url: str) -> str:
        """Verweis auf ein nichtöffentliches Objekt für das Prüfprotokoll vormerken (:attr:`nonpublic_refs`)."""
        if self.sicht is not None:
            self.nonpublic_refs.append(url)
        return url

    def _marks(self, public_url: str | None, *, non_public: bool) -> Objekt:
        """
        Kennzeichen der nichtöffentlichen Ausgabe (#565): ``kommparl:nonPublic`` für Nichtöffentliches,
        ``kommparl:publicId`` für Objekte, die auch öffentlich abrufbar sind. Ohne Sicht nichts.
        """
        if self.sicht is None:
            return {}
        return clean({"kommparl:nonPublic": True if non_public else None, "kommparl:publicId": public_url})

    # -- System, Körperschaft, Wahlperiode -------------------------------------------------------

    def system(self) -> Objekt:
        tenant = self.tenant
        return clean(
            {
                "id": self.uris.system(),
                "type": schema_type("system"),
                "oparlVersion": "https://schema.oparl.org/1.1/",
                "kommparl:conformsTo": self.conforms_to,
                # Profil Nichtöffentliches (#565): der einzige Verweis der öffentlichen auf die nichtöffentliche Ausgabe
                "kommparl:nonPublicSystem": self.nonpublic_system,
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
                **self._marks(self._public_url("system"), non_public=False),
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
                # Änderungsfeed und Snapshot nach den Profilen von kommParl; die älteren Formen sind abgekündigt
                "kommparl:changes": self.uris.kommparl_changes() if self.changes else None,
                "kommparl:snapshot": self.uris.kommparl_snapshot() if self.changes else None,
                "mandari:changes": self.uris.changes() if self.changes else None,
                "mandari:snapshot": self.uris.snapshot() if self.changes else None,
                # Profil Einreichung (Issue #1228): Adressen, die Endpunkte verlangen eine Anmeldung
                "kommparl:submission": self.uris.submissions() if self.submission else None,
                "kommparl:submissionUpload": self.uris.submission_uploads() if self.submission else None,
                "kommparl:submissionRules": self.uris.submission_rules() if self.submission else None,
                **self._marks(self._public_url("body"), non_public=False),
            }
        )
        # Pflichtfeld in OParl 1.1: auch ohne Wahlperiode vorhanden (leere Liste)
        body["legislativeTerm"] = [self.legislative_term(term) for term in tenant.legislative_terms.all()]
        return body

    def _public_mark(self, kind: str, pk: Any) -> Objekt:
        """``kommparl:publicId`` eines Objekts, das immer öffentlich ist (nur in der Sicht)."""
        return self._marks(self._public_url(kind, pk), non_public=False)

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
                **self._public_mark("legislativeterm", term.id),
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
                **self._public_mark("organization", org.id),
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
                **self._public_mark("person", person.id),
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
                **self._public_mark("membership", membership.id),
            }
        )

    # -- Sitzung, Ort, Tagesordnung ------------------------------------------------------------------

    def _meeting_public_url(self, meeting: Any) -> str | None:
        """Öffentliche Adresse der Sitzung (auch nur mit Termin), nur in der Sicht."""
        if not (meeting.is_public or getattr(meeting, "date_public", False)):
            return None
        return self._public_url("meeting", meeting.id)

    def meeting(self, meeting: Any) -> Objekt:
        if not self._meeting_full(meeting):
            # Termin einer nichtöffentlichen Sitzung (Issue #757): nur Termin und Gremien, nie Ort oder Inhalte
            _require_public("meeting", meeting, bool(getattr(meeting, "date_public", False)))
            return self._meeting_date(meeting)
        protocol_file = self.source.results_protocol(meeting)
        protocol_file_id = protocol_file.pk if protocol_file is not None else None
        files = [f for f in meeting.files.all() if self._file_flag_visible(f) and f.pk != protocol_file_id]
        items = [i for i in meeting.agenda_items.all() if self._item_visible(i)]
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
                # Genehmigung dieser Fassung (Issue #525): nur, wenn sie veröffentlicht ist
                "mandari:protocolApproval": self._protocol_approval(meeting) if protocol_file is not None else None,
                "auxiliaryFile": [self.file(f) for f in files],
                # OParl 1.1 bettet Tagesordnungspunkte in Meeting ein (nur der öffentliche Teil, in der Sicht mehr)
                "agendaItem": [self.agenda_item(item) for item in items],
                **_timestamps(meeting),
                **self._marks(self._meeting_public_url(meeting), non_public=not meeting.is_public),
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
                **self._submission_extension(meeting),
            }
        )

    def _submission_extension(self, meeting: Any) -> Objekt:
        """Rückmeldung zur Ladung und Empfangsbestätigung (Profil Einreichung), wenn die Ausgabe es anbietet."""
        if not self.submission:
            return {}
        return {
            "kommparl:attendanceResponse": self.uris.attendance_response(meeting.id),
            "kommparl:invitationReceipt": self.uris.invitation_receipt(meeting.id),
        }

    def _meeting_date(self, meeting: Any) -> Objekt:
        """Nichtöffentliche Sitzung mit veröffentlichtem Termin: ohne Ort, Tagesordnung, Anlagen und Format."""
        return clean(
            {
                "id": self.uris.obj("meeting", meeting.id),
                "type": schema_type("meeting"),
                "name": meeting.name,
                "meetingState": meeting.get_meeting_state_display(),
                "cancelled": meeting.cancelled,
                "start": iso(meeting.start),
                "end": iso(meeting.end),
                "organization": [
                    self.uris.obj("organization", org_id) for org_id in meeting.participating_organization_ids
                ],
                "agendaItem": [],
                **_timestamps(meeting),
                "mandari:nonPublic": True,
                **self._marks(self._meeting_public_url(meeting), non_public=True),
                # Geladene melden sich auch zu nichtöffentlichen Sitzungen zurück (Profil Einreichung)
                **self._submission_extension(meeting),
            }
        )

    def location(self, meeting: Any) -> Objekt | None:
        """
        Sitzungsort als OParl-Location (eingebettet in ``Meeting.location``), ``None`` ohne Ortsangabe.

        Session führt den Ort an der Sitzung. Das Location-Objekt gehört deshalb zur Sitzung und trägt
        deren Kennung (``…/location/<Kennung der Sitzung>/``). Abgebildet wird es nur für öffentliche
        Sitzungen.
        """
        _require_public("location", meeting, self._meeting_full(meeting))
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
                **self._marks(
                    self._public_url("location", meeting.id) if meeting.is_public else None,
                    non_public=not meeting.is_public,
                ),
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
        # Nur der öffentliche Teil öffentlicher Sitzungen, in der nichtöffentlichen Sicht auch mehr
        _require_public("agendaitem", item, self._item_visible(item))
        public = self._item_public(item)
        consultation = self._visible_consultation(item)
        consultation_url = self.uris.obj("consultation", consultation.id) if consultation else None
        if consultation_url and not self.source.is_published(consultation.paper):
            self._nonpublic_ref(consultation_url)
        files = [f for f in item.files.all() if self._file_flag_visible(f)]
        return clean(
            {
                "id": self.uris.obj("agendaitem", item.id),
                "type": schema_type("agendaitem"),
                "meeting": self.uris.obj("meeting", item.meeting_id),
                "number": item.number,
                "order": item.order,
                "name": item.name,
                # Öffentlich abgebildet werden nur öffentliche TOPs; in der Sicht auch nichtöffentliche (false)
                "public": public,
                "consultation": consultation_url,
                "result": item.get_vote_result_display() if item.vote_result != "pending" else None,
                # Nur der öffentliche Beschlusstext – das verschlüsselte Feld nie
                "resolutionText": item.resolution_text or None,
                "auxiliaryFile": [self.file(f) for f in files],
                # Nichtöffentlicher Teil des Beschlusstexts, nur in der Sicht und nur mit Zugang zum TOP (#565)
                "kommparl:nonPublicResolutionText": (
                    self.sicht.nonpublic_resolution_text(item) or None
                    if self.sicht is not None and self.sicht.agenda_item(item)
                    else None
                ),
                **_timestamps(item),
                **self._marks(
                    self._public_url("agendaitem", item.id) if public else None,
                    non_public=not public,
                ),
                "mandari:resolutionNumber": item.resolution_number or None,
                "mandari:vote": self._vote(item),
                "mandari:rollCall": self._roll_call(item),
                "mandari:implementation": self._implementation(item),
            }
        )

    def _implementation(self, item: Any) -> Objekt | None:
        """
        Umsetzungsstand des Beschlusses (Beschlusskontrolle, Issue #525): nur mit Freigabe der Verwaltung und nur die
        öffentliche Statusmeldung; Erledigungsvermerk, Zuständigkeit und Bearbeiter bleiben intern.
        """
        rule = self.source.implementation_public
        if rule is None or not item.implementation_status or not rule(item):
            return None
        return clean(
            {
                "status": item.implementation_status,
                "statusLabel": item.get_implementation_status_display(),
                "deadline": iso_date(item.implementation_deadline),
                "note": item.implementation_public_note or None,
                "modified": iso(item.implementation_updated_at),
            }
        )

    def _protocol_approval(self, meeting: Any) -> Objekt | None:
        """
        Genehmigung der veröffentlichten Niederschrift (Issue #525): Weg, Tag und die genehmigende Sitzung, diese nur,
        wenn sie selbst öffentlich ist.

        Den Weg hält Session an der Niederschrift fest (``approval_mode``, gesetzt beim Genehmigen bzw. beim
        Veröffentlichen ohne Genehmigungsschritt); er übersteht Rücknahme und erneute Veröffentlichung. Ohne Angabe
        (ältere bzw. ohne den Workflow angelegte Niederschriften) gilt: Fielen Genehmigung und Veröffentlichung in
        denselben Schritt, war es eine Veröffentlichung ohne Genehmigungsschritt.
        """
        protocol = meeting.protocol
        if protocol.approved_at is None:
            return None
        mode = protocol.approval_mode or (
            DIRECT if protocol.published_at is not None and protocol.approved_at == protocol.published_at else FOLLOW_UP
        )
        if mode == DIRECT:
            return {"mode": DIRECT, "date": iso_day(protocol.approved_at)}
        approving = protocol.approval_meeting if protocol.approval_meeting_id else None
        if approving is None or not self.source.is_published(approving):
            return {"mode": FOLLOW_UP, "date": iso_day(protocol.approved_at)}
        return clean(
            {
                "mode": FOLLOW_UP,
                "date": iso_day(approving.start or protocol.approved_at),
                "meeting": self.uris.obj("meeting", approving.pk),
            }
        )

    def _visible_consultation(self, item: Any) -> Any:
        """Öffentlich sichtbare Beratungsstation eines TOP (oder ``None``)."""
        try:
            consultation = item.consultation
        except ObjectDoesNotExist:
            return None
        if consultation is None or not self._paper_visible(consultation.paper):
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
        _require_public("paper", paper, self._paper_visible(paper))
        published = bool(self.source.is_published(paper))
        files = sorted((f for f in paper.files.all() if self._file_flag_visible(f)), key=lambda f: f.created_at)
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
                **self._marks(
                    self._public_url("paper", paper.id) if published else None,
                    non_public=not published,
                ),
            }
        )

    def consultation(self, consultation: Any) -> Objekt:
        # Eine Beratung ist so öffentlich wie ihre Vorlage
        _require_public("consultation", consultation, self._paper_visible(consultation.paper))
        published = bool(self.source.is_published(consultation.paper))
        meeting = consultation.meeting
        item = consultation.agenda_item
        # Verweise auf nichtöffentliche Sitzungen und TOPs entfallen (in der Sicht: auf solche, die sie nicht zeigt)
        meeting_visible = meeting is not None and self._meeting_full(meeting)
        item_visible = item is not None and self._item_visible(item)
        # Eine Station in einer nichtöffentlichen Sitzung bzw. auf einem nichtöffentlichen TOP nennt auch
        # Gremium und Rolle nicht – öffentlich bleibt nur, dass die Vorlage dort beraten wird
        non_public = (meeting is not None and not meeting_visible) or (item is not None and not item_visible)
        # In der Sicht (#565): Nennt die Beratung eine nichtöffentliche Sitzung oder einen nichtöffentlichen TOP, die die
        # Sicht zeigt, oder Gremium und Rolle einer solchen Station, ist sie nichtöffentlich (kommparl:nonPublic), auch
        # wenn die Vorlage veröffentlicht ist. Ohne solche Angaben bleibt sie wie in der öffentlichen Ausgabe.
        station_non_public = (meeting_visible and not meeting.is_public) or (
            item_visible and not self._item_public(item)
        )
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
                **self._marks(
                    self._public_url("consultation", consultation.id) if published else None,
                    non_public=not published or station_non_public,
                ),
            }
        )

    def file(self, file_obj: Any, *, include_text: bool = False) -> Objekt:
        download = self.uris.file_download(file_obj.id)
        file_name = self.source.download_name(file_obj)
        refs: Objekt = {}
        if file_obj.paper_id and self._paper_visible(file_obj.paper):
            refs["paper"] = [self.uris.obj("paper", file_obj.paper_id)]
        if file_obj.meeting_id and self._meeting_full(file_obj.meeting):
            refs["meeting"] = [self.uris.obj("meeting", file_obj.meeting_id)]
        if file_obj.agenda_item_id and self._item_visible(file_obj.agenda_item):
            refs["agendaItem"] = [self.uris.obj("agendaitem", file_obj.agenda_item_id)]
        # Öffentlich ist eine Datei nur mit mindestens einem öffentlichen Bezugsobjekt (Vorlage, Sitzung, TOP); in der
        # Sicht genügt ein Bezugsobjekt, das die Sicht zeigt
        _require_public("file", file_obj, bool(self._file_flag_visible(file_obj) and refs))
        public = self.sicht is not None and self._file_public(file_obj)
        if public:
            # Öffentliche Datei, die (auch) an einem nichtöffentlichen Bezugsobjekt hängt: Verweis fürs Prüfprotokoll
            if "paper" in refs and not self.source.is_published(file_obj.paper):
                self._nonpublic_ref(refs["paper"][0])
            if "meeting" in refs and not file_obj.meeting.is_public:
                self._nonpublic_ref(refs["meeting"][0])
            if "agendaItem" in refs and not self._item_public(file_obj.agenda_item):
                self._nonpublic_ref(refs["agendaItem"][0])
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
                # Höchstens bis zur Obergrenze der Texterkennung (``with_limited_text``)
                "text": file_text(file_obj) if include_text else None,
                **refs,
                **_timestamps(file_obj),
                **self._marks(
                    self._public_url("file", file_obj.id) if public else None,
                    non_public=not public,
                ),
                "mandari:version": file_obj.version,
            }
        )

    def _file_public(self, file_obj: Any) -> bool:
        """Ist die Anlage auch öffentlich abrufbar (öffentlich mit mindestens einem öffentlichen Bezugsobjekt)?"""
        if not file_obj.is_public:
            return False
        return bool(
            (file_obj.paper_id and self.source.is_published(file_obj.paper))
            or (file_obj.meeting_id and file_obj.meeting.is_public)
            or (file_obj.agenda_item_id and self._item_public(file_obj.agenda_item))
        )

    def file_with_text(self, file_obj: Any) -> Objekt:
        """Datei samt erkanntem Text (Objekt-Endpunkt; in Listen und Einbettungen entfällt der Text)."""
        return self.file(file_obj, include_text=True)

    # -- Gelöschtes --------------------------------------------------------------------------------

    def tombstone(self, kind: str, object_id: Any, created: datetime | None, deleted: datetime | None) -> Objekt:
        """Gekürztes Objekt für gelöschte oder nicht mehr öffentliche Einträge (OParl 1.1 §2.8)."""
        return tombstone(self.uris.obj(kind, object_id), kind, created, deleted)

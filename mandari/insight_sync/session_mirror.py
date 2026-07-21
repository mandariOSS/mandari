# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lokaler, synchroner OParl-Spiegel in die Insight-Modelle (Issue #36).

Konsumiert eine OParl-1.1-Quelle (insbesondere die Session-OParl-API aus
Issue #35) als ganz normaler OParl-Client — System -> Body -> Listen mit
``links.next``-Pagination, inkrementell per ``modified_since`` und mit
Tombstone-Verarbeitung (``deleted: true`` -> ``mark_deleted``) — und
schreibt die öffentlichen Daten in die insight_core-Modelle, aus denen
das Bürgerportal gespeist wird.

Abgrenzung zum Ingestor-Daemon (``ingestor/``): Der Daemon ist der
Produktionspfad (async, PostgreSQL, Volltext-Pipeline). Dieser Spiegel
ist die leichtgewichtige, synchrone Pipeline für lokale Setups, Tests
(SQLite) und Einzel-Syncs per ``manage.py sync_session_insight`` — ohne
zusätzliche Abhängigkeiten (urllib).

Sicherheit: Es wird ausschließlich gespeichert, was die Quelle liefert —
die Session-OParl-API liefert per Konstruktion nur öffentliche Daten
(Beweis: scripts/smoke_session_oparl.py).
"""

import json
import logging
import urllib.request
from datetime import date, datetime
from urllib.parse import quote

from django.utils import timezone

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)

logger = logging.getLogger(__name__)

# type-URL-Suffix -> Insight-Modell (für Tombstones in Listen)
MODEL_BY_TYPE_SUFFIX = {
    "/Organization": OParlOrganization,
    "/Person": OParlPerson,
    "/Membership": OParlMembership,
    "/Meeting": OParlMeeting,
    "/AgendaItem": OParlAgendaItem,
    "/Paper": OParlPaper,
    "/Consultation": OParlConsultation,
    "/File": OParlFile,
    "/LegislativeTerm": OParlLegislativeTerm,
}


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _default_fetch(url):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "mandari-session-mirror/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 — Quelle ist konfiguriert
        return json.loads(response.read().decode("utf-8"))


class SessionMirror:
    """Spiegelt eine OParl-Quelle synchron in die Insight-Modelle."""

    def __init__(self, source, fetch=None):
        self.source = source
        self.fetch = fetch or _default_fetch
        self.stats = {
            "bodies": 0,
            "organizations": 0,
            "persons": 0,
            "memberships": 0,
            "meetings": 0,
            "agenda_items": 0,
            "papers": 0,
            "files": 0,
            "consultations": 0,
            "legislative_terms": 0,
            "tombstones": 0,
        }

    # ------------------------------------------------------------------
    # HTTP-Navigation
    # ------------------------------------------------------------------

    def _iter_list(self, url, modified_since=None):
        """Alle Einträge einer externen Liste (folgt links.next)."""
        if url and modified_since is not None:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}modified_since={quote(modified_since.isoformat())}"
        while url:
            page = self.fetch(url)
            yield from page.get("data", [])
            url = (page.get("links") or {}).get("next")

    # ------------------------------------------------------------------
    # Upserts
    # ------------------------------------------------------------------

    def _base_defaults(self, data):
        return {
            "oparl_created": _parse_dt(data.get("created")),
            "oparl_modified": _parse_dt(data.get("modified")),
            "raw_json": data,
            "deleted": False,
            "deleted_at": None,
        }

    def _handle_tombstone(self, data) -> bool:
        """Tombstone (deleted: true) auf den lokalen Spiegel anwenden."""
        if not data.get("deleted"):
            return False
        type_url = data.get("type", "")
        for suffix, model in MODEL_BY_TYPE_SUFFIX.items():
            if type_url.endswith(suffix):
                obj = model.objects.filter(external_id=data.get("id", "")).first()
                if obj is not None and not obj.deleted:
                    obj.mark_deleted(_parse_dt(data.get("modified")))
                    self.stats["tombstones"] += 1
                return True
        return True

    def _upsert_body(self, data):
        body, _created = OParlBody.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "source": self.source,
                "name": data.get("name") or "Unbekannt",
                "short_name": data.get("shortName"),
                "website": data.get("website"),
                "classification": data.get("classification"),
                "organization_list_url": data.get("organization"),
                "person_list_url": data.get("person"),
                "meeting_list_url": data.get("meeting"),
                "paper_list_url": data.get("paper"),
                "membership_list_url": data.get("membership"),
                **self._base_defaults(data),
            },
        )
        if not body.slug:
            from django.utils.text import slugify

            candidate = slugify(body.short_name or body.name)[:100]
            if candidate and not OParlBody.objects.exclude(pk=body.pk).filter(slug=candidate).exists():
                body.slug = candidate
                body.save(update_fields=["slug", "updated_at"])
        for term in data.get("legislativeTerm", []) or []:
            if isinstance(term, dict):
                self._upsert_legislative_term(body, term)
        self.stats["bodies"] += 1
        return body

    def _upsert_legislative_term(self, body, data):
        OParlLegislativeTerm.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "body": body,
                "name": data.get("name"),
                "start_date": _parse_date(data.get("startDate")),
                "end_date": _parse_date(data.get("endDate")),
                **self._base_defaults(data),
            },
        )
        self.stats["legislative_terms"] += 1

    def _upsert_organization(self, body, data):
        OParlOrganization.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "body": body,
                "name": data.get("name"),
                "short_name": data.get("shortName"),
                "organization_type": data.get("organizationType"),
                "classification": data.get("classification"),
                "start_date": _parse_date(data.get("startDate")),
                "end_date": _parse_date(data.get("endDate")),
                "website": data.get("website"),
                **self._base_defaults(data),
            },
        )
        self.stats["organizations"] += 1

    def _upsert_person(self, body, data):
        title = data.get("title")
        if isinstance(title, list):
            title = " ".join(str(t) for t in title if t)
        email = data.get("email")
        if isinstance(email, list):
            email = email[0] if email else None
        person, _created = OParlPerson.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "body": body,
                "name": data.get("name"),
                "family_name": data.get("familyName"),
                "given_name": data.get("givenName"),
                "title": title,
                "email": email,
                **self._base_defaults(data),
            },
        )
        self.stats["persons"] += 1
        for membership in data.get("membership", []) or []:
            if isinstance(membership, dict):
                self._upsert_membership(person, membership)
        return person

    def _upsert_membership(self, person, data):
        org_ref = data.get("organization")
        organization = OParlOrganization.objects.filter(external_id=org_ref).first() if org_ref else None
        if organization is None:
            return
        OParlMembership.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "person": person,
                "organization": organization,
                "role": data.get("role"),
                "voting_right": bool(data.get("votingRight", True)),
                "start_date": _parse_date(data.get("startDate")),
                "end_date": _parse_date(data.get("endDate")),
                **self._base_defaults(data),
            },
        )
        self.stats["memberships"] += 1

    def _upsert_file(self, body, data, paper=None, meeting=None):
        OParlFile.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "body": body,
                "paper": paper,
                "meeting": meeting,
                "name": data.get("name"),
                "file_name": data.get("fileName"),
                "mime_type": data.get("mimeType"),
                "size": data.get("size"),
                "access_url": data.get("accessUrl"),
                "download_url": data.get("downloadUrl"),
                "file_date": _parse_dt(data.get("date")),
                **self._base_defaults(data),
            },
        )
        self.stats["files"] += 1

    def _upsert_meeting(self, body, data):
        meeting, _created = OParlMeeting.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "body": body,
                "name": data.get("name"),
                "meeting_state": data.get("meetingState"),
                "cancelled": bool(data.get("cancelled", False)),
                "start": _parse_dt(data.get("start")),
                "end": _parse_dt(data.get("end")),
                "location_name": data.get("mandari:locationName"),
                "location_address": data.get("mandari:locationAddress"),
                **self._base_defaults(data),
            },
        )
        org_refs = [ref for ref in data.get("organization", []) if isinstance(ref, str)]
        if org_refs:
            meeting.organizations.set(OParlOrganization.objects.filter(external_id__in=org_refs))
        for item in data.get("agendaItem", []) or []:
            if isinstance(item, dict):
                self._upsert_agenda_item(meeting, item)
        for file_data in data.get("auxiliaryFile", []) or []:
            if isinstance(file_data, dict):
                self._upsert_file(body, file_data, meeting=meeting)
        self.stats["meetings"] += 1
        return meeting

    def _upsert_agenda_item(self, meeting, data):
        OParlAgendaItem.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "meeting": meeting,
                "number": data.get("number"),
                "order": data.get("order"),
                "name": data.get("name"),
                "public": bool(data.get("public", True)),
                "result": data.get("result"),
                "resolution_text": data.get("resolutionText"),
                **self._base_defaults(data),
            },
        )
        self.stats["agenda_items"] += 1

    def _upsert_paper(self, body, data):
        paper, _created = OParlPaper.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "body": body,
                "name": data.get("name"),
                "reference": data.get("reference"),
                "paper_type": data.get("paperType"),
                "date": _parse_date(data.get("date")),
                **self._base_defaults(data),
            },
        )
        main_file = data.get("mainFile")
        if isinstance(main_file, dict):
            self._upsert_file(body, main_file, paper=paper)
        for file_data in data.get("auxiliaryFile", []) or []:
            if isinstance(file_data, dict):
                self._upsert_file(body, file_data, paper=paper)
        for consultation in data.get("consultation", []) or []:
            if isinstance(consultation, dict):
                self._upsert_consultation(body, paper, consultation)
        self.stats["papers"] += 1
        return paper

    def _upsert_consultation(self, body, paper, data):
        meeting_ref = data.get("meeting")
        item_ref = data.get("agendaItem")
        OParlConsultation.objects.update_or_create(
            external_id=data.get("id", ""),
            defaults={
                "body": body,
                "paper": paper,
                "paper_external_id": data.get("paper"),
                "meeting_external_id": meeting_ref if isinstance(meeting_ref, str) else None,
                "agenda_item_external_id": item_ref if isinstance(item_ref, str) else None,
                "role": data.get("role"),
                "authoritative": bool(data.get("authoritative", False)),
                **self._base_defaults(data),
            },
        )
        self.stats["consultations"] += 1

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync(self, full: bool = False) -> dict:
        """
        Quelle spiegeln.

        Args:
            full: True = alles laden; sonst inkrementell ab source.last_sync
                  (modified_since, inkl. Tombstones)
        """
        started_at = timezone.now()
        modified_since = None if full else self.source.last_sync

        system = self.fetch(self.source.url)
        body_list_url = system.get("body")
        if not body_list_url:
            raise ValueError(f"System-Objekt {self.source.url} enthält keine body-Liste.")

        for body_data in self._iter_list(body_list_url):
            if body_data.get("deleted"):
                continue
            body = self._upsert_body(body_data)

            # Reihenfolge: Gremien vor Personen (Membership-Auflösung),
            # beide vor Sitzungen/Vorlagen (M2M-/Referenz-Auflösung).
            for entry in self._iter_list(body_data.get("organization"), modified_since):
                if not self._handle_tombstone(entry):
                    self._upsert_organization(body, entry)
            for entry in self._iter_list(body_data.get("person"), modified_since):
                if not self._handle_tombstone(entry):
                    self._upsert_person(body, entry)
            for entry in self._iter_list(body_data.get("meeting"), modified_since):
                if not self._handle_tombstone(entry):
                    self._upsert_meeting(body, entry)
            for entry in self._iter_list(body_data.get("paper"), modified_since):
                if not self._handle_tombstone(entry):
                    self._upsert_paper(body, entry)

            # Inkrementell: TOPs/Anlagen/Beratungen/Mitgliedschaften können
            # sich ändern, ohne dass sich das Elternobjekt ändert — deshalb
            # zusätzlich die eigenen Listen (OParl 1.1 Body-Listen) ziehen.
            # Hier kommen auch Tombstones der Ö->NÖ-Kaskaden an.
            if modified_since is not None:
                for entry in self._iter_list(body_data.get("membership"), modified_since):
                    if self._handle_tombstone(entry):
                        continue
                    person = OParlPerson.objects.filter(external_id=entry.get("person")).first()
                    if person is not None:
                        self._upsert_membership(person, entry)
                for entry in self._iter_list(body_data.get("agendaItem"), modified_since):
                    if self._handle_tombstone(entry):
                        continue
                    meeting = OParlMeeting.objects.filter(external_id=entry.get("meeting")).first()
                    if meeting is not None:
                        self._upsert_agenda_item(meeting, entry)
                for entry in self._iter_list(body_data.get("consultation"), modified_since):
                    if self._handle_tombstone(entry):
                        continue
                    paper = OParlPaper.objects.filter(external_id=entry.get("paper")).first()
                    if paper is not None:
                        self._upsert_consultation(body, paper, entry)
                for entry in self._iter_list(body_data.get("file"), modified_since):
                    if self._handle_tombstone(entry):
                        continue
                    paper_refs = entry.get("paper") or []
                    meeting_refs = entry.get("meeting") or []
                    paper = OParlPaper.objects.filter(external_id=paper_refs[0]).first() if paper_refs else None
                    meeting = OParlMeeting.objects.filter(external_id=meeting_refs[0]).first() if meeting_refs else None
                    if paper is not None or meeting is not None:
                        self._upsert_file(body, entry, paper=paper, meeting=meeting)

            body.last_sync = started_at
            body.save(update_fields=["last_sync", "updated_at"])

        self.source.last_sync = started_at
        if full:
            self.source.last_full_sync = started_at
        self.source.save(update_fields=["last_sync", "last_full_sync", "updated_at"])
        return self.stats

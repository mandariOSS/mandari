# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session meldet Abstimmung, Beschluss, Niederschrift und ihre Rücknahme an die Datendrehscheibe (Issue #535).

Geprüft an den Fachfunktionen (Ansichten und Dienste, wie die Oberfläche sie aufruft):

- Abstimmung erfassen: ``ris.voting.recorded`` (nie Einzelstimmen) und ``ris.resolution.adopted``; Vertagung ohne
  Abstimmung; Rücknahme eines Ergebnisses als ``ris.object.depublished`` der Abstimmung (``zurueckgenommen``) und
  geänderter TOP; nichtöffentliche TOPs nur nichtöffentlich;
- die Abstimmung folgt ihrem TOP: nichtöffentlich gestellt, gelöscht oder veröffentlicht;
- Beschlussnummer (``resolutionNumber``) und Beschlusskontrolle (``ris.resolution.implementation_changed`` nur nach
  der Regel der Beschlussseiten im Bürgerportal: Opt-in am Mandanten und am Beschluss, angenommen, nicht abgesetzt);
- Niederschrift: Entwurf und Prüfung intern, Genehmigung (``ris.protocol.approved``), Veröffentlichung der
  öffentlichen Fassung (``ris.protocol.published``), Erneuerung – auch nach dem Commit über ``refresh_meeting`` –,
  Rücknahme (``ris.object.depublished`` der Datei);
- vor der Freischaltung der Schnittstelle nichts Öffentliches.

Jedes Ereignis prüft ``publish()`` gegen seinen Vertrag (``EVENTS_VALIDATE_CONTRACTS``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db.models import Max
from django.test import Client
from django.utils import timezone
from mandari_oparl.ids import canonical_id

from apps.common.models import IdentifierBase
from apps.common.tests.factories import UserFactory
from apps.events.models import Event
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionProtocol,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import protocol_publication
from insight_core.models import OParlBody, OParlSource

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/nord/api/oparl/"
ALLE_RECHTE = {f.name for f in SessionRole._meta.get_fields() if f.name.startswith("can_")}


def kennung(kind: str, pk: Any) -> str:
    return str(canonical_id(f"{BASIS}{kind}/{pk}/"))


def abstimmung(top: SessionAgendaItem) -> str:
    """Kennung der Abstimmung eines TOP (Adresse des TOP mit dem Zusatz ``voting``)."""
    return str(canonical_id(f"{BASIS}agendaitem/{top.pk}/voting"))


def niederschrift(sitzung: SessionMeeting) -> str:
    """Kennung der Niederschrift einer Sitzung (Adresse der Sitzung mit dem Zusatz ``protocol``)."""
    return str(canonical_id(f"{BASIS}meeting/{sitzung.pk}/protocol"))


@dataclass
class Welt:
    tenant: SessionTenant
    rat: SessionOrganization
    user: SessionUser
    client: Client
    sitzung: SessionMeeting

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"

    def top(self, nummer: int = 1, **werte: Any) -> SessionAgendaItem:
        daten: dict[str, Any] = {
            "meeting": self.sitzung,
            "name": f"Punkt {nummer}",
            "number": str(nummer),
            "order": nummer,
        }
        daten.update(werte)
        return SessionAgendaItem.objects.create(**daten)

    def erfassen(self, top: SessionAgendaItem, **daten: Any) -> None:
        antwort = self.client.post(self.url(f"/agenda/{top.id}/voting/"), {"voting_method": "summary", **daten})
        assert antwort.status_code == 302, antwort.status_code

    def niederschrift(self, aktion: str, **daten: Any) -> None:
        antwort = self.client.post(self.url(f"/meetings/{self.sitzung.id}/protocol/{aktion}/"), daten)
        assert antwort.status_code == 302, antwort.status_code

    def top_bearbeiten(self, top: SessionAgendaItem, *, oeffentlich: bool) -> None:
        daten = {"name": top.name}
        if oeffentlich:
            daten["is_public"] = "on"
        antwort = self.client.post(self.url(f"/agenda/{top.id}/edit/"), daten)
        assert antwort.status_code == 302, antwort.status_code

    def sitzung_bearbeiten(self, **aenderungen: Any) -> None:
        """Bearbeitungsformular der Sitzung mit den gespeicherten Werten und den gewünschten Änderungen."""
        sitzung = SessionMeeting.objects.get(pk=self.sitzung.pk)
        daten: dict[str, Any] = {
            "name": sitzung.name,
            "organization": str(sitzung.organization_id),
            "start": timezone.localtime(sitzung.start).strftime("%Y-%m-%dT%H:%M"),
            "location": sitzung.location,
            "room": sitzung.room,
            "format": sitzung.format,
            "meeting_state": sitzung.meeting_state,
            "cancellation_reason": sitzung.cancellation_reason,
        }
        if sitzung.is_public:
            daten["is_public"] = "on"
        for name, wert in aenderungen.items():
            if wert is None:
                daten.pop(name, None)
            else:
                daten[name] = wert
        antwort = self.client.post(self.url(f"/meetings/{sitzung.id}/edit/"), daten)
        assert antwort.status_code == 302, antwort.content.decode()[:2000]

    def beschlusskontrolle_veroeffentlichen(self) -> None:
        """Mandant veröffentlicht im Bürgerportal, samt Umsetzungsstand (Opt-in der Verwaltung, Issue #48)."""
        self.tenant.insight_publish = True
        self.tenant.implementation_publish = True
        self.tenant.save(update_fields=["insight_publish", "implementation_publish"])
        source = OParlSource.objects.get(sync_config__session_tenant=self.tenant.slug)
        body = OParlBody.objects.create(
            external_id=f"{source.url}body/", source=source, name=self.tenant.name, slug=self.tenant.slug
        )
        self.tenant.oparl_body = body
        self.tenant.save(update_fields=["oparl_body"])


@pytest.fixture
def welt(settings: Any, tmp_path: Path) -> Welt:
    cache.clear()
    settings.SITE_URL = SITE
    settings.SESSION_EVENTS = "aktiv"
    settings.MEDIA_ROOT = str(tmp_path)
    IdentifierBase.objects.update_or_create(pk=1, defaults={"url": SITE})
    tenant = SessionTenant.objects.create(name="Bezirk Nord", slug="nord", oparl_public_since=timezone.now())
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat")
    rolle = SessionRole.objects.create(tenant=tenant, name="Sitzungsdienst", **dict.fromkeys(ALLE_RECHTE, True))
    user = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    user.roles.add(rolle)
    client = Client()
    client.force_login(user.user)
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        name="Ratssitzung",
        organization=rat,
        start=(timezone.now() - timedelta(days=1)).replace(second=0, microsecond=0),
        is_public=True,
        meeting_state="completed",
    )
    return Welt(tenant=tenant, rat=rat, user=user, client=client, sitzung=sitzung)


def _start() -> int:
    return int(Event.objects.aggregate(hoechste=Max("id"))["hoechste"] or 0)


def _neu(seit: int) -> list[Event]:
    return list(Event.objects.filter(id__gt=seit).order_by("id"))


def _kurz(events: list[Event]) -> list[tuple[str, str, str]]:
    return [(e.type, e.visibility, e.operation) for e in events]


# =============================================================================
# Abstimmung und Beschluss
# =============================================================================


class TestAbstimmung:
    def test_abstimmung_und_beschluss(self, welt: Welt) -> None:
        top = welt.top()
        seit = _start()
        welt.erfassen(top, vote_result="approved", votes_yes="12", votes_no="3", votes_abstain="1")
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.voting.recorded", "oeffentlich", "upsert"),
            ("ris.resolution.adopted", "oeffentlich", "upsert"),
        ]
        assert events[0].payload == {
            "voting": abstimmung(top),
            "agenda_item": kennung("agendaitem", top.pk),
            "meeting": kennung("meeting", welt.sitzung.pk),
            "method": "summary",
            "result": "approved",
        }
        assert events[0].aggregate_type == "Voting"
        assert events[1].payload == {
            "agenda_item": kennung("agendaitem", top.pk),
            "meeting": kennung("meeting", welt.sitzung.pk),
            "result": "approved",
            "changed": ["result"],
        }

        # Nur die Summen korrigiert: neue Abstimmung erfasst, der Beschluss bleibt
        seit = _start()
        welt.erfassen(top, votes_yes="13", votes_no="2", votes_abstain="1")
        assert _kurz(_neu(seit)) == [("ris.voting.recorded", "oeffentlich", "upsert")]

        # Erneut gespeichert ohne Änderung: nichts
        seit = _start()
        welt.erfassen(top)
        assert _neu(seit) == []

    def test_vertagen_ist_ein_beschluss_ohne_abstimmung(self, welt: Welt) -> None:
        top = welt.top()
        seit = _start()
        welt.erfassen(top, vote_result="deferred")
        events = _neu(seit)
        assert _kurz(events) == [("ris.resolution.adopted", "oeffentlich", "upsert")]
        assert events[0].payload["result"] == "deferred"

    def test_ruecknahme_eines_ergebnisses(self, welt: Welt) -> None:
        top = welt.top()
        welt.erfassen(top, vote_result="rejected", votes_yes="1", votes_no="9")
        seit = _start()
        welt.erfassen(top, vote_result="pending")
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.object.depublished", "oeffentlich", "delete"),
            ("ris.agendaitem.changed", "oeffentlich", "upsert"),
        ]
        assert events[0].payload == {"object_type": "Voting", "object": abstimmung(top), "reason": "zurueckgenommen"}
        assert events[1].payload["changed"] == ["result"]

    def test_nichtoeffentlicher_top_nur_nichtoeffentlich(self, welt: Welt) -> None:
        top = welt.top(is_public=False, number="N1")
        seit = _start()
        welt.erfassen(top, vote_result="approved", votes_yes="5")
        assert {e.visibility for e in _neu(seit)} == {"nichtoeffentlich"}
        seit = _start()
        welt.erfassen(top, vote_result="pending")
        events = _neu(seit)
        assert _kurz(events) == [("ris.agendaitem.changed", "nichtoeffentlich", "upsert")], (
            "Keine öffentliche Rücknahme für eine nie öffentliche Abstimmung"
        )

    def test_beschlussnummer(self, welt: Welt) -> None:
        top = welt.top()
        welt.erfassen(top, vote_result="approved", votes_yes="10")
        seit = _start()
        antwort = welt.client.post(welt.url(f"/meetings/{welt.sitzung.id}/resolutions/generate/"))
        assert antwort.status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [("ris.resolution.adopted", "oeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["resolutionNumber"]

    def test_beschlusskontrolle_ohne_opt_in_der_verwaltung_nur_intern(self, welt: Welt) -> None:
        """Ohne ``implementation_publish`` (Standard) ist der Umsetzungsstand nie öffentlich – auch mit Freigabe am TOP."""
        top = welt.top()
        welt.erfassen(top, vote_result="approved", votes_yes="10")
        top.refresh_from_db()
        assert top.implementation_public, "Am Beschluss steht die Freigabe standardmäßig an"
        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{top.id}/tracking/"), {"status": "in_progress"})
        assert antwort.status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [("ris.agendaitem.changed", "nichtoeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["implementationStatus"]

    def test_beschlusskontrolle_mit_opt_in(self, welt: Welt) -> None:
        welt.beschlusskontrolle_veroeffentlichen()
        top = welt.top()
        seit = _start()
        welt.erfassen(top, vote_result="approved", votes_yes="10")
        events = _neu(seit)
        # Mit der Annahme wird der Umsetzungsstand öffentlich (für die Öffentlichkeit neu, ohne früheren Stand)
        assert _kurz(events) == [
            ("ris.voting.recorded", "oeffentlich", "upsert"),
            ("ris.resolution.adopted", "oeffentlich", "upsert"),
            ("ris.resolution.implementation_changed", "oeffentlich", "upsert"),
        ]
        assert events[2].payload == {"agenda_item": kennung("agendaitem", top.pk), "status": "open"}

        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{top.id}/tracking/"), {"status": "in_progress"})
        assert antwort.status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [("ris.resolution.implementation_changed", "oeffentlich", "upsert")]
        assert events[0].payload["status"] == "in_progress"
        assert events[0].payload["previous_status"] == "open"

        # Freigabe am Beschluss zurückgenommen: Wer den Stand kannte, liest den TOP neu
        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{top.id}/tracking/"), {"status": "done", "public": "0"})
        events = _neu(seit)
        assert _kurz(events) == [("ris.agendaitem.changed", "oeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["implementationStatus"]

        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{top.id}/tracking/"), {"status": "deferred", "public": "0"})
        events = _neu(seit)
        assert _kurz(events) == [("ris.agendaitem.changed", "nichtoeffentlich", "upsert")]

    def test_beschlusskontrolle_abgesetzt_nur_intern(self, welt: Welt) -> None:
        welt.beschlusskontrolle_veroeffentlichen()
        abgesetzt = welt.top(vote_result="approved", is_withdrawn=True)
        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{abgesetzt.id}/tracking/"), {"status": "in_progress"})
        assert antwort.status_code == 302
        assert _kurz(_neu(seit)) == [("ris.agendaitem.changed", "nichtoeffentlich", "upsert")]

    def test_nicht_mehr_angenommen_nimmt_den_oeffentlichen_stand_zurueck(self, welt: Welt) -> None:
        welt.beschlusskontrolle_veroeffentlichen()
        top = welt.top()
        welt.erfassen(top, vote_result="approved", votes_yes="10")
        seit = _start()
        welt.erfassen(top, vote_result="rejected", votes_yes="2", votes_no="8")
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.voting.recorded", "oeffentlich", "upsert"),
            ("ris.resolution.adopted", "oeffentlich", "upsert"),
            ("ris.agendaitem.changed", "oeffentlich", "upsert"),
        ]
        assert events[2].payload["changed"] == ["implementationStatus"]
        assert "ris.resolution.implementation_changed" not in {e.type for e in events}


class TestAbstimmungFolgtDemTop:
    """Die Abstimmung ist ein eigenes Aggregat; sie wird mit ihrem TOP veröffentlicht und zurückgenommen."""

    def test_top_nichtoeffentlich_nach_der_abstimmung(self, welt: Welt) -> None:
        top = welt.top()
        welt.erfassen(top, vote_result="approved", votes_yes="10")
        seit = _start()
        welt.top_bearbeiten(top, oeffentlich=False)
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.object.depublished", "oeffentlich", "delete"),
            ("ris.agendaitem.changed", "nichtoeffentlich", "upsert"),
            ("ris.object.depublished", "oeffentlich", "delete"),
        ]
        assert events[2].payload == {"object_type": "Voting", "object": abstimmung(top), "reason": "nichtoeffentlich"}

        # Wieder öffentlich: Punkt und Abstimmung sind für die Öffentlichkeit neu
        seit = _start()
        welt.top_bearbeiten(top, oeffentlich=True)
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.agendaitem.changed", "oeffentlich", "upsert"),
            ("ris.voting.recorded", "oeffentlich", "upsert"),
        ]
        assert events[0].payload["change"] == "added"
        assert events[1].payload["voting"] == abstimmung(top)
        assert events[1].payload["result"] == "approved"

    def test_sitzung_nichtoeffentlich_nach_der_abstimmung(self, welt: Welt) -> None:
        top = welt.top()
        welt.erfassen(top, vote_result="rejected", votes_no="10")
        seit = _start()
        welt.sitzung_bearbeiten(is_public=None)
        zurueck = [e.payload["object_type"] for e in _neu(seit) if e.type == "ris.object.depublished"]
        assert zurueck == ["Meeting", "AgendaItem", "Voting"]

    def test_top_geloescht_nach_der_abstimmung(self, welt: Welt) -> None:
        top = welt.top()
        welt.erfassen(top, vote_result="approved", votes_yes="10")
        seit = _start()
        assert welt.client.post(welt.url(f"/agenda/{top.id}/delete/")).status_code == 302
        events = _neu(seit)
        assert [(e.payload["object_type"], e.payload["reason"]) for e in events] == [
            ("AgendaItem", "quelle_geloescht"),
            ("Voting", "quelle_geloescht"),
        ]

    def test_ergebnis_ohne_abstimmung_nimmt_die_abstimmung_zurueck(self, welt: Welt) -> None:
        top = welt.top()
        welt.erfassen(top, vote_result="approved", votes_yes="10")
        seit = _start()
        welt.erfassen(top, vote_result="deferred")
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.object.depublished", "oeffentlich", "delete"),
            ("ris.resolution.adopted", "oeffentlich", "upsert"),
        ]
        assert events[0].payload["object"] == abstimmung(top)
        assert events[0].payload["reason"] == "zurueckgenommen"


# =============================================================================
# Niederschrift
# =============================================================================


class TestNiederschrift:
    def test_entwurf_genehmigung_veroeffentlichung_ruecknahme(self, welt: Welt) -> None:
        welt.top()
        seit = _start()
        antwort = welt.client.post(welt.url(f"/meetings/{welt.sitzung.id}/protocol/create/"))
        assert antwort.status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [("ris.meeting.changed", "nichtoeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["protocol"]

        seit = _start()
        welt.niederschrift("submit")
        assert _kurz(_neu(seit)) == [("ris.meeting.changed", "nichtoeffentlich", "upsert")]

        seit = _start()
        welt.niederschrift("approve")
        protokoll = SessionProtocol.objects.get(meeting=welt.sitzung)
        assert protokoll.status == "approved"
        events = _neu(seit)
        assert _kurz(events) == [("ris.protocol.approved", "nichtoeffentlich", "upsert")]
        # Genehmigt ohne gewählte Genehmigungssitzung: Genehmigung in der Folgesitzung, nur ohne approved_in
        assert events[0].payload == {
            "protocol": niederschrift(welt.sitzung),
            "meeting": kennung("meeting", welt.sitzung.pk),
            "mode": "follow_up",
        }

        seit = _start()
        welt.niederschrift("publish")
        protokoll.refresh_from_db()
        assert protokoll.public_file_id is not None
        events = _neu(seit)
        assert _kurz(events) == [("ris.protocol.published", "oeffentlich", "upsert")]
        assert events[0].payload == {
            "protocol": niederschrift(welt.sitzung),
            "meeting": kennung("meeting", welt.sitzung.pk),
            "change": "published",
            "file": kennung("file", protokoll.public_file_id),
        }
        assert str(protokoll.pk) not in str(events[0].payload), "Nie die interne Kennung der Niederschrift"

        # Neu erzeugt: erneuert, die alte Fassung ist zurückgenommen
        alt = protokoll.public_file_id
        seit = _start()
        protocol_publication.publish(protokoll, force=True)
        protokoll.refresh_from_db()
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.protocol.published", "oeffentlich", "upsert"),
            ("ris.object.depublished", "oeffentlich", "delete"),
        ]
        assert events[0].payload["change"] == "renewed"
        assert events[1].payload["object"] == kennung("file", alt)

        # Rücknahme der Veröffentlichung
        datei = protokoll.public_file_id
        seit = _start()
        welt.niederschrift("unpublish")
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.object.depublished", "oeffentlich", "delete"),
            ("ris.meeting.changed", "oeffentlich", "upsert"),
        ]
        assert events[0].payload == {
            "object_type": "File",
            "object": kennung("file", datei),
            "reason": "zurueckgenommen",
        }
        assert events[1].payload["changed"] == ["resultsProtocol"]

    def test_genehmigung_mit_folgesitzung(self, welt: Welt) -> None:
        folge = SessionMeeting.objects.create(
            tenant=welt.tenant,
            name="Folgesitzung",
            organization=welt.rat,
            start=timezone.now() + timedelta(days=14),
            is_public=True,
        )
        welt.client.post(welt.url(f"/meetings/{welt.sitzung.id}/protocol/create/"))
        welt.niederschrift("submit")
        seit = _start()
        welt.niederschrift("approve", approval_meeting=str(folge.pk))
        events = _neu(seit)
        assert _kurz(events) == [("ris.protocol.approved", "nichtoeffentlich", "upsert")]
        assert events[0].payload["mode"] == "follow_up"
        assert events[0].payload["approved_in"] == kennung("meeting", folge.pk)

    def test_direkt_veroeffentlicht_ohne_genehmigungsschritt(self, welt: Welt) -> None:
        welt.tenant.protocol_approval_mode = SessionTenant.PROTOCOL_APPROVAL_DIRECT
        welt.tenant.save(update_fields=["protocol_approval_mode"])
        welt.top()
        welt.client.post(welt.url(f"/meetings/{welt.sitzung.id}/protocol/create/"))
        welt.niederschrift("submit")
        seit = _start()
        welt.niederschrift("publish")
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.protocol.approved", "nichtoeffentlich", "upsert"),
            ("ris.protocol.published", "oeffentlich", "upsert"),
        ]
        assert events[0].payload["mode"] == "direct"
        assert "approved_in" not in events[0].payload


# =============================================================================
# Erneuerung nach dem Commit (refresh_meeting) – mit echter Transaktion
# =============================================================================


@pytest.mark.django_db(transaction=True)
class TestErneuerungNachDemCommit:
    """
    Ändert sich der öffentliche Inhalt nach der Veröffentlichung, erneuert ``refresh_meeting`` die Fassung nach dem
    Commit (``on_commit``). Es öffnet seine eigene Erfassung – die der Änderung ist dann schon geschrieben.
    """

    def _veroeffentlicht(self, welt: Welt) -> SessionProtocol:
        welt.top(1, name="Radweg")
        welt.top(2, name="Spielplatz")
        welt.client.post(welt.url(f"/meetings/{welt.sitzung.id}/protocol/create/"))
        welt.niederschrift("submit")
        welt.niederschrift("approve")
        welt.niederschrift("publish")
        protokoll = SessionProtocol.objects.get(meeting=welt.sitzung)
        assert protokoll.public_file_id is not None
        return protokoll

    def test_top_nachtraeglich_nichtoeffentlich(self, welt: Welt) -> None:
        protokoll = self._veroeffentlicht(welt)
        alt = protokoll.public_file_id
        top = SessionAgendaItem.objects.get(meeting=welt.sitzung, name="Spielplatz")
        seit = _start()
        welt.top_bearbeiten(top, oeffentlich=False)
        protokoll.refresh_from_db()
        assert protokoll.public_file_id not in (None, alt), "Neue Fassung ohne den nichtöffentlichen TOP"
        events = _neu(seit)
        assert _kurz(events) == [
            ("ris.object.depublished", "oeffentlich", "delete"),
            ("ris.agendaitem.changed", "nichtoeffentlich", "upsert"),
            ("ris.protocol.published", "oeffentlich", "upsert"),
            ("ris.object.depublished", "oeffentlich", "delete"),
        ]
        assert events[2].payload == {
            "protocol": niederschrift(welt.sitzung),
            "meeting": kennung("meeting", welt.sitzung.pk),
            "change": "renewed",
            "file": kennung("file", protokoll.public_file_id),
        }
        assert events[3].payload == {
            "object_type": "File",
            "object": kennung("file", alt),
            "reason": "quelle_geloescht",
        }

    def test_sitzung_umbenannt(self, welt: Welt) -> None:
        protokoll = self._veroeffentlicht(welt)
        alt = protokoll.public_file_id
        seit = _start()
        welt.sitzung_bearbeiten(name="Ratssitzung (Sondersitzung)")
        events = _neu(seit)
        assert [e.type for e in events] == [
            "ris.meeting.changed",
            "ris.protocol.published",
            "ris.object.depublished",
        ]
        assert events[1].payload["change"] == "renewed"
        assert events[2].payload["object"] == kennung("file", alt)

    def test_sitzung_nichtoeffentlich_nimmt_die_fassung_zurueck(self, welt: Welt) -> None:
        protokoll = self._veroeffentlicht(welt)
        alt = protokoll.public_file_id
        seit = _start()
        welt.sitzung_bearbeiten(is_public=None)
        protokoll.refresh_from_db()
        assert protokoll.public_file_id is None
        events = _neu(seit)
        datei = [e for e in events if e.aggregate_type == "File"]
        assert [(e.type, e.payload["object"], e.payload["reason"]) for e in datei] == [
            ("ris.object.depublished", kennung("file", alt), "zurueckgenommen")
        ]
        assert events[-1].type == "ris.meeting.changed"
        assert events[-1].payload["changed"] == ["resultsProtocol"]
        assert "ris.protocol.published" not in {e.type for e in events}


# =============================================================================
# Freischaltung
# =============================================================================


def test_vor_der_freischaltung_nichts_oeffentliches(welt: Welt) -> None:
    welt.tenant.oparl_public_since = None
    welt.tenant.save(update_fields=["oparl_public_since"])
    top = welt.top()
    seit = _start()
    welt.erfassen(top, vote_result="approved", votes_yes="10")
    welt.top_bearbeiten(top, oeffentlich=False)
    welt.top_bearbeiten(top, oeffentlich=True)
    welt.client.post(welt.url(f"/meetings/{welt.sitzung.id}/protocol/create/"))
    welt.niederschrift("submit")
    welt.niederschrift("approve")
    welt.niederschrift("publish")
    welt.niederschrift("unpublish")
    events = _neu(seit)
    assert {"ris.voting.recorded", "ris.protocol.approved"} <= {e.type for e in events}
    assert {e.visibility for e in events} == {"nichtoeffentlich"}
    assert not {"ris.protocol.published", "ris.object.depublished"} & {e.type for e in events}

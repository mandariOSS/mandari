# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session meldet Abstimmung, Beschluss, Niederschrift und ihre Rücknahme an die Datendrehscheibe (Issue #535).

Geprüft an den Fachfunktionen (Ansichten und Dienste, wie die Oberfläche sie aufruft):

- Abstimmung erfassen: ``ris.voting.recorded`` (nie Einzelstimmen) und ``ris.resolution.adopted``; Vertagung ohne
  Abstimmung; Rücknahme eines Ergebnisses als ``ris.object.depublished`` der Abstimmung (``zurueckgenommen``) und
  geänderter TOP; nichtöffentliche TOPs nur nichtöffentlich;
- Beschlussnummer (``resolutionNumber``) und Beschlusskontrolle (``ris.resolution.implementation_changed`` nur bei
  freigegebener Veröffentlichung);
- Niederschrift: Entwurf und Prüfung intern, Genehmigung (``ris.protocol.approved``), Veröffentlichung der
  öffentlichen Fassung (``ris.protocol.published``), Erneuerung, Rücknahme (``ris.object.depublished`` der Datei).

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

pytestmark = pytest.mark.django_db

SITE = "https://mandari.example"
BASIS = f"{SITE}/session/nord/api/oparl/"
ALLE_RECHTE = {f.name for f in SessionRole._meta.get_fields() if f.name.startswith("can_")}


def kennung(kind: str, pk: Any) -> str:
    return str(canonical_id(f"{BASIS}{kind}/{pk}/"))


def abstimmung(top: SessionAgendaItem) -> str:
    """Kennung der Abstimmung eines TOP (Adresse des TOP mit dem Zusatz ``voting``)."""
    return str(canonical_id(f"{BASIS}agendaitem/{top.pk}/voting"))


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

    def test_beschlusskontrolle(self, welt: Welt) -> None:
        top = welt.top()
        welt.erfassen(top, vote_result="approved", votes_yes="10")
        top.refresh_from_db()
        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{top.id}/tracking/"), {"status": "in_progress"})
        assert antwort.status_code == 302
        events = _neu(seit)
        assert _kurz(events) == [("ris.resolution.implementation_changed", "oeffentlich", "upsert")]
        assert events[0].payload["status"] == "in_progress"

        seit = _start()
        antwort = welt.client.post(welt.url(f"/agenda/{top.id}/tracking/"), {"status": "done", "public": "0"})
        events = _neu(seit)
        assert _kurz(events) == [("ris.agendaitem.changed", "nichtoeffentlich", "upsert")]
        assert events[0].payload["changed"] == ["implementationStatus"]


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
        assert events[0].payload == {
            "protocol": str(protokoll.pk),
            "meeting": kennung("meeting", welt.sitzung.pk),
            "mode": "direct",
        }

        seit = _start()
        welt.niederschrift("publish")
        protokoll.refresh_from_db()
        assert protokoll.public_file_id is not None
        events = _neu(seit)
        assert _kurz(events) == [("ris.protocol.published", "oeffentlich", "upsert")]
        assert events[0].payload == {
            "protocol": str(protokoll.pk),
            "meeting": kennung("meeting", welt.sitzung.pk),
            "change": "published",
            "file": kennung("file", protokoll.public_file_id),
        }

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

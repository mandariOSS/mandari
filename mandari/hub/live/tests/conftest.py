# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures der Live-Übertragungen (Issue #915): erfundene Kommune „Musterstadt“ mit Rat, Sitzung, TOPs, Personen."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone

from hub.live.models import (
    Broadcast,
    BroadcastSection,
    BroadcastSource,
    BroadcastSpeech,
    BroadcastStatus,
    SpeechAssignment,
)
from hub.live.profil import vorlage
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

BASIS = "https://ris.musterstadt.example/oparl"
EMBED_ID = "0a1b2c3d-4e5f-11ee-8a9b-0c1d2e3f4a5b"


def kennung(art: str) -> str:
    return f"{BASIS}/{art}/{uuid.uuid4()}"


@dataclass
class Welt:
    body: OParlBody
    rat: OParlOrganization
    sitzung: OParlMeeting
    top1: OParlAgendaItem
    top5: OParlAgendaItem
    top51: OParlAgendaItem
    vorlage: OParlPaper
    muster: OParlPerson
    beispiel: OParlPerson
    quelle: BroadcastSource
    jetzt: datetime


@pytest.fixture(autouse=True)
def _live_an(settings: Any) -> None:
    settings.LIVE_UEBERTRAGUNG_AKTIV = True


@pytest.fixture
def jetzt() -> datetime:
    return timezone.now().replace(microsecond=0)


def person(body: OParlBody, vorname: str, nachname: str, *, titel: str = "") -> OParlPerson:
    name = " ".join(t for t in (titel, vorname, nachname) if t)
    return OParlPerson.objects.create(
        external_id=kennung("persons"),
        body=body,
        name=name,
        given_name=vorname,
        family_name=nachname,
        title=titel or None,
    )


def mitglied(person: OParlPerson, gremium: OParlOrganization, **felder: Any) -> OParlMembership:
    return OParlMembership.objects.create(
        external_id=kennung("memberships"), person=person, organization=gremium, **felder
    )


@pytest.fixture
def welt(db: Any, jetzt: datetime) -> Welt:
    source = OParlSource.objects.create(name="Musterstadt-RIS", url=f"{BASIS}/system")
    body = OParlBody.objects.create(external_id=kennung("bodies"), source=source, name="Musterstadt", slug="muster")
    rat = OParlOrganization.objects.create(external_id=kennung("organizations"), body=body, name="Rat")
    sitzung = OParlMeeting.objects.create(
        external_id=kennung("meetings"), body=body, name="Sitzung des Rates", start=jetzt - timedelta(minutes=5)
    )
    sitzung.organizations.set([rat])
    top1 = OParlAgendaItem.objects.create(
        external_id=kennung("agendaitems"), meeting=sitzung, number="1", order=1, name="Eröffnung der Sitzung"
    )
    top5 = OParlAgendaItem.objects.create(
        external_id=kennung("agendaitems"), meeting=sitzung, number="Ö 5", order=5, name="Neubau einer Grundschule"
    )
    top51 = OParlAgendaItem.objects.create(
        external_id=kennung("agendaitems"), meeting=sitzung, number="5.1", order=6, name="Haushaltssatzung 2027"
    )
    papier = OParlPaper.objects.create(
        external_id=kennung("papers"), body=body, name="Neubau einer Grundschule", reference="V/1/2026"
    )
    OParlConsultation.objects.create(
        external_id=kennung("consultations"),
        body=body,
        paper=papier,
        paper_external_id=papier.external_id,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=top5.external_id,
    )
    muster = person(body, "Erika", "Muster", titel="Dr.")
    beispiel = person(body, "Max", "Beispiel")
    mitglied(muster, rat, start_date=date(2020, 1, 1))
    mitglied(beispiel, rat)
    quelle = BroadcastSource.objects.create(
        body=body,
        organization=rat,
        provider="3q",
        identifier=EMBED_ID,
        page_url="https://www.musterstadt.example/live",
        overlay_profile=vorlage("balken_unten_dreizeilig"),
        active=True,
    )
    return Welt(
        body=body,
        rat=rat,
        sitzung=sitzung,
        top1=top1,
        top5=top5,
        top51=top51,
        vorlage=papier,
        muster=muster,
        beispiel=beispiel,
        quelle=quelle,
        jetzt=jetzt,
    )


@pytest.fixture
def laufend(welt: Welt) -> Broadcast:
    """Laufende Übertragung seit 16:15 Uhr: TOP 5 mit Vorlage, zwei Wortmeldungen, Erika Muster am Wort."""
    broadcast = Broadcast.objects.create(
        source=welt.quelle,
        meeting=welt.sitzung,
        status=BroadcastStatus.LIVE,
        started_at=timezone.localtime(welt.jetzt).replace(hour=16, minute=15),
    )
    abschnitt = BroadcastSection.objects.create(
        broadcast=broadcast, agenda_item=welt.top5, number="5", started_at=welt.jetzt
    )
    unbekannt = BroadcastSpeech.objects.create(
        broadcast=broadcast,
        section=abschnitt,
        name_read="Gisela Gast",
        function_read="Bürgermeisterin",
        started_at=welt.jetzt,
        assignment=SpeechAssignment.KEINE,
    )
    am_wort = BroadcastSpeech.objects.create(
        broadcast=broadcast,
        section=abschnitt,
        person=welt.muster,
        name_read="Erika Muster",
        faction_read="Fraktion A",
        started_at=welt.jetzt + timedelta(minutes=2),
        assignment=SpeechAssignment.EINDEUTIG,
        readings=3,
    )
    assert unbekannt.pk != am_wort.pk
    broadcast.current_section = abschnitt
    broadcast.current_speech = am_wort
    broadcast.save()
    return broadcast

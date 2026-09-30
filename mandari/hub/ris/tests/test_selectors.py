# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lese-Fassade des RIS-Bestands (Issue #522): fachliche Abfragen, auf die Kommunen beschränkt."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone

from hub.ris import selectors as ris
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

BASIS = "https://ris.beispielstadt.example/oparl"


def _kennung(art: str) -> str:
    return f"{BASIS}/{art}/{uuid.uuid4()}"


@dataclass
class Bestand:
    body: OParlBody
    fremd: OParlBody
    rat: OParlOrganization
    ausschuss: OParlOrganization
    aufgeloest: OParlOrganization
    fremdes_gremium: OParlOrganization
    vergangen: OParlMeeting
    kommend: OParlMeeting
    abgesagt: OParlMeeting
    fremde_sitzung: OParlMeeting
    top1: OParlAgendaItem
    top2: OParlAgendaItem
    vorlage: OParlPaper
    alte_vorlage: OParlPaper
    fremde_vorlage: OParlPaper
    beratung: OParlConsultation
    datei: OParlFile
    person: OParlPerson


def _body(source: OParlSource, name: str) -> OParlBody:
    return OParlBody.objects.create(external_id=_kennung("bodies"), source=source, name=name)


def _gremium(body: OParlBody, name: str, **felder: Any) -> OParlOrganization:
    return OParlOrganization.objects.create(external_id=_kennung("organizations"), body=body, name=name, **felder)


def _sitzung(body: OParlBody, start: datetime, *gremien: OParlOrganization, **felder: Any) -> OParlMeeting:
    sitzung = OParlMeeting.objects.create(external_id=_kennung("meetings"), body=body, start=start, **felder)
    sitzung.organizations.set(gremien)
    return sitzung


def _vorlage(body: OParlBody, name: str, reference: str, datum: date) -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=_kennung("papers"), body=body, name=name, reference=reference, date=datum
    )


@pytest.fixture
def jetzt() -> datetime:
    return timezone.now().replace(microsecond=0)


@pytest.fixture
def bestand(jetzt: datetime) -> Bestand:
    source = OParlSource.objects.create(name="Beispiel-RIS", url=f"{BASIS}/system")
    body = _body(source, "Beispielstadt")
    fremd = _body(source, "Nachbarstadt")
    rat = _gremium(body, "Rat")
    ausschuss = _gremium(body, "Bauausschuss")
    aufgeloest = _gremium(body, "Sonderausschuss", end_date=date.today() - timedelta(days=30))
    fremdes_gremium = _gremium(fremd, "Rat Nachbarstadt")
    vergangen = _sitzung(body, jetzt - timedelta(days=7), rat, name="Rat im Mai")
    kommend = _sitzung(body, jetzt + timedelta(days=3), rat, ausschuss, name="Bauausschuss im Juni")
    abgesagt = _sitzung(body, jetzt + timedelta(days=5), ausschuss, name="Bauausschuss abgesagt", cancelled=True)
    fremde_sitzung = _sitzung(fremd, jetzt + timedelta(days=2), fremdes_gremium, name="Rat Nachbarstadt")
    top2 = OParlAgendaItem.objects.create(external_id=_kennung("agendaitems"), meeting=kommend, number="2", order=2)
    top1 = OParlAgendaItem.objects.create(external_id=_kennung("agendaitems"), meeting=kommend, number="1", order=1)
    vorlage = _vorlage(body, "Spielplatz sanieren", "V/0042/2026", date(2026, 9, 1))
    alte_vorlage = _vorlage(body, "Radweg planen", "V/0007/2025", date(2025, 3, 1))
    fremde_vorlage = _vorlage(fremd, "Spielplatz Nachbarstadt", "N/1/2026", date(2026, 9, 2))
    beratung = OParlConsultation.objects.create(
        external_id=_kennung("consultations"),
        body=body,
        paper=vorlage,
        paper_external_id=vorlage.external_id,
        meeting_external_id=kommend.external_id,
        agenda_item_external_id=top1.external_id,
        role="Entscheidung",
        authoritative=True,
    )
    datei = OParlFile.objects.create(external_id=_kennung("files"), body=body, paper=vorlage, name="Anlage 1")
    person = OParlPerson.objects.create(external_id=_kennung("persons"), body=body, name="Erika Muster")
    OParlMembership.objects.create(external_id=_kennung("memberships"), person=person, organization=rat)
    OParlMembership.objects.create(external_id=_kennung("memberships"), person=person, organization=fremdes_gremium)
    return Bestand(
        body=body,
        fremd=fremd,
        rat=rat,
        ausschuss=ausschuss,
        aufgeloest=aufgeloest,
        fremdes_gremium=fremdes_gremium,
        vergangen=vergangen,
        kommend=kommend,
        abgesagt=abgesagt,
        fremde_sitzung=fremde_sitzung,
        top1=top1,
        top2=top2,
        vorlage=vorlage,
        alte_vorlage=alte_vorlage,
        fremde_vorlage=fremde_vorlage,
        beratung=beratung,
        datei=datei,
        person=person,
    )


def _eigene(b: Bestand) -> Any:
    return OParlBody.objects.filter(pk=b.body.pk)


# --- Sitzungen -------------------------------------------------------------------------------


def test_sitzungen_nur_der_eigenen_kommunen(bestand: Bestand) -> None:
    assert set(ris.meetings(_eigene(bestand))) == {bestand.vergangen, bestand.kommend, bestand.abgesagt}
    assert set(ris.meetings([bestand.body, bestand.fremd])) >= {bestand.fremde_sitzung}
    assert set(ris.meetings([bestand.fremd.pk])) == {bestand.fremde_sitzung}


def test_einzelne_sitzung(bestand: Bestand) -> None:
    eigene = _eigene(bestand)
    assert ris.meeting(eigene, str(bestand.kommend.pk)) == bestand.kommend
    assert ris.meeting(eigene, bestand.kommend.pk) == bestand.kommend
    assert ris.meeting(eigene, bestand.fremde_sitzung.pk) is None
    assert ris.meeting(eigene, "keine-kennung") is None
    assert ris.meeting(eigene, None) is None


def test_kommende_sitzungen(bestand: Bestand, jetzt: datetime) -> None:
    assert list(ris.upcoming_meetings(_eigene(bestand))) == [bestand.kommend]
    seit_letzter_woche = ris.upcoming_meetings(_eigene(bestand), since=jetzt - timedelta(days=7))
    assert list(seit_letzter_woche) == [bestand.vergangen, bestand.kommend]


def test_sitzungen_suchen(bestand: Bestand) -> None:
    eigene = _eigene(bestand)
    assert list(ris.search_meetings(eigene, "bauausschuss")) == [bestand.abgesagt, bestand.kommend]
    assert list(ris.search_meetings(eigene, "", on=timezone.localtime(bestand.vergangen.start).date())) == [
        bestand.vergangen
    ]
    assert list(ris.search_meetings(eigene, "Nachbarstadt")) == []


def test_sitzungen_von_gremien_im_zeitraum(bestand: Bestand, jetzt: datetime) -> None:
    beide: list[OParlOrganization | uuid.UUID] = [bestand.rat, bestand.ausschuss.pk]
    assert list(ris.meetings_of_organizations(beide)) == [bestand.vergangen, bestand.kommend, bestand.abgesagt]
    kommend = bestand.kommend.start
    # ab (einschließlich) … bis (einschließlich)
    assert list(ris.meetings_of_organizations(beide, starts_from=kommend, starts_until=kommend)) == [bestand.kommend]
    # nach (ausschließlich)
    assert list(ris.meetings_of_organizations(beide, starts_after=kommend)) == [bestand.abgesagt]
    assert list(ris.meetings_of_organizations(beide, starts_from=jetzt, include_cancelled=False)) == [bestand.kommend]
    assert list(ris.meetings_of_organizations([])) == []


def test_sitzungen_zu_oparl_kennungen(bestand: Bestand) -> None:
    kennungen = [bestand.kommend.external_id, "https://unbekannt.example/meetings/1"]
    assert list(ris.meetings_by_external_id(kennungen)) == [bestand.kommend]


# --- Tagesordnung ------------------------------------------------------------------------------


def test_tagesordnung_in_reihenfolge(bestand: Bestand) -> None:
    assert list(ris.agenda_items(bestand.kommend)) == [bestand.top1, bestand.top2]
    assert list(ris.agenda_items(bestand.vergangen)) == []


def test_tagesordnungspunkt_vorhanden(bestand: Bestand) -> None:
    assert ris.agenda_item_exists(bestand.top1.pk)
    assert ris.agenda_item_exists(str(bestand.top1.pk))
    assert not ris.agenda_item_exists(uuid.uuid4())
    assert not ris.agenda_item_exists("keine-kennung")


def test_tagesordnungspunkte_zu_oparl_kennungen(bestand: Bestand) -> None:
    assert list(ris.agenda_items_by_external_id([bestand.top2.external_id])) == [bestand.top2]


def test_vorlagen_eines_tagesordnungspunkts(bestand: Bestand) -> None:
    assert list(ris.papers_of_agenda_item(bestand.top1)) == [bestand.vorlage]
    assert list(ris.papers_of_agenda_item(bestand.top2)) == []


# --- Vorlagen ------------------------------------------------------------------------------------


def test_vorlagen_nur_der_eigenen_kommunen(bestand: Bestand) -> None:
    assert set(ris.papers(_eigene(bestand))) == {bestand.vorlage, bestand.alte_vorlage}
    assert ris.paper(_eigene(bestand), bestand.vorlage.pk) == bestand.vorlage
    assert ris.paper(_eigene(bestand), str(bestand.fremde_vorlage.pk)) is None
    assert ris.paper(_eigene(bestand), "' OR 1=1 --") is None


def test_vorlage_vorhanden(bestand: Bestand) -> None:
    assert ris.paper_exists(bestand.fremde_vorlage.pk)
    assert not ris.paper_exists(uuid.uuid4())
    assert not ris.paper_exists("")


def test_vorlagen_suchen_nach_name_und_nummer(bestand: Bestand) -> None:
    eigene = _eigene(bestand)
    assert list(ris.search_papers(eigene, "spielplatz")) == [bestand.vorlage]
    assert list(ris.search_papers(eigene, "V/00")) == [bestand.vorlage, bestand.alte_vorlage]
    assert list(ris.search_papers(eigene, "Nachbarstadt")) == []


def test_neueste_vorlagen(bestand: Bestand) -> None:
    assert list(ris.recent_papers(_eigene(bestand), limit=1)) == [bestand.vorlage]


def test_beratungsfolge_und_dateien_einer_vorlage(bestand: Bestand) -> None:
    assert list(ris.consultations_of_paper(bestand.vorlage)) == [bestand.beratung]
    assert list(ris.files_of_paper(bestand.vorlage)) == [bestand.datei]
    assert list(ris.files(_eigene(bestand))) == [bestand.datei]
    assert list(ris.files([bestand.fremd])) == []


def test_zurueckgenommene_beratungen_zaehlen_nicht(bestand: Bestand) -> None:
    # Von mandari Session zurückgenommen: gelöscht und mit Kennung der Session-Schnittstelle (withdrawn_q)
    OParlConsultation.objects.filter(pk=bestand.beratung.pk).update(
        deleted=True, external_id="https://mandari.example/session/beispiel/api/oparl/consultation/1"
    )
    assert list(ris.consultations_of_paper(bestand.vorlage)) == []
    assert list(ris.papers_of_agenda_item(bestand.top1)) == []


def test_in_der_quelle_geloeschte_beratung_bleibt_sichtbar(bestand: Bestand) -> None:
    """Nur Rücknahmen aus Session fallen heraus; Löschungen fremder Quellen regelt der Aufrufer."""
    OParlConsultation.objects.filter(pk=bestand.beratung.pk).update(deleted=True)
    assert list(ris.consultations_of_paper(bestand.vorlage)) == [bestand.beratung]


# --- Gremien und Personen -------------------------------------------------------------------------


def test_gremien(bestand: Bestand) -> None:
    eigene = _eigene(bestand)
    assert set(ris.organizations(eigene)) == {bestand.rat, bestand.ausschuss, bestand.aufgeloest}
    assert set(ris.active_organizations(eigene)) == {bestand.rat, bestand.ausschuss}
    vor_einem_jahr = date.today() - timedelta(days=365)
    assert bestand.aufgeloest in ris.active_organizations(eigene, on=vor_einem_jahr)
    assert ris.organization(eigene, bestand.rat.pk) == bestand.rat
    assert ris.organization(eigene, bestand.fremdes_gremium.pk) is None
    assert list(ris.organizations_by_external_id([bestand.ausschuss.external_id])) == [bestand.ausschuss]


def test_personen_und_mitgliedschaften(bestand: Bestand) -> None:
    assert list(ris.persons(_eigene(bestand))) == [bestand.person]
    assert ris.memberships_of_person(bestand.person).count() == 2
    nur_eigene = ris.memberships_of_person(bestand.person, bodies=_eigene(bestand))
    assert [mitgliedschaft.organization for mitgliedschaft in nur_eigene] == [bestand.rat]

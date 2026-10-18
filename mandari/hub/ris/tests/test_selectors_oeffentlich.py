# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lese-Fassade: öffentlicher Bestand für den KI-Assistenten und offene Schnittstellen (Issue #899)."""

from __future__ import annotations

from datetime import date

import pytest

from hub.ris import selectors as ris
from insight_ai.tests.musterstadt import JETZT, Musterstadt, baue_musterstadt, kennung, zeit
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlOrganization

pytestmark = pytest.mark.django_db


@pytest.fixture
def stadt() -> Musterstadt:
    return baue_musterstadt()


def test_sitzungen_im_zeitraum_ohne_geloeschte_und_fremde(stadt: Musterstadt) -> None:
    woche = ris.public_meetings_between([stadt.body.pk], zeit(5, 0), zeit(11, 23))
    assert list(woche) == [stadt.rat_sitzung, stadt.ausschuss_sitzung, stadt.abgesagte_sitzung]
    nur_rat = ris.public_meetings_between([stadt.body.pk], zeit(5, 0), zeit(31, 23), organizations=[stadt.rat])
    assert list(nur_rat) == [stadt.rat_sitzung, stadt.spaetere_sitzung]


def test_einzelne_sitzung_nur_oeffentlich_und_eigene(stadt: Musterstadt) -> None:
    assert ris.public_meeting([stadt.body.pk], stadt.rat_sitzung.pk) == stadt.rat_sitzung
    assert ris.public_meeting([stadt.body.pk], stadt.geloeschte_sitzung.pk) is None
    assert ris.public_meeting([stadt.body.pk], stadt.fremde_sitzung.pk) is None
    assert ris.public_meeting([stadt.body.pk], "keine-kennung") is None


def test_tagesordnung_in_natuerlicher_reihenfolge_mit_vorlagen(stadt: Musterstadt) -> None:
    sitzung = OParlMeeting.objects.create(external_id=kennung("meetings"), body=stadt.body, start=JETZT)
    for nummer in ("10", "2", "1"):
        OParlAgendaItem.objects.create(external_id=kennung("agendaitems"), meeting=sitzung, number=nummer)
    OParlAgendaItem.objects.create(external_id=kennung("agendaitems"), meeting=sitzung, number="3", deleted=True)
    assert [eintrag.item.number for eintrag in ris.public_agenda(sitzung)] == ["1", "2", "10"]

    tagesordnung = ris.public_agenda(stadt.rat_sitzung)
    assert [eintrag.item for eintrag in tagesordnung] == [
        stadt.top_eroeffnung,
        stadt.top_radweg,
        stadt.top_nichtoeffentlich,
    ]
    assert tagesordnung[1].papers == [stadt.vorlage]
    stadt.vorlage.mark_deleted()
    assert ris.public_agenda(stadt.rat_sitzung)[1].papers == []


def test_beratungsfolge_ohne_nichtoeffentliche_ergebnisse(stadt: Musterstadt) -> None:
    verlauf = ris.public_consultation_history([stadt.vorlage.pk, stadt.grundstueck.pk, "keine-kennung"])

    radweg = verlauf[stadt.vorlage.pk]
    assert [schritt.meeting_id for schritt in radweg] == [stadt.vergangene_sitzung.pk, stadt.rat_sitzung.pk]
    assert radweg[0].organization_name == "Ausschuss für Umwelt und Verkehr"
    assert radweg[0].result == "einstimmig empfohlen"
    assert radweg[0].resolution_text is not None
    assert radweg[1].authoritative is True and radweg[1].agenda_number == "2"

    (grundstueck,) = verlauf[stadt.grundstueck.pk]
    assert grundstueck.public is False
    assert grundstueck.result is None and grundstueck.resolution_text is None
    assert ris.public_consultation_history([]) == {}


def test_beratung_in_geloeschter_sitzung_ohne_termin(stadt: Musterstadt) -> None:
    stadt.vergangene_sitzung.mark_deleted()
    radweg = ris.public_consultation_history([stadt.vorlage.pk])[stadt.vorlage.pk]
    # Termin unbekannt: steht zuletzt, ohne Sitzung und ohne Tagesordnungspunkt
    assert radweg[-1].meeting_id is None and radweg[-1].date is None and radweg[-1].result is None


def test_vorlagen_nur_der_kommune_und_nicht_geloescht(stadt: Musterstadt) -> None:
    eigene = [stadt.body.pk]
    assert list(ris.public_papers_by_reference(eigene, " v/2026/0123 ")) == [stadt.vorlage]
    assert list(ris.public_papers_by_reference(eigene, "")) == []
    assert ris.public_papers_by_ids(eigene, [stadt.vorlage.pk, stadt.fremde_vorlage.pk, "x"]) == {
        stadt.vorlage.pk: stadt.vorlage
    }
    assert list(ris.search_public_papers(eigene, "radweg muster")) == [stadt.vorlage]
    assert list(ris.search_public_papers(eigene, "Radweg", date_from=date(2026, 9, 2))) == []
    assert list(ris.search_public_papers(eigene, "Radweg", organizations=[stadt.ausschuss])) == [stadt.vorlage]
    assert list(ris.search_public_papers(eigene, "Grundstück", organizations=[stadt.ausschuss])) == []
    stadt.vorlage.mark_deleted()
    assert ris.public_paper(eigene, stadt.vorlage.pk) is None


def test_dateien_nur_oeffentlich(stadt: Musterstadt) -> None:
    eigene = [stadt.body.pk]
    assert list(ris.public_files_of_paper(stadt.vorlage)) == [stadt.datei]
    ids = [stadt.datei.pk, stadt.datei_entfernt.pk, stadt.fremde_datei.pk]
    assert list(ris.public_files_by_ids(eigene, ids)) == [stadt.datei.pk]
    datei = ris.public_file_with_text(eigene, stadt.datei.pk)
    assert datei is not None and "Radweg" in (datei.text_content or "")
    assert ris.public_file_with_text(eigene, stadt.fremde_datei.pk) is None


def test_gremien_zum_namen_einer_frage(stadt: Musterstadt) -> None:
    OParlOrganization.objects.create(external_id=kennung("organizations"), body=stadt.body, name="Seniorenbeirat")
    eigene = [stadt.body.pk]
    assert ris.public_organizations_named(eigene, "rat") == [stadt.rat]
    assert [o.name for o in ris.public_organizations_named(eigene, "beirat")] == ["Seniorenbeirat"]
    assert [o.name for o in ris.public_organizations_named(eigene, "Stadtfest")] == ["Sonderausschuss Stadtfest"]
    assert ris.public_organizations_named(eigene, "  ") == []
    assert [o.name for o in ris.public_organizations(eigene)] == [
        "Ausschuss für Umwelt und Verkehr",
        "Rat",
        "Seniorenbeirat",
    ]


def test_personen_und_laufende_mitgliedschaften(stadt: Musterstadt) -> None:
    eigene = [stadt.body.pk]
    assert list(ris.search_public_persons(eigene, "erika muster")) == [stadt.person]
    assert list(ris.search_public_persons([stadt.fremd.pk], "Mustermann")) == []
    laufend = ris.public_current_memberships(stadt.person, on=JETZT.date())
    assert [m.organization for m in laufend] == [stadt.rat]


def test_kommune(stadt: Musterstadt) -> None:
    assert ris.public_body(stadt.body.pk) == stadt.body
    assert ris.public_body("keine-kennung") is None
    stadt.fremd.mark_deleted()
    assert ris.public_body(stadt.fremd.pk) is None


def test_gelistete_kommunen(stadt: Musterstadt) -> None:
    OParlBody.objects.filter(pk=stadt.body.pk).update(slug="musterstadt")
    OParlBody.objects.filter(pk=stadt.fremd.pk).update(slug="nachbarort", is_listed=False)
    assert [b.slug for b in ris.listed_bodies()] == ["musterstadt"]
    assert [b.slug for b in ris.listed_bodies("muster")] == ["musterstadt"]
    assert ris.listed_body("Musterstadt") == stadt.body
    assert ris.listed_body(str(stadt.body.pk)) == stadt.body
    assert ris.listed_body("nachbarort") is None
    assert ris.listed_body(str(stadt.fremd.pk)) is None
    assert ris.listed_body("  ") is None

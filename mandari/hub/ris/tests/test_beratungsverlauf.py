# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lese-Fassade: Beratungsverlauf einer Vorlage und Zuordnungen zwischen Vorlagen und Tagesordnungspunkten (Issue #853).

Die RIS-Seiten von Work im neuen Erscheinungsbild bauen Stand-Satz und Zeitstrahl aus ``consultation_history`` und
ordnen Positionen über ``agenda_items_of_papers`` bzw. ``papers_of_agenda_items`` zu. Von mandari Session
zurückgenommene Beratungen, Sitzungen, Punkte und Vorlagen zählen dabei nicht – wie in Insight. Die Vorgangsseite von
Insight liest den Verlauf aus derselben Abfrage, Niederschrift, Sitzungsformat und Sitzungsdateien lesen beide
Sitzungsseiten aus der Fassade (keine doppelten Abfragen mehr).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
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
    OParlOrganization,
    OParlPaper,
    OParlSource,
)
from insight_core.services.paper_status import paper_status

pytestmark = pytest.mark.django_db

RIS = "https://ris.verlauf.example/oparl"
#: Kennung der Session-Schnittstelle: gelöscht und mit dieser Kennung gilt ein Objekt als zurückgenommen
SESSION = "https://mandari.example/session/stadt/api/oparl"


def _kennung(art: str, basis: str = RIS) -> str:
    return f"{basis}/{art}/{uuid.uuid4()}"


def _beratung(vorlage: OParlPaper, sitzung: OParlMeeting, punkt: OParlAgendaItem, **felder: Any) -> OParlConsultation:
    basis = felder.pop("basis", RIS)
    return OParlConsultation.objects.create(
        external_id=_kennung("consultations", basis),
        body=vorlage.body,
        paper=vorlage,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=punkt.external_id,
        **felder,
    )


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Verlauf-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(external_id=_kennung("bodies"), source=source, name="Verlaufstadt")


def _sitzung(body: OParlBody, tage: int, gremium: str) -> tuple[OParlMeeting, OParlAgendaItem]:
    org = OParlOrganization.objects.create(external_id=_kennung("org"), body=body, name=gremium)
    sitzung = OParlMeeting.objects.create(
        external_id=_kennung("meetings"), body=body, start=timezone.now() + timedelta(days=tage)
    )
    sitzung.organizations.add(org)
    punkt = OParlAgendaItem.objects.create(
        external_id=_kennung("items"), meeting=sitzung, number="3", result="beschlossen" if tage < 0 else None
    )
    return sitzung, punkt


def test_beratungsverlauf_chronologisch_ohne_zurueckgenommene(body: OParlBody) -> None:
    vorlage = OParlPaper.objects.create(external_id=_kennung("papers"), body=body, name="Radweg")
    ausschuss, punkt_ausschuss = _sitzung(body, -10, "Bauausschuss")
    rat, punkt_rat = _sitzung(body, 5, "Rat")
    _beratung(vorlage, rat, punkt_rat, role="Entscheidung", authoritative=True)
    _beratung(vorlage, ausschuss, punkt_ausschuss, role="Vorberatung")
    zurueck = _beratung(vorlage, rat, punkt_rat, basis=SESSION)
    zurueck.deleted = True
    zurueck.save(update_fields=["deleted"])

    verlauf = ris.consultation_history(vorlage)

    assert [e["organization_name"] for e in verlauf] == ["Bauausschuss", "Rat"]
    erster = verlauf[0]
    assert erster["agenda_item"] == punkt_ausschuss and erster["agenda_number"] == "3"
    assert erster["result"] == "beschlossen" and erster["role"] == "Vorberatung" and erster["organization_count"] == 1
    assert verlauf[1]["authoritative"] is True and verlauf[1]["meeting"] == rat
    stand = paper_status(verlauf)
    assert (
        stand.text.startswith("Am ")
        and "im Bauausschuss beschlossen" in stand.text
        and "Nächste Beratung" in stand.text
    )
    assert ris.consultation_history(OParlPaper.objects.create(external_id=_kennung("papers"), body=body)) == []


def test_punkte_je_vorlage_und_vorlagen_je_punkt(body: OParlBody) -> None:
    eins = OParlPaper.objects.create(external_id=_kennung("papers"), body=body, name="Eins", reference="V/1")
    zwei = OParlPaper.objects.create(external_id=_kennung("papers"), body=body, name="Zwei", reference="V/2")
    weg = OParlPaper.objects.create(
        external_id=_kennung("papers", SESSION), body=body, name="Zurückgenommen", deleted=True
    )
    sitzung, punkt = _sitzung(body, 3, "Rat")
    _, anderer_punkt = _sitzung(body, 9, "Ausschuss")
    _beratung(eins, sitzung, punkt)
    _beratung(zwei, sitzung, punkt)
    _beratung(eins, sitzung, anderer_punkt)
    _beratung(weg, sitzung, punkt)

    assert ris.agenda_items_of_papers([eins.pk, zwei.pk, "kaputt"]) == {
        eins.pk: {punkt.pk, anderer_punkt.pk},
        zwei.pk: {punkt.pk},
    }
    assert ris.agenda_items_of_papers([]) == {}
    vorlagen = ris.papers_of_agenda_items([punkt, anderer_punkt])
    assert [v.name for v in vorlagen[punkt.pk]] == ["Eins", "Zwei"]
    assert [v.name for v in vorlagen[anderer_punkt.pk]] == ["Eins"]
    assert ris.papers_of_agenda_items([]) == {}


def test_vorlagen_je_punkt_in_einer_abfrage(body: OParlBody, django_assert_num_queries: Any) -> None:
    sitzung, _ = _sitzung(body, 3, "Rat")
    punkte = []
    for nummer in range(6):
        punkt = OParlAgendaItem.objects.create(external_id=_kennung("items"), meeting=sitzung, number=str(nummer))
        vorlage = OParlPaper.objects.create(external_id=_kennung("papers"), body=body, name=f"V{nummer}")
        _beratung(vorlage, sitzung, punkt)
        punkte.append(punkt)

    with django_assert_num_queries(1):
        assert len(ris.papers_of_agenda_items(punkte)) == 6


def test_beratung_ohne_bekannte_sitzung_steht_bei_jetzt_wie_in_insight(body: OParlBody) -> None:
    vorlage = OParlPaper.objects.create(external_id=_kennung("papers"), body=body, name="Radweg")
    ausschuss, punkt_ausschuss = _sitzung(body, -10, "Bauausschuss")
    rat, punkt_rat = _sitzung(body, 5, "Rat")
    _beratung(vorlage, rat, punkt_rat)
    OParlConsultation.objects.create(
        external_id=_kennung("consultations"),
        body=body,
        paper=vorlage,
        meeting_external_id=_kennung("meetings"),
        role="Kenntnisnahme",
    )
    _beratung(vorlage, ausschuss, punkt_ausschuss)

    verlauf = ris.consultation_history(vorlage)

    # Ohne Sitzung zwischen vergangenen und kommenden Beratungen, so wie die Vorgangsseite von Insight sie zeigte
    assert [e["organization_name"] for e in verlauf] == ["Bauausschuss", None, "Rat"]
    assert verlauf[1]["role"] == "Kenntnisnahme" and verlauf[1]["organization_count"] is None


def test_gremienzahl_aus_den_rohdaten_wenn_kein_gremium_benannt_ist(body: OParlBody) -> None:
    sitzung = OParlMeeting.objects.create(
        external_id=_kennung("meetings"),
        body=body,
        name="Ausschuss für Planung, Bau und Umwelt",
        raw_json={"organization": [_kennung("org")]},
    )
    assert ris.organization_count(sitzung) == 1
    sitzung.raw_json = {}
    assert ris.organization_count(sitzung) is None
    for name in ("A", "B", "C"):
        sitzung.organizations.add(OParlOrganization.objects.create(external_id=_kennung("org"), body=body, name=name))
    # Mehr als zwei zählen für den Stand-Satz nur als „mehrere“
    assert ris.organization_count(OParlMeeting.objects.prefetch_related("organizations").get(pk=sitzung.pk)) == 2


def test_vorgangsseite_von_insight_liest_den_verlauf_aus_der_fassade(
    body: OParlBody, client: Any, monkeypatch: Any
) -> None:
    vorlage = OParlPaper.objects.create(external_id=_kennung("papers"), body=body, name="Radweg")
    sitzung, punkt = _sitzung(body, -3, "Bauausschuss")
    _beratung(vorlage, sitzung, punkt)
    aufrufe: list[Any] = []
    original = ris.consultation_history

    def mitschreiben(paper: OParlPaper) -> list[dict[str, Any]]:
        aufrufe.append(paper)
        return original(paper)

    monkeypatch.setattr(ris, "consultation_history", mitschreiben)

    antwort = client.get(f"/insight/vorgaenge/{vorlage.pk}/")

    assert antwort.status_code == 200 and aufrufe == [vorlage]
    assert "im Bauausschuss" in antwort.content.decode()


def test_niederschrift_format_und_dateien_einer_sitzung(body: OParlBody) -> None:
    sitzung, _ = _sitzung(body, -3, "Rat")
    niederschrift = OParlFile.objects.create(external_id=_kennung("files"), body=body, meeting=sitzung, name="N")
    einladung = OParlFile.objects.create(external_id=_kennung("files"), body=body, meeting=sitzung, name="Einladung")
    OParlFile.objects.create(external_id=_kennung("files"), body=body, meeting=sitzung, name="Weg", deleted=True)
    fremd = OParlFile.objects.create(external_id=_kennung("files"), meeting=None, name="Fremd")
    anderer = OParlBody.objects.create(external_id=_kennung("bodies"), source=body.source, name="Andere Stadt")
    fremd.body = anderer
    fremd.save(update_fields=["body"])

    sitzung.raw_json = {"resultsProtocol": {"id": niederschrift.external_id}}
    assert ris.protocol_file(sitzung) == niederschrift
    # Eine Niederschrift aus einer anderen Kommune zeigt die Seite nicht
    sitzung.raw_json = {"verbatimProtocol": fremd.external_id}
    assert ris.protocol_file(sitzung) is None
    assert [d.name for d in ris.files_of_meeting(sitzung)] == ["Einladung", "N"]
    assert ris.files_of_meeting(sitzung, ohne=[niederschrift.pk, "kaputt"]) == [einladung]

    sitzung.raw_json = {"mandari:meetingFormat": "digital", "mandari:publicAccess": {"url": "javascript:alert(1)"}}
    info = ris.broadcast_info(sitzung)
    assert info is not None and info["label"].startswith("Digitale Sitzung") and info["url"] == ""
    sitzung.raw_json = {"mandari:meetingFormat": "praesenz"}
    assert ris.broadcast_info(sitzung) is None

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorlage am TOP auch über die Beratung, die der TOP nennt (Issue #1523).

Work findet die Vorlage eines TOP über den Rückverweis der Beratung (``agenda_item_external_id``). SD.NET RIM setzt
diesen Verweis erst nach dem Abgleich der Beratung, ohne ``modified`` zu ändern; der TOP nennt seine Beratung aber
(``AgendaItem.consultation``). Als Rückfall gilt diese Beratung, wenn sie selbst keinen TOP trägt, nicht gelöscht ist,
eine Vorlage hat und zur Quelle des TOP gehört (oder keine Kommune hat, Altbestand).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.utils import timezone

from apps.work.meetings import selectors
from insight_core.models import OParlAgendaItem, OParlBody, OParlConsultation, OParlMeeting, OParlPaper, OParlSource

pytestmark = pytest.mark.django_db

A = "https://ris.stadt-a.example/webservice/oparl/v1.1/body/1"
B = "https://ris.stadt-b.example/webservice/oparl/v1.1/body/1"


def _kennung(basis: str, art: str) -> str:
    return f"{basis}/{art}/{uuid.uuid4().hex}"


@pytest.fixture
def welt() -> SimpleNamespace:
    kommune_a = OParlBody.objects.create(
        external_id=A, source=OParlSource.objects.create(name="A", url=f"{A}/system"), name="Stadt A"
    )
    kommune_b = OParlBody.objects.create(
        external_id=B, source=OParlSource.objects.create(name="B", url=f"{B}/system"), name="Stadt B"
    )
    sitzung = OParlMeeting.objects.create(
        external_id=_kennung(B, "meeting"), body=kommune_b, name="Rat", start=timezone.now()
    )
    vorlage = OParlPaper.objects.create(external_id=_kennung(B, "paper"), body=kommune_b, name="Trinkbrunnen")
    fremde_vorlage = OParlPaper.objects.create(external_id=_kennung(A, "paper"), body=kommune_a, name="Radweg")
    beratung = OParlConsultation.objects.create(
        external_id=_kennung(B, "consultation"), body=kommune_b, paper=vorlage, meeting_external_id=sitzung.external_id
    )
    fremde = OParlConsultation.objects.create(
        external_id=_kennung(A, "consultation"), body=kommune_a, paper=fremde_vorlage
    )

    def punkt(nummer: int, nennt: str | None) -> OParlAgendaItem:
        raw = {"consultation": nennt} if nennt else {}
        return OParlAgendaItem.objects.create(
            external_id=_kennung(B, "agendaitem"), meeting=sitzung, number=str(nummer), order=nummer, raw_json=raw
        )

    return SimpleNamespace(
        kommune_a=kommune_a,
        kommune_b=kommune_b,
        sitzung=sitzung,
        vorlage=vorlage,
        beratung=beratung,
        fremde=fremde,
        punkt=punkt,
    )


def test_vorlage_ueber_die_beratung_die_der_top_nennt(welt: SimpleNamespace) -> None:
    punkt = welt.punkt(1, welt.beratung.external_id)

    assert selectors.get_primary_paper_for_item(punkt) == welt.vorlage
    vorgeladen = selectors.prefetch_papers_for_agenda_items([punkt])
    assert vorgeladen[punkt.id] == [welt.vorlage]
    assert vorgeladen[punkt.id][0]._prefetched_consultation_count == 1  # type: ignore[attr-defined]


def test_kein_rueckfall_fuer_fremde_geloeschte_oder_anders_verknuepfte_beratungen(welt: SimpleNamespace) -> None:
    fremd = welt.punkt(1, welt.fremde.external_id)
    anderer = welt.punkt(2, None)
    verknuepft = welt.punkt(3, welt.beratung.external_id)
    OParlConsultation.objects.filter(pk=welt.beratung.pk).update(agenda_item_external_id=anderer.external_id)

    assert selectors.get_primary_paper_for_item(fremd) is None
    assert selectors.get_primary_paper_for_item(verknuepft) is None  # die Beratung nennt einen anderen TOP
    assert selectors.get_primary_paper_for_item(anderer) == welt.vorlage
    vorgeladen = selectors.prefetch_papers_for_agenda_items([fremd, anderer, verknuepft])
    assert vorgeladen == {fremd.id: [], anderer.id: [welt.vorlage], verknuepft.id: []}

    OParlConsultation.objects.filter(pk=welt.beratung.pk).update(agenda_item_external_id=None, deleted=True)
    assert selectors.get_primary_paper_for_item(verknuepft) is None


def test_beratung_ohne_kommune_gilt_wie_im_altbestand(welt: SimpleNamespace) -> None:
    OParlConsultation.objects.filter(pk=welt.beratung.pk).update(body=None)
    punkt = welt.punkt(1, welt.beratung.external_id)
    assert selectors.get_primary_paper_for_item(punkt) == welt.vorlage


def test_vorladen_mit_rueckfall_ohne_abfrage_je_top(welt: SimpleNamespace, django_assert_max_num_queries: Any) -> None:
    einer = [welt.punkt(1, welt.beratung.external_id)]
    with django_assert_max_num_queries(10) as erster:
        selectors.prefetch_papers_for_agenda_items(einer)
    weitere = einer + [welt.punkt(n, welt.beratung.external_id) for n in range(2, 8)]
    with django_assert_max_num_queries(len(erster.captured_queries)):
        ergebnis = selectors.prefetch_papers_for_agenda_items(weitere)
    assert all(ergebnis[p.id] == [welt.vorlage] for p in weitere)


def test_quellenbindung_je_top_bei_gemischten_quellen(welt: SimpleNamespace) -> None:
    """Ein TOP aus Quelle A, der dieselbe Beratung aus B nennt wie ein TOP aus B, bekommt ihre Vorlage nicht."""
    sitzung_a = OParlMeeting.objects.create(
        external_id=_kennung(A, "meeting"), body=welt.kommune_a, name="Rat A", start=timezone.now()
    )
    punkt_a = OParlAgendaItem.objects.create(
        external_id=_kennung(A, "agendaitem"),
        meeting=sitzung_a,
        number="1",
        order=1,
        raw_json={"consultation": welt.beratung.external_id},
    )
    punkt_b = welt.punkt(1, welt.beratung.external_id)

    vorgeladen = selectors.prefetch_papers_for_agenda_items([punkt_a, punkt_b])
    assert vorgeladen == {punkt_a.id: [], punkt_b.id: [welt.vorlage]}
    assert selectors.get_primary_paper_for_item(punkt_a) is None

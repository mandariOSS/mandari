# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beratungen verbinden Vorlagen mit Sitzungen und Tagesordnungspunkten nur innerhalb einer Quelle.

Eine Beratung nennt Sitzung und Tagesordnungspunkt nur als Kennung der Quelle (Text). Der Bezug gilt nur, wenn
Sitzung bzw. Punkt zur selben Quelle gehören wie die Kommune der Beratung; Beratungen ohne Kommune (Altbestand)
gelten weiter. Geprüft in beide Richtungen: Vorlagen unter Sitzungen und Punkten (Sitzungsseiten, Tagesordnung,
Vorschau, Live, offene Schnittstelle) und Sitzungen und Punkte im Verlauf einer Vorlage.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.utils import timezone

from apps.work.meetings import selectors as work_meetings
from apps.work.ris import selectors as work_ris
from hub.live import selectors as live
from hub.ris import neuveroeffentlichung
from hub.ris import selectors as ris
from hub.ris.mapping.bestand import BestandMapping, RefContext
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
from insight_core.services import search_documents, search_projection
from insight_core.views.files import _annotate_files_with_context
from insight_core.views.home import zuletzt_beschlossen

pytestmark = pytest.mark.django_db

A = "https://ris.stadt-a.example/oparl"
B = "https://ris.stadt-b.example/oparl"
SITE = "https://mandari.example"


def _kennung(basis: str, art: str) -> str:
    return f"{basis}/{art}/{uuid.uuid4()}"


def _beratung(body: OParlBody | None, vorlage: OParlPaper, sitzung: OParlMeeting, punkt: OParlAgendaItem) -> Any:
    basis = A if body is not None and body.external_id.startswith(A) else B
    return OParlConsultation.objects.create(
        external_id=_kennung(basis, "consultation"),
        body=body,
        paper=vorlage,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=punkt.external_id,
    )


@pytest.fixture
def welt() -> SimpleNamespace:
    """
    Stadt B mit Gremium, einer kommenden und einer vergangenen Sitzung und ihren Punkten; Vorlage B und eine Vorlage
    aus dem Altbestand (Beratung ohne Kommune) unter Punkt 1. Quelle A nennt in den Beratungen ihrer Vorlage dieselbe
    Sitzung und Punkt 1, dazu Punkt 2 und den Punkt der vergangenen Sitzung, die sonst niemand berät.
    """
    jetzt = timezone.now()
    kommune_a = OParlBody.objects.create(
        external_id=f"{A}/body/1", source=OParlSource.objects.create(name="A", url=f"{A}/system"), name="Stadt A"
    )
    kommune_b = OParlBody.objects.create(
        external_id=f"{B}/body/1", source=OParlSource.objects.create(name="B", url=f"{B}/system"), name="Stadt B"
    )
    gremium = OParlOrganization.objects.create(external_id=_kennung(B, "organization"), body=kommune_b, name="Rat B")
    sitzung = OParlMeeting.objects.create(
        external_id=_kennung(B, "meeting"), body=kommune_b, name="Rat", start=jetzt + timedelta(days=5)
    )
    sitzung.organizations.add(gremium)
    vergangen = OParlMeeting.objects.create(
        external_id=_kennung(B, "meeting"), body=kommune_b, name="Rat", start=jetzt - timedelta(days=5)
    )
    vergangen.organizations.add(gremium)
    punkt_1 = OParlAgendaItem.objects.create(
        external_id=_kennung(B, "agendaitem"), meeting=sitzung, number="1", order=1, public=True
    )
    punkt_2 = OParlAgendaItem.objects.create(
        external_id=_kennung(B, "agendaitem"), meeting=sitzung, number="2", order=2, public=True
    )
    beschluss = OParlAgendaItem.objects.create(
        external_id=_kennung(B, "agendaitem"), meeting=vergangen, number="1", public=True, result="beschlossen"
    )
    vorlage_b = OParlPaper.objects.create(external_id=_kennung(B, "paper"), body=kommune_b, name="Radweg West")
    vorlage_alt = OParlPaper.objects.create(external_id=_kennung(B, "paper"), body=kommune_b, name="Radweg Nord")
    vorlage_a = OParlPaper.objects.create(external_id=_kennung(A, "paper"), body=kommune_a, name="Radweg Ost")
    _beratung(kommune_b, vorlage_b, sitzung, punkt_1)
    _beratung(None, vorlage_alt, sitzung, punkt_1)
    fremd = [
        _beratung(kommune_a, vorlage_a, sitzung, punkt_1),
        _beratung(kommune_a, vorlage_a, sitzung, punkt_2),
        _beratung(kommune_a, vorlage_a, vergangen, beschluss),
    ]
    datei_a = OParlFile.objects.create(
        external_id=_kennung(A, "file"),
        body=kommune_a,
        paper=vorlage_a,
        name="Anlage",
        text_content="Text",
        text_extraction_status="completed",
    )
    return SimpleNamespace(
        kommune_a=kommune_a,
        kommune_b=kommune_b,
        gremium=gremium,
        sitzung=sitzung,
        vergangen=vergangen,
        punkt_1=punkt_1,
        punkt_2=punkt_2,
        beschluss=beschluss,
        vorlage_a=vorlage_a,
        vorlage_b=vorlage_b,
        vorlage_alt=vorlage_alt,
        fremd=fremd,
        datei_a=datei_a,
    )


def test_vorlagen_unter_sitzungen_und_punkten_nur_aus_derselben_quelle(welt: SimpleNamespace) -> None:
    eigene = {welt.vorlage_b.pk, welt.vorlage_alt.pk}
    sitzung = OParlMeeting.objects.filter(pk=welt.sitzung.pk)

    assert {p.pk for p in ris.papers_of_agenda_item(welt.punkt_1)} == eigene
    assert not ris.papers_of_agenda_item(welt.punkt_2).exists()
    assert ris.paper_ids_of_agenda_items([welt.punkt_1.pk, welt.punkt_2.pk]) == eigene
    assert ris.paper_ids_on_upcoming_agendas([welt.kommune_b], until=timezone.now() + timedelta(days=30)) == eigene
    assert ris.agenda_overview([welt.sitzung.pk])[welt.sitzung.pk].with_paper == {welt.punkt_1.pk}
    assert {e.paper.pk for e in ris.papers_on_agendas(sitzung, limit=10)} == eigene
    je_punkt = ris.papers_of_agenda_items([welt.punkt_1, welt.punkt_2])
    assert {p.pk for p in je_punkt[welt.punkt_1.pk]} == eigene and welt.punkt_2.pk not in je_punkt
    tagesordnung = {e.item.pk: {p.pk for p in e.papers} for e in ris.public_agenda(welt.sitzung)}
    assert tagesordnung == {welt.punkt_1.pk: eigene, welt.punkt_2.pk: set()}
    assert {c.paper_id for c in welt.punkt_1.get_consultations()} == eigene
    assert list(welt.punkt_2.get_papers()) == []

    # Suche mit Gremium: beraten in einer Sitzung des Gremiums, nur innerhalb der Quelle
    gremium = [welt.gremium.pk]
    assert not ris.search_public_papers([welt.kommune_a], "Radweg", organizations=gremium).exists()
    assert {p.pk for p in ris.search_public_papers([welt.kommune_b], "Radweg", organizations=gremium)} == eigene


def test_work_live_neuveroeffentlichung_startseite_und_schnittstelle(welt: SimpleNamespace) -> None:
    eigene = {welt.vorlage_b.pk, welt.vorlage_alt.pk}

    assert work_meetings.get_primary_paper_for_item(welt.punkt_2) is None
    vorgeladen = work_meetings.prefetch_papers_for_agenda_items([welt.punkt_1, welt.punkt_2])
    assert {p.pk for p in vorgeladen[welt.punkt_1.pk]} == eigene and vorgeladen[welt.punkt_2.pk] == []
    assert live._vorlagen([SimpleNamespace(agenda_item=welt.punkt_2)]) == {}  # type: ignore[list-item]
    assert neuveroeffentlichung._vorlagen_je_top([welt.punkt_2.external_id]) == {}
    assert RefContext.for_agenda_items([welt.punkt_2]).consultations_by_agenda_ext == {}
    assert [p.vorgang for p in zuletzt_beschlossen(welt.kommune_b)] == [None]  # type: ignore[attr-defined]
    # Suchindex: Ändert sich Punkt 2, betrifft das keine Datei der Quelle A
    assert search_projection.context_file_ids("AgendaItem", [welt.punkt_2.pk]) == []
    assert search_projection.context_file_ids("Meeting", [welt.vergangen.pk]) == []


def test_verlauf_einer_vorlage_nennt_keine_sitzung_einer_anderen_quelle(welt: SimpleNamespace) -> None:
    vorlage = welt.vorlage_a

    verlauf = ris.consultation_history(vorlage)
    assert len(verlauf) == 3 and all(e["meeting"] is None and e["agenda_item"] is None for e in verlauf)
    assert ris.agenda_items_of_papers([vorlage.pk, welt.vorlage_b.pk]) == {welt.vorlage_b.pk: {welt.punkt_1.pk}}
    schritte = ris.public_consultation_history([vorlage.pk])[vorlage.pk]
    assert [(s.meeting_id, s.agenda_number) for s in schritte] == [(None, None)] * 3
    assert all(e["meeting"] is None and e["agenda_item"] is None for e in work_ris.enriched_consultations(vorlage))
    folge = work_meetings.resolve_consultations({vorlage.pk}, welt.sitzung)[vorlage.pk]
    assert [eintrag["meetingId"] for eintrag in folge] == [None] * 3
    assert search_documents.file_contexts([welt.datei_a]) == {welt.datei_a.pk: {}}
    # Gremien nennt die Beratung nur als Kennung: ein Gremium einer anderen Quelle zählt im Suchdokument nicht
    OParlConsultation.objects.filter(pk=welt.fremd[0].pk).update(raw_json={"organization": [welt.gremium.external_id]})
    assert search_documents.paper_to_doc(vorlage, files=[])["organization_names"] == []
    OParlConsultation.objects.filter(paper=welt.vorlage_b).update(raw_json={"organization": [welt.gremium.external_id]})
    assert search_documents.paper_to_doc(welt.vorlage_b, files=[])["organization_names"] == ["Rat B"]
    dateien = [welt.datei_a]
    _annotate_files_with_context(dateien)  # type: ignore[no-untyped-call]
    assert not dateien[0].context_info

    # Offene Schnittstelle: Die Beratung nennt weder Sitzung noch Punkt einer anderen Quelle
    mapping = BestandMapping(f"{SITE}/oparl", SITE)
    kontext = RefContext.for_consultations(welt.fremd)
    for beratung in welt.fremd:
        objekt = mapping.consultation(beratung, kontext)
        assert "meeting" not in objekt and "agendaItem" not in objekt

    # Gegenprobe in derselben Quelle und im Altbestand: Sitzung und Punkt sind bekannt
    eigener = ris.consultation_history(welt.vorlage_b)
    assert [(e["meeting"], e["agenda_item"]) for e in eigener] == [(welt.sitzung, welt.punkt_1)]
    alter = ris.consultation_history(welt.vorlage_alt)
    assert [(e["meeting"], e["agenda_item"]) for e in alter] == [(welt.sitzung, welt.punkt_1)]

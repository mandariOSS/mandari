# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beratungen verbinden Vorlagen mit Sitzungen und Tagesordnungspunkten nur innerhalb einer Quelle.

Eine Beratung nennt Sitzung und Tagesordnungspunkt nur als Kennung der Quelle (Text). Der Bezug gilt nur, wenn
Sitzung bzw. Punkt zur selben Quelle gehören wie die Kommune der Beratung; Beratungen ohne Kommune (Altbestand)
gelten weiter. Geprüft in beide Richtungen: Vorlagen unter Sitzungen und Punkten (Sitzungsseite, Work, offene
Schnittstelle) und Sitzungen und Punkte im Verlauf einer Vorlage (Vorgangsseite, Dokumente, Suche, KI, Work).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.work.meetings import selectors as work_meetings
from apps.work.ris import selectors as work_ris
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
from insight_core.services import search_documents
from insight_core.views import files as file_views
from insight_core.views.papers import PaperDetailView
from oparl_api import serializers as oparl_serializers

pytestmark = pytest.mark.django_db

A = "https://ris.stadt-a.example/oparl"
B = "https://ris.stadt-b.example/oparl"

# Ohne Typangaben (mypy: no-untyped-call)
RefContext = cast(Any, oparl_serializers.RefContext)
serialize_consultation = cast(Any, oparl_serializers.serialize_consultation)
_annotate_files_with_context = cast(Any, file_views._annotate_files_with_context)


def _verlauf(vorlage: OParlPaper) -> list[dict[str, Any]]:
    """Beratungsverlauf der Vorgangsseite im Bürgerportal."""
    return cast(list[dict[str, Any]], cast(Any, PaperDetailView())._get_consultations_with_meetings(vorlage))


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
        external_id=f"{A}/body/1",
        source=OParlSource.objects.create(name="A", url=f"{A}/system"),
        name="Stadt A",
        slug="stadt-a",
    )
    kommune_b = OParlBody.objects.create(
        external_id=f"{B}/body/1",
        source=OParlSource.objects.create(name="B", url=f"{B}/system"),
        name="Stadt B",
        slug="stadt-b",
    )
    gremium = OParlOrganization.objects.create(
        external_id=_kennung(B, "organization"), body=kommune_b, name="Rat der Stadt B"
    )
    sitzung = OParlMeeting.objects.create(
        external_id=_kennung(B, "meeting"), body=kommune_b, name="Ratssitzung", start=jetzt + timedelta(days=5)
    )
    sitzung.organizations.add(gremium)
    vergangen = OParlMeeting.objects.create(
        external_id=_kennung(B, "meeting"), body=kommune_b, name="Ratssitzung", start=jetzt - timedelta(days=5)
    )
    vergangen.organizations.add(gremium)
    punkt_1 = OParlAgendaItem.objects.create(
        external_id=_kennung(B, "agendaitem"), meeting=sitzung, number="1", order=1, public=True
    )
    punkt_2 = OParlAgendaItem.objects.create(
        external_id=_kennung(B, "agendaitem"), meeting=sitzung, number="2", order=2, public=True
    )
    beschluss = OParlAgendaItem.objects.create(
        external_id=_kennung(B, "agendaitem"), meeting=vergangen, number="7", public=True, result="beschlossen"
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
        text_content="Text der Anlage",
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

    assert {c.paper_id for c in welt.punkt_1.get_consultations()} == eigene
    assert {p.pk for p in welt.punkt_1.get_papers()} == eigene
    assert list(welt.punkt_2.get_papers()) == []

    # Sitzungsseite im Bürgerportal
    seite = Client().get(f"/insight/termine/{welt.sitzung.id}/").content.decode()
    assert "Radweg West" in seite and "Radweg Nord" in seite
    assert "Radweg Ost" not in seite

    # Work: Sitzungsvorbereitung
    assert work_meetings.get_primary_paper_for_item(welt.punkt_2) is None
    vorgeladen = work_meetings.prefetch_papers_for_agenda_items([welt.punkt_1, welt.punkt_2])
    assert {p.pk for p in vorgeladen[welt.punkt_1.pk]} == eigene and vorgeladen[welt.punkt_2.pk] == []

    # Offene Schnittstelle: Ein TOP nennt keine Beratung einer anderen Quelle
    assert RefContext.for_agenda_items([welt.punkt_2]).consultations_by_agenda_ext == {}
    beratungen = RefContext.for_meetings([welt.sitzung]).consultations_by_agenda_ext
    assert len(beratungen[welt.punkt_1.external_id]) == 2 and welt.punkt_2.external_id not in beratungen


def test_verlauf_einer_vorlage_nennt_keine_sitzung_einer_anderen_quelle(welt: SimpleNamespace) -> None:
    vorlage = welt.vorlage_a

    # Vorgangsseite im Bürgerportal
    verlauf = _verlauf(vorlage)
    assert len(verlauf) == 3 and all(e["meeting"] is None and e["agenda_item"] is None for e in verlauf)
    seite = Client().get(f"/insight/vorgaenge/{vorlage.id}/").content.decode()
    assert f"/insight/termine/{welt.sitzung.id}/" not in seite
    assert f"/insight/termine/{welt.vergangen.id}/" not in seite

    # Work: RIS-Vorgang und Beratungsfolge in der Sitzungsvorbereitung
    assert all(e["meeting"] is None and e["agenda_item"] is None for e in work_ris.enriched_consultations(vorlage))
    folge = work_meetings.resolve_consultations({vorlage.pk}, welt.sitzung)[vorlage.pk]
    assert [eintrag["meetingId"] for eintrag in folge] == [None] * 3

    # Dokumente, Suchindex
    dateien = [welt.datei_a]
    _annotate_files_with_context(dateien)
    assert not dateien[0].context_info
    dokument = search_documents.file_to_doc(welt.datei_a)
    assert dokument["meeting_name"] is None and dokument["organization_names"] == []
    # Gremien nennt die Beratung nur als Kennung: ein Gremium einer anderen Quelle zählt im Suchdokument nicht
    OParlConsultation.objects.filter(pk=welt.fremd[0].pk).update(raw_json={"organization": [welt.gremium.external_id]})
    assert search_documents.paper_to_doc(vorlage, files=[])["organization_names"] == []
    OParlConsultation.objects.filter(paper=welt.vorlage_b).update(raw_json={"organization": [welt.gremium.external_id]})
    assert search_documents.paper_to_doc(welt.vorlage_b, files=[])["organization_names"] == ["Rat der Stadt B"]

    # Offene Schnittstelle: Die Beratung nennt weder Sitzung noch Punkt einer anderen Quelle
    kontext = RefContext.for_consultations(welt.fremd)
    for beratung in welt.fremd:
        objekt = serialize_consultation(beratung, kontext)
        assert "meeting" not in objekt and "agendaItem" not in objekt

    # Gegenprobe in derselben Quelle und im Altbestand: Sitzung und Punkt sind bekannt
    for eigene in (welt.vorlage_b, welt.vorlage_alt):
        eintraege = _verlauf(eigene)
        assert [(e["meeting"], e["agenda_item"]) for e in eintraege] == [(welt.sitzung, welt.punkt_1)]
        kontext = RefContext.for_papers([eigene])
        objekt = serialize_consultation(eigene.consultations.get(), kontext)
        assert "meeting" in objekt and "agendaItem" in objekt


@dataclass
class _Antwort:
    content: str
    input_tokens: int = 1
    output_tokens: int = 1


class _FakeProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return True

    def chat_completion(self, messages: list[Any], **kwargs: Any) -> _Antwort:
        self.prompts.append("\n".join(m.content for m in messages))
        return _Antwort("Kurzfassung")


def test_zusammenfassung_nennt_kein_gremium_einer_anderen_quelle(welt: SimpleNamespace) -> None:
    from insight_ai.services.summarizer import SummaryService

    provider = _FakeProvider()
    cast(Any, SummaryService)(provider=provider).generate_summary(welt.vorlage_a, save=False)
    assert "Rat der Stadt B" not in provider.prompts[0]

    # Gegenprobe: Vorlage der Stadt B nennt ihr Gremium
    OParlFile.objects.create(
        external_id=_kennung(B, "file"),
        body=welt.kommune_b,
        paper=welt.vorlage_b,
        name="Anlage B",
        text_content="Text der Anlage B",
        text_extraction_status="completed",
    )
    cast(Any, SummaryService)(provider=provider).generate_summary(welt.vorlage_b, save=False)
    assert "Rat der Stadt B" in provider.prompts[1]

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Musterstadt-Bestand in Insight (``setup_demo_insight``, Issue #99).

Die öffentliche Demo soll aussehen wie ein echtes Ratsinformationssystem: ein Jahr Sitzungen, Vorlagen mit
Beratungsfolgen, Beschlüsse mit Abstimmung und Umsetzungsstand, Dateien und Orte. Der Bestand folgt dem Plan aus
``apps.common.demo_stadt``; ein wiederholter Lauf (Neuaufbau alle 12 Stunden) darf nichts doppelt anlegen.
"""

from __future__ import annotations

from datetime import date
from io import StringIO
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command

from apps.common.demo_daten import demo_ext
from apps.common.demo_stadt import BESTAND, PERSONEN, stadtplan, top_kennung
from insight_core.models import (
    OParlAgendaItem,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlPaper,
    OParlPerson,
    PaperLocation,
    VorgangUrheber,
)

AUFBAUTAG = date(2026, 11, 11)

pytestmark = pytest.mark.django_db


def _bestand(art: str) -> str:
    return demo_ext(art, BESTAND)


@pytest.fixture
def musterstadt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Basisdemo mit festem Aufbautag (Umfang klein aus settings_test)."""
    from apps.common.management.commands import setup_demo_environment as basisdemo

    monkeypatch.setattr(basisdemo, "aufbautag", lambda: AUFBAUTAG)
    call_command("setup_demo_environment", stdout=StringIO())


def _zaehlen() -> dict[str, int]:
    return {
        "personen": OParlPerson.objects.filter(external_id__startswith=demo_ext("person", "")).count(),
        "mitgliedschaften": OParlMembership.objects.filter(external_id__startswith=_bestand("membership")).count(),
        "sitzungen": OParlMeeting.objects.filter(external_id__startswith=_bestand("meeting")).count(),
        "tops": OParlAgendaItem.objects.filter(external_id__startswith=_bestand("agendaitem")).count(),
        "vorlagen": OParlPaper.objects.filter(external_id__startswith=_bestand("paper")).count(),
        "beratungen": OParlConsultation.objects.filter(external_id__startswith=_bestand("consultation")).count(),
        "dateien": OParlFile.objects.filter(external_id__startswith=_bestand("file")).count(),
        "orte": PaperLocation.objects.filter(paper__external_id__startswith=_bestand("paper")).count(),
    }


def test_bestand_folgt_dem_plan_und_wiederholung_verdoppelt_nichts(musterstadt: None) -> None:
    plan = stadtplan(AUFBAUTAG, "klein")
    vorher = _zaehlen()
    assert vorher["personen"] == len(PERSONEN)
    assert vorher["vorlagen"] == len(plan.vorgaenge) > 0
    assert vorher["sitzungen"] == sum(1 for s in plan.sitzungen if not s.basis)
    assert vorher["tops"] == sum(len(s.tops) for s in plan.sitzungen)
    assert vorher["orte"] == sum(len(v.orte) for v in plan.vorgaenge)

    call_command("setup_demo_insight", aufbautag=AUFBAUTAG, umfang="klein", stdout=StringIO())
    assert _zaehlen() == vorher


def test_drucksachen_haben_text_datei_und_beratungsfolge(musterstadt: None) -> None:
    plan = stadtplan(AUFBAUTAG, "klein")
    vorgang = plan.vorgaenge[0]
    paper = OParlPaper.objects.get(external_id=demo_ext("paper", vorgang.key))
    assert paper.reference == vorgang.nummer and paper.name == vorgang.titel
    datei = paper.files.get()
    assert vorgang.beschluss in (datei.text_content or "")
    assert (Path(settings.MEDIA_ROOT) / "demo" / (datei.file_name or "")).read_bytes().startswith(b"%PDF")
    assert OParlConsultation.objects.filter(paper=paper).exists()


def test_sitzungen_der_basisdemo_bekommen_weitere_tops(musterstadt: None) -> None:
    """Die kommende Ratssitzung (rat-3) hat ihre vier Geschichten-TOPs und dahinter TOPs aus dem Bestand."""
    plan = stadtplan(AUFBAUTAG, "klein")
    rat3 = next((s for s in plan.sitzungen if s.key == "rat-3"), None)
    if rat3 is None:
        pytest.skip("Im kleinen Umfang hat rat-3 an diesem Tag keine Bestands-TOPs")
    meeting = OParlMeeting.objects.get(external_id=demo_ext("meeting", "rat-3"))
    nummern = list(meeting.agenda_items.order_by("order").values_list("number", flat=True))
    assert nummern[:4] == ["1", "2", "3", "4"]
    assert nummern[4:] == [t.nummer for t in rat3.tops]
    erster = OParlAgendaItem.objects.get(external_id=demo_ext("agendaitem", top_kennung(rat3, rat3.tops[0])))
    assert erster.meeting_id == meeting.pk


def test_vergangene_entscheidungen_haben_ergebnis_und_abstimmung(musterstadt: None) -> None:
    entschieden = OParlAgendaItem.objects.filter(
        external_id__startswith=_bestand("agendaitem"), vote_result__in=["approved", "rejected"]
    )
    assert entschieden.exists()
    for top in entschieden:
        assert top.meeting.start is not None and top.meeting.start.date() < AUFBAUTAG
        assert top.votes_yes is not None and top.result
    kommend = OParlAgendaItem.objects.filter(
        external_id__startswith=_bestand("agendaitem"), meeting__start__date__gt=AUFBAUTAG
    )
    assert not kommend.exclude(vote_result=None).exists()


def test_voller_umfang(musterstadt: None) -> None:
    """Der volle Bestand der öffentlichen Demo: viele Sitzungen, Vorlagen, Urheber-Fraktionen und Umsetzungsstände."""
    call_command("setup_demo_insight", aufbautag=AUFBAUTAG, umfang="voll", stdout=StringIO())
    plan = stadtplan(AUFBAUTAG, "voll")
    zahlen = _zaehlen()
    assert zahlen["vorlagen"] == len(plan.vorgaenge) >= 150
    assert zahlen["sitzungen"] >= 100
    assert zahlen["tops"] >= 600
    # Anträge und Anfragen tragen die Fraktion im Titel; Insight ermittelt daraus die Urheber wie im Betrieb
    assert (
        VorgangUrheber.objects.filter(
            paper_id__in=OParlPaper.objects.filter(external_id__startswith=_bestand("paper")).values("pk")
        ).count()
        >= 40
    )
    assert OParlAgendaItem.objects.filter(implementation_status="done").exists()
    namentlich = OParlAgendaItem.objects.filter(vote_method="roll_call").first()
    assert namentlich is not None and namentlich.roll_call
    assert len(namentlich.roll_call) == (namentlich.votes_yes or 0) + (namentlich.votes_no or 0) + (
        namentlich.votes_abstain or 0
    )

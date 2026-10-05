# SPDX-License-Identifier: AGPL-3.0-or-later
"""„Im Beratungsverlauf“ in der Sitzungsvorbereitung: Positionen aus anderen Gremien zur selben Vorlage."""

import datetime
from typing import Any

import pytest

from apps.work.meetings.models import AgendaItemPosition
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
)

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org"


def test_beratungsverlauf_zeigt_datum_lesbar_und_sortiert_nach_iso(org: Any) -> None:
    source = OParlSource.objects.create(name="Test-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Stadt Test")
    ausschuss = OParlOrganization.objects.create(
        external_id=f"{RIS}/org/1", body=body, name="Ausschuss", short_name="AUKB"
    )
    vorberatung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/1",
        body=body,
        name="Ausschuss",
        start=datetime.datetime(2026, 9, 22, 14, 50, tzinfo=datetime.UTC),
    )
    vorberatung.organizations.add(ausschuss)
    rat = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/2", body=body, name="Rat")
    top_ausschuss = OParlAgendaItem.objects.create(
        external_id=f"{RIS}/agenda/1", meeting=vorberatung, number="3", order=3
    )
    top_rat = OParlAgendaItem.objects.create(external_id=f"{RIS}/agenda/2", meeting=rat, number="7", order=7)
    vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Radweg")
    for nr, top in enumerate((top_ausschuss, top_rat), start=1):
        OParlConsultation.objects.create(
            external_id=f"{RIS}/consultation/{nr}", body=body, paper=vorlage, agenda_item_external_id=top.external_id
        )
    AgendaItemPosition.objects.create(organization=org, agenda_item=top_ausschuss, position="for")

    eintraege = AgendaItemPosition.get_cross_positions_for_items(org, [top_rat])[top_rat.id]

    assert len(eintraege) == 1
    eintrag = eintraege[0]
    assert eintrag["gremium"] == "AUKB"
    assert eintrag["datum_display"] == "22.09.2026"
    assert eintrag["datum"].startswith("2026-09-22T14:50")

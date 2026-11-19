# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungen, die das RIS nur „Sitzung“ nennt (Münster), heißen überall nach ihrem Gremium (Issue #1508).

Der Sitzungskalender in Work zeigte an jeder Sitzung „17 Uhr Sitzung“, weil er zuerst den Namen der Sitzung nahm.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.work.meetings.serializers import serialize_calendar_event
from apps.work.motions.references import meeting_label
from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlSource
from insight_core.services.search_documents import meeting_to_doc

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org"


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Musterstadt", url=f"{RIS}/system")
    return OParlBody.objects.create(source=source, external_id=f"{RIS}/body/1", name="Musterstadt")


def _sitzung(body: OParlBody, nr: int, name: str | None, gremium: OParlOrganization | None) -> OParlMeeting:
    sitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/{nr}",
        body=body,
        name=name,
        start=datetime(2027, 4, 6, 15, 0, tzinfo=UTC),
        raw_json={"organization": [gremium.external_id]} if gremium else {},
    )
    if gremium:
        sitzung.organizations.add(gremium)
    return sitzung


def test_eigener_name_nur_wenn_er_mehr_sagt_als_sitzung(body: OParlBody) -> None:
    assert _sitzung(body, 1, "Sitzung", None).eigener_name() == ""
    assert _sitzung(body, 2, " sitzung ", None).eigener_name() == ""
    assert _sitzung(body, 3, None, None).eigener_name() == ""
    assert _sitzung(body, 4, "Sondersitzung zum Haushalt", None).eigener_name() == "Sondersitzung zum Haushalt"


def test_kalender_suche_und_bezuege_nennen_das_gremium(body: OParlBody) -> None:
    ausschuss = OParlOrganization.objects.create(
        external_id=f"{RIS}/organization/160", body=body, name="Kulturausschuss", short_name="KA"
    )
    sitzung = _sitzung(body, 10, "Sitzung", ausschuss)

    ereignis = serialize_calendar_event(sitzung, "fraktion")
    assert ereignis["title"] == "Kulturausschuss" and ereignis["extendedProps"]["committee"] == "Kulturausschuss"
    assert meeting_label(sitzung) == "Kulturausschuss, 06.04.2027"
    assert meeting_to_doc(sitzung)["name"] == "Kulturausschuss"

    # Ein eigener Name geht vor, auch mit Gremium
    sonder = _sitzung(body, 12, "Sondersitzung zum Haushalt", ausschuss)
    assert sonder.bezeichnung() == "Sondersitzung zum Haushalt" == serialize_calendar_event(sonder, "fraktion")["title"]
    assert meeting_label(sonder) == "Sondersitzung zum Haushalt, 06.04.2027"

    # Ohne Gremium und ohne eigenen Namen bleibt es bei „Sitzung“
    assert serialize_calendar_event(_sitzung(body, 11, "Sitzung", None), "fraktion")["title"] == "Sitzung"

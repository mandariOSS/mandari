# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gremiensortierung ohne Join über alle Sitzungen (Issue #1415).

``last_meeting_date`` war ``Max("meetings__start")``: Die Liste wurde über alle Sitzungen gruppiert, die
Mitglieder-Unterabfrage lief je Sitzung statt je Gremium (Köln: 7 s je Aufruf).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlSource
from insight_core.ranking import INACTIVITY_PENALTY, sort_organizations_by_ranking

pytestmark = pytest.mark.django_db


@pytest.fixture
def body() -> OParlBody:
    source = OParlSource.objects.create(name="Beispielstadt", url="https://ris.example/oparl")
    return OParlBody.objects.create(
        external_id="https://ris.example/oparl/body/1", source=source, name="Beispielstadt", slug="beispielstadt"
    )


def _gremium(body: OParlBody, name: str, *sitzungen_vor_tagen: int | None) -> OParlOrganization:
    gremium = OParlOrganization.objects.create(body=body, external_id=f"{body.external_id}/org/{name}", name=name)
    for nummer, tage in enumerate(sitzungen_vor_tagen):
        sitzung = OParlMeeting.objects.create(
            body=body,
            external_id=f"{gremium.external_id}/meeting/{nummer}",
            name=f"{name} {nummer}",
            start=None if tage is None else timezone.now() - timedelta(days=tage),
        )
        sitzung.organizations.add(gremium)
    return gremium


def test_letzte_sitzung_und_reihenfolge(body: OParlBody) -> None:
    jetzt = timezone.now()
    _gremium(body, "Rat", 3, 40, None, -10)
    _gremium(body, "Ausschuss für Umwelt", 800)
    _gremium(body, "Ausschuss für Bau", 5, 2)
    _gremium(body, "Beirat ohne Sitzung")

    qs = cast(Any, sort_organizations_by_ranking)(OParlOrganization.objects.filter(body=body))
    assert "GROUP BY" not in str(qs.query), "kein Aggregat über den Join mit den Sitzungen"
    zeilen = {o.name: o for o in qs}

    rat = zeilen["Rat"].last_meeting_date
    assert rat is not None and abs(rat - (jetzt + timedelta(days=10))) < timedelta(minutes=1), (
        "späteste Sitzung, auch künftige; ohne Beginn zählt nicht"
    )
    assert zeilen["Beirat ohne Sitzung"].last_meeting_date is None
    assert zeilen["Ausschuss für Umwelt"].inactivity_penalty == INACTIVITY_PENALTY
    assert zeilen["Beirat ohne Sitzung"].inactivity_penalty == INACTIVITY_PENALTY
    assert zeilen["Ausschuss für Bau"].inactivity_penalty == 0
    namen = [o.name for o in qs]
    assert namen.index("Rat") < namen.index("Ausschuss für Bau") < namen.index("Ausschuss für Umwelt")


def test_gremienliste_zeigt_jedes_gremium_einmal(body: OParlBody) -> None:
    _gremium(body, "Rat", 1, 2, 3, 4)
    _gremium(body, "Ausschuss für Bau", 5, 6)
    response = Client().get("/insight/k/beispielstadt/gremien/?tab=all")
    assert response.status_code == 200
    gremien = list(response.context["organizations"])
    assert [g.name for g in gremien] == ["Rat", "Ausschuss für Bau"]

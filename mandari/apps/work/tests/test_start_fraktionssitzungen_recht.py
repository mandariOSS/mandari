# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionssitzungen auf der Startseite von Work nur mit dem Recht ``faction.view_public``.

Liste, Detail und Kalender-Feed der Fraktionssitzungen verlangen das Recht. Die Startseite verlangt nur
``dashboard.view`` und nennt die nächsten Fraktionssitzungen deshalb nur, wenn das Mitglied sie auch dort sehen
dürfte: in beiden Erscheinungsbildern, auch wenn das Recht einzeln verweigert ist.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import PermissionFactory
from apps.work.faction.models import FactionMeeting

pytestmark = pytest.mark.django_db

TITEL = "Klausur zur Haushaltsstrategie"
ORT = "Sitzungssaal Nordflügel"
SATZ = "Die nächste interne Sitzung ist am"


@pytest.fixture
def sitzung(org: Any, make_member: Any) -> FactionMeeting:
    leitung = make_member(org, ["dashboard.view", "faction.view_public", "faction.manage"])
    return FactionMeeting.objects.create(
        organization=org,
        title=TITEL,
        location=ORT,
        start=timezone.now() + timedelta(days=2),
        status="planned",
        created_by=leitung,
    )


def _start(client_for: Any, org: Any, member: Any, *, neu: bool) -> str:
    org.work_new_design = neu
    org.save(update_fields=["work_new_design"])
    response = client_for(member.user).get(reverse("work:dashboard", kwargs={"org_slug": org.slug}))
    assert response.status_code == 200
    return cast(str, response.content.decode())


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_ohne_recht_keine_fraktionssitzung_auf_der_startseite(
    org: Any, make_member: Any, client_for: Any, sitzung: FactionMeeting, neu: bool
) -> None:
    ohne = make_member(org, ["dashboard.view"], email="ohne-recht@example.org")

    html = _start(client_for, org, ohne, neu=neu)

    assert TITEL not in html and ORT not in html
    assert SATZ not in html


@pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neu"])
def test_mit_recht_bleibt_die_fraktionssitzung_auf_der_startseite(
    org: Any, make_member: Any, client_for: Any, sitzung: FactionMeeting, neu: bool
) -> None:
    mit = make_member(org, ["dashboard.view", "faction.view_public"], email="mit-recht@example.org")

    html = _start(client_for, org, mit, neu=neu)

    assert TITEL in html
    if neu:
        assert SATZ in html


def test_einzeln_verweigertes_recht_gilt_auch_auf_der_startseite(
    org: Any, make_member: Any, client_for: Any, sitzung: FactionMeeting
) -> None:
    mitglied = make_member(org, ["dashboard.view", "faction.view_public"], email="verweigert@example.org")
    mitglied.denied_permissions.add(cast(Any, PermissionFactory)(codename="faction.view_public"))

    html = _start(client_for, org, mitglied, neu=True)

    assert TITEL not in html and SATZ not in html

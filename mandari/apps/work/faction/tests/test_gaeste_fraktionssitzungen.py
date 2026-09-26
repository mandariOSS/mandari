# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gastzugänge gehören nicht zu Fraktionssitzungen.

Gäste sehen nur freigegebene Dokumente: Sie werden weder als Teilnehmende angelegt noch
eingeladen, und der persönliche Kalender-Feed enthält Fraktionssitzungen nur aus
Organisationen, in denen die Person sie sehen darf.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.faction.feeds import build_personal_feed
from apps.work.faction.models import FactionAttendance, FactionMeeting
from apps.work.faction.services import FactionMeetingEmailService


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    return make_member(org, ["faction.view_public", "faction.create", "faction.manage"], email="vorsitz@example.org")


@pytest.fixture
def gast(org: Any) -> Any:
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    return MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]


@pytest.mark.django_db
def test_neue_sitzung_ohne_gastzugaenge(org: Any, vorsitz: Any, gast: Any, client_for: Any) -> None:
    morgen = (timezone.now() + timedelta(days=1)).date().isoformat()
    client_for(vorsitz.user).post(
        reverse("work:faction", kwargs={"org_slug": org.slug}),
        {"title": "Fraktionssitzung", "start_date": morgen, "start_time": "18:00"},
    )

    meeting = FactionMeeting.objects.get(organization=org, title="Fraktionssitzung")
    teilnehmende = set(FactionAttendance.objects.filter(meeting=meeting).values_list("membership_id", flat=True))
    assert vorsitz.id in teilnehmende
    assert gast.id not in teilnehmende


@pytest.mark.django_db
def test_einladung_geht_nicht_an_gaeste(org: Any, vorsitz: Any, gast: Any) -> None:
    meeting = FactionMeeting.objects.create(
        organization=org, title="Sitzung", start=timezone.now() + timedelta(days=2), created_by=vorsitz
    )
    # Bestand: Anwesenheit eines Gastzugangs aus früherer Anlage
    FactionAttendance.objects.create(meeting=meeting, membership=gast, status="invited")
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="invited")

    FactionMeetingEmailService().send_invitations(meeting)

    empfaenger = {adresse for nachricht in mail.outbox for adresse in nachricht.to}
    assert "gast@example.org" not in empfaenger
    assert "vorsitz@example.org" in empfaenger


@pytest.mark.django_db
def test_kalender_feed_ohne_sitzungen_fuer_gaeste_und_ohne_recht(
    org: Any, make_member: Any, vorsitz: Any, gast: Any
) -> None:
    ohne_recht = make_member(org, ["dashboard.view"], email="ohne@example.org")
    FactionMeeting.objects.create(
        organization=org,
        title="Vertrauliche Klausur",
        start=timezone.now() + timedelta(days=3),
        status="planned",
        created_by=vorsitz,
    )

    assert b"Vertrauliche Klausur" in build_personal_feed(vorsitz.user)
    assert b"Vertrauliche Klausur" not in build_personal_feed(gast.user)
    assert b"Vertrauliche Klausur" not in build_personal_feed(ohne_recht.user)

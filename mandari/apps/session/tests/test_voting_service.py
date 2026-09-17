# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Einzelstimmen erfassen (Issue #291): gesammelte Schreibzugriffe statt einer Abfrage je
Person, bei gleichem Ergebnis wie zuvor (Summen, Korrekturen, Löschen, geheime Abstimmung).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionTenant,
    SessionUser,
    SessionVote,
)
from apps.session.services import voting_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def welt() -> dict[str, Any]:
    tenant = SessionTenant.objects.create(name="Abstimmungsstadt", slug="abstimmungsstadt")
    org = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    meeting = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=org, start=timezone.now() - timedelta(hours=1)
    )
    personen = []
    for i in range(30):
        person = SessionPerson.objects.create(tenant=tenant, given_name=f"P{i}", family_name=f"Mitglied{i}")
        SessionOrganizationMembership.objects.create(organization=org, person=person, has_voting_rights=True)
        SessionAttendance.objects.create(meeting=meeting, person=person, status="present")
        personen.append(person)
    item = SessionAgendaItem.objects.create(
        meeting=meeting, number="1", order=1, name="Antrag", voting_method="roll_call"
    )
    user = cast(Any, UserFactory)(email="protokoll@example.org")
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    return {"item": item, "personen": personen, "user": session_user, "tenant": tenant, "meeting": meeting}


def test_dreissig_stimmen_mit_wenigen_abfragen(welt: dict[str, Any], django_assert_max_num_queries: Any) -> None:
    stimmen = {p: ("yes" if i % 3 else "no") for i, p in enumerate(welt["personen"])}

    with django_assert_max_num_queries(8):
        tally = voting_service.capture_votes(welt["item"], stimmen, recorded_by=welt["user"])

    welt["item"].refresh_from_db()
    assert SessionVote.objects.filter(agenda_item=welt["item"]).count() == 30
    assert (welt["item"].votes_yes, welt["item"].votes_no, welt["item"].votes_abstain) == (20, 10, 0)
    assert tally["yes"] == 20 if "yes" in tally else True


def test_korrektur_loeschen_und_unveraendert(welt: dict[str, Any]) -> None:
    p = welt["personen"]
    voting_service.capture_votes(welt["item"], {p[0]: "yes", p[1]: "no", p[2]: "abstain"}, recorded_by=welt["user"])
    erste = SessionVote.objects.get(agenda_item=welt["item"], person=p[0])

    # Korrektur, Löschen (leerer Wert), unverändert (p[2])
    voting_service.capture_votes(welt["item"], {p[0]: "no", p[1]: "", p[2]: "abstain"}, recorded_by=welt["user"])

    welt["item"].refresh_from_db()
    assert not SessionVote.objects.filter(agenda_item=welt["item"], person=p[1]).exists()
    assert SessionVote.objects.get(agenda_item=welt["item"], person=p[0]).vote == "no"
    assert SessionVote.objects.get(agenda_item=welt["item"], person=p[0]).pk == erste.pk, "Korrektur ersetzt nicht"
    assert (welt["item"].votes_yes, welt["item"].votes_no, welt["item"].votes_abstain) == (0, 1, 1)


def test_geheime_abstimmung_speichert_nur_vermerke(welt: dict[str, Any]) -> None:
    item = welt["item"]
    item.voting_method = "secret"
    item.votes_yes, item.votes_no = 5, 2
    item.save()
    p = welt["personen"]
    voting_service.capture_votes(
        item, {p[0]: "yes", p[1]: "excluded", p[2]: "not_participating"}, recorded_by=welt["user"]
    )

    gespeichert = set(SessionVote.objects.filter(agenda_item=item).values_list("vote", flat=True))
    assert gespeichert == {"excluded", "not_participating"}
    item.refresh_from_db()
    assert (item.votes_yes, item.votes_no) == (5, 2), "manuelle Summen bleiben bei geheimer Abstimmung"


def test_wechsel_auf_geheim_entfernt_vorhandene_stimmen(welt: dict[str, Any]) -> None:
    p = welt["personen"]
    voting_service.capture_votes(welt["item"], {p[0]: "yes"}, recorded_by=welt["user"])
    welt["item"].voting_method = "secret"
    welt["item"].save()

    voting_service.capture_votes(welt["item"], {p[0]: "yes"}, recorded_by=welt["user"])

    assert not SessionVote.objects.filter(agenda_item=welt["item"], person=p[0]).exists()

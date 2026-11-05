# SPDX-License-Identifier: AGPL-3.0-or-later
"""Die Einladungsmail listet Unterpunkte direkt unter ihrem TOP, nicht am Ende der Tagesordnung."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.faction.services import FactionMeetingEmailService, invitation_agenda


def _sitzung(org: Any, vorsitz: Any) -> FactionMeeting:
    """Wie in Münster am 02.11.: Unterpunkte 3.1/3.2 nach TOP 7 angelegt, also mit höherer ``order``."""
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=datetime(2026, 11, 9, 17, 0, tzinfo=UTC),
        created_by=vorsitz,
    )
    tops = {}
    for order, (nummer, titel) in enumerate(
        [("1", "Tagesordnung festlegen"), ("2", "Beschlüsse"), ("3", "Politische Arbeit"), ("4", "Termine")]
    ):
        tops[nummer] = FactionAgendaItem.objects.create(meeting=meeting, number=nummer, title=titel, order=order)
    FactionAgendaItem.objects.create(meeting=meeting, number="3.1", title="AG Neubündelung", parent=tops["3"], order=7)
    FactionAgendaItem.objects.create(meeting=meeting, number="3.2", title="AG Soziales", parent=tops["3"], order=8)
    FactionAgendaItem.objects.create(
        meeting=meeting, number="3.3", title="Personalie", parent=tops["3"], order=9, visibility="internal"
    )
    FactionAgendaItem.objects.create(
        meeting=meeting, number="3.4", title="Vorschlag", parent=tops["3"], order=10, proposal_status="proposed"
    )
    noe = FactionAgendaItem.objects.create(
        meeting=meeting, number="NÖ 1", title="Vertrauliches", order=11, visibility="internal"
    )
    FactionAgendaItem.objects.create(
        meeting=meeting, number="NÖ 1.1", title="Detail", parent=noe, order=12, visibility="internal"
    )
    return meeting


@pytest.mark.django_db
def test_unterpunkte_stehen_unter_ihrem_top(org: Any, make_member: Any) -> None:
    vorsitz = make_member(org, ["faction.view_public"], email="vorsitz@example.org")
    meeting = _sitzung(org, vorsitz)

    oeffentlich, intern = invitation_agenda(meeting, include_internal=False)
    assert [t.number for t in oeffentlich] == ["1", "2", "3", "3.1", "3.2", "4"]
    assert intern == []

    oeffentlich, intern = invitation_agenda(meeting, include_internal=True)
    assert [t.number for t in oeffentlich] == ["1", "2", "3", "3.1", "3.2", "3.3", "4"]
    assert [t.number for t in intern] == ["NÖ 1", "NÖ 1.1"]


@pytest.mark.django_db
def test_einladungsmail_in_reihenfolge_der_tagesordnung(org: Any, make_member: Any) -> None:
    vorsitz = make_member(org, ["faction.view_public"], email="vorsitz@example.org")
    meeting = _sitzung(org, vorsitz)

    _betreff, text, html = FactionMeetingEmailService()._invitation_content(
        meeting, vorsitz.user, include_internal=False, invitation_mode="opt_in"
    )
    for inhalt in (text, html or ""):
        stellen = [inhalt.index(f"TOP {nr}") for nr in ("3", "3.1", "3.2", "4")]
        assert stellen == sorted(stellen)
        assert "Personalie" not in inhalt and "Vorschlag" not in inhalt and "Vertrauliches" not in inhalt

    _betreff, text, html = FactionMeetingEmailService()._invitation_content(
        meeting, vorsitz.user, include_internal=True, invitation_mode="opt_in"
    )
    assert "TOP 3.3: Personalie (nicht-öffentlich)" in text
    assert "nicht-öffentlich" in (html or "")

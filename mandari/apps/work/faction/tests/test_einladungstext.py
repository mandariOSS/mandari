# SPDX-License-Identifier: AGPL-3.0-or-later
"""Der Einladungstext nennt Datum und Uhrzeit auf Deutsch und in Ortszeit (nicht UTC)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from django.utils import translation

from apps.work.faction.models import FactionMeeting
from apps.work.faction.services import FactionMeetingEmailService


@pytest.mark.django_db
def test_einladungstext_in_ortszeit_und_deutsch(org: Any, make_member: Any) -> None:
    vorsitz = make_member(org, ["faction.view_public"], email="vorsitz@example.org")
    meeting = FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=datetime(2026, 10, 1, 16, 0, tzinfo=UTC),  # 18:00 Uhr in Berlin (Sommerzeit)
        created_by=vorsitz,
    )

    # Auch wenn gerade eine andere Sprache aktiv ist (z. B. im Hintergrundjob)
    with translation.override("en"):
        text = FactionMeetingEmailService()._get_simple_invitation_text(meeting, vorsitz.user)

    assert "Datum: Donnerstag, 01. Oktober 2026" in text
    assert "Uhrzeit: 18:00 Uhr" in text

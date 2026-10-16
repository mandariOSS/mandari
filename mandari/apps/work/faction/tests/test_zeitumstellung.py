# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fraktionssitzungen an den Tagen der Zeitumstellung (Issue #974), Europe/Berlin.

Bisher speicherte das Formular 02:30 Uhr am 28.03.2027 (fehlende Stunde) still als 03:30 Uhr. Jetzt: verständliche
Meldung, nichts wird gespeichert. 02:30 Uhr am 25.10.2026 (doppelte Stunde) gilt als erste mit Hinweis; eine Sitzung,
die schon auf der zweiten steht, bleibt beim Bearbeiten dort.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from apps.common import local_time
from apps.work.faction.models import FactionMeeting

pytestmark = pytest.mark.django_db

RECHTE = ["faction.view_public", "faction.create", "faction.manage"]
ERSTE = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
ZWEITE = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def _meldungen(antwort: Any) -> list[str]:
    return [str(m) for m in get_messages(antwort.wsgi_request)]


def _anlegen(client: Any, org: Any, datum: str, uhrzeit: str) -> Any:
    daten = {"title": "Nachtsitzung", "start_date": datum, "start_time": uhrzeit}
    return client.post(reverse("work:faction", kwargs={"org_slug": org.slug}), daten)


def test_anlegen_in_der_doppelten_stunde_mit_hinweis(org: Any, make_member: Any, client_for: Any) -> None:
    antwort = _anlegen(client_for(make_member(org, RECHTE).user), org, "2026-10-25", "02:30")

    assert FactionMeeting.objects.get(organization=org).start == ERSTE
    assert local_time.ambiguous_notice(ERSTE, label="Beginn") in _meldungen(antwort)


def test_anlegen_in_der_fehlenden_stunde_wird_abgelehnt(org: Any, make_member: Any, client_for: Any) -> None:
    antwort = _anlegen(client_for(make_member(org, RECHTE).user), org, "2027-03-28", "02:30")

    assert not FactionMeeting.objects.filter(organization=org).exists()
    assert local_time.nonexistent_message(datetime(2027, 3, 28, 2, 30)) in _meldungen(antwort)


def _bearbeiten(client: Any, org: Any, meeting: FactionMeeting, datum: str, uhrzeit: str) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})
    return client.post(
        url,
        {"action": "update", "title": meeting.title, "start_date": datum, "start_time": uhrzeit, "location": "Büro"},
    )


def test_bearbeiten_in_der_fehlenden_stunde_aendert_nichts(org: Any, make_member: Any, client_for: Any) -> None:
    member = make_member(org, RECHTE)
    meeting = FactionMeeting.objects.create(organization=org, title="Sitzung", start=ERSTE, created_by=member)

    antwort = _bearbeiten(client_for(member.user), org, meeting, "2027-03-28", "02:30")

    meeting.refresh_from_db()
    assert (meeting.start, meeting.location) == (ERSTE, "")
    assert local_time.nonexistent_message(datetime(2027, 3, 28, 2, 30)) in _meldungen(antwort)


def test_bearbeiten_behaelt_die_gespeicherte_zweite_uhrzeit(org: Any, make_member: Any, client_for: Any) -> None:
    member = make_member(org, RECHTE)
    meeting = FactionMeeting.objects.create(organization=org, title="Sitzung", start=ZWEITE, created_by=member)

    antwort = _bearbeiten(client_for(member.user), org, meeting, "2026-10-25", "02:30")

    meeting.refresh_from_db()
    assert (meeting.start, meeting.location) == (ZWEITE, "Büro")
    assert not any("zweimal" in meldung for meldung in _meldungen(antwort))

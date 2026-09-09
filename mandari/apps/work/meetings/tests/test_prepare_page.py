# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rendering der Sitzungsvorbereitung (Issue #170): Die Alpine-Komponente `preparationApp`
lebt in `frontend/alpine/prepare-meeting.ts`; das Template liefert die Konfiguration nur
noch als JSON (`json_script`) und enthält kein Inline-JavaScript mehr.
"""

import json
import re
from typing import Any

import pytest

from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

CONFIG_RE = re.compile(r'<script[^>]*id="prepare-config"[^>]*>(.*?)</script>', re.S)


@pytest.fixture
def meeting(org: Any) -> OParlMeeting:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(external_id="https://ris.example.org/meeting/1", body=body, name="Rat")
    OParlAgendaItem.objects.create(
        external_id="https://ris.example.org/agenda/1", meeting=meeting, number="1", name="Haushalt", order=1
    )
    return meeting


@pytest.mark.django_db
def test_prepare_page_provides_json_config_and_no_inline_script(
    org: Any, meeting: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    member = make_member(org, ["meetings.prepare"], email="vorbereiter@example.org")
    response = client_for(member.user).get(f"/work/{org.slug}/meetings/{meeting.id}/prepare/")
    html = response.content.decode()

    assert response.status_code == 200
    assert 'x-data="preparationApp"' in html

    match = CONFIG_RE.search(html)
    assert match, "json_script #prepare-config fehlt"
    config = json.loads(match.group(1))
    assert config["orgSlug"] == org.slug
    assert config["meetingId"] == str(meeting.id)
    assert config["positionLabels"]["for"] == "Zustimmung"
    assert config["urls"]["summary"] == f"/work/{org.slug}/meetings/{meeting.id}/summary/"
    assert [item["name"] for item in config["items"]] == ["Haushalt"]
    assert config["items"][0]["position"] == "open"

    used_templates = {t.name for t in response.templates if t.name}
    assert "work/meetings/partials/_prepare_js.html" not in used_templates
    assert "function preparationApp" not in html

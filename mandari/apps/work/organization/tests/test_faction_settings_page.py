# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rendering der Fraktionssitzungs-Einstellungen (Hotspot-Zerlegung, Issue #174):
Die Live-Vorschau des ersten TOP-Titels lebt in `frontend/alpine/faction-title-preview.ts`
(`x-data="factionTitlePreview"`); das Template enthält kein Inline-JavaScript mehr, die
Sektionen liegen in `work/organization/partials/_faction_*.html`.
"""

import re
from typing import Any

import pytest
from django.urls import reverse

from apps.work.faction.models import FactionMeetingSchedule

MANAGE_PERMISSIONS = ["faction.manage", "protocols.publish"]
INLINE_SCRIPT_RE = re.compile(r"<script\b(?![^>]*type=\"application/json\")", re.I)
ON_HANDLER_RE = re.compile(r"\son[a-z]+=\"", re.I)


@pytest.fixture
def manager(org: Any, make_member: Any) -> Any:
    return make_member(org, MANAGE_PERMISSIONS, email="vorsitz@example.org")


def settings_url(org: Any) -> str:
    return reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})


@pytest.mark.django_db
def test_faction_settings_page_uses_alpine_preview_without_inline_script(
    org: Any, manager: Any, client_for: Any
) -> None:
    response = client_for(manager.user).get(settings_url(org))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'x-data="factionTitlePreview"' in html
    assert 'x-ref="titleInput"' in html
    assert 'id="preview-title" x-text="preview"' in html
    assert 'name="first_agenda_title_with_previous"' in html
    assert 'name="invitation_mode"' in html
    assert 'name="publish_protocols"' in html
    assert "Noch keine Sitzungsreihe angelegt." in html
    assert "DOMContentLoaded" not in html
    assert "updatePreview" not in html

    used = {t.name for t in response.templates if t.name}
    for partial in (
        "work/organization/partials/_faction_workflow_settings.html",
        "work/organization/partials/_faction_agenda_title_settings.html",
        "work/organization/partials/_faction_schedules.html",
    ):
        assert partial in used, partial
    for template in response.templates:
        if template.name and template.name.startswith("work/organization/"):
            assert not INLINE_SCRIPT_RE.search(template.source), template.name
            assert not ON_HANDLER_RE.search(template.source), template.name
    assert not re.search(r"<c-[a-z]", html)


@pytest.mark.django_db
def test_faction_settings_lists_schedules_with_confirm_dialog(org: Any, manager: Any, client_for: Any) -> None:
    FactionMeetingSchedule.objects.create(
        organization=org, name="Wöchentliche Fraktionssitzung", weekday=0, time="18:00"
    )

    response = client_for(manager.user).get(settings_url(org))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Wöchentliche Fraktionssitzung" in html
    assert "confirmAction({title: 'Sitzungsreihe löschen'" in html
    assert 'name="section" value="delete_schedule"' in html
    assert "onsubmit=" not in html

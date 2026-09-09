# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rendering der Fraktionssitzungs-Detailseite und des TOP-Panels (Hotspot-Zerlegung, Issue #174):
Die Alpine-Komponenten `factionDetail` und `agendaItemPanel` leben in
`frontend/alpine/faction-detail.ts` bzw. `frontend/alpine/agenda-item-panel.ts`; die Templates
liefern ihre Konfiguration nur noch per `json_script` und enthalten kein Inline-JavaScript mehr.
"""

import json
import re
from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionMeeting

MANAGE_PERMISSIONS = ["faction.view_public", "faction.view_non_public", "faction.manage", "faction.start"]
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
INLINE_SCRIPT_RE = re.compile(r"<script\b(?![^>]*type=\"application/json\")", re.I)
ON_HANDLER_RE = re.compile(r"\son[a-z]+=\"", re.I)


def read_config(html: str, element_id: str) -> dict[str, Any]:
    match = re.search(rf'<script[^>]*id="{element_id}"[^>]*>(.*?)</script>', html, re.S)
    assert match, f"json_script #{element_id} fehlt"
    config: dict[str, Any] = json.loads(match.group(1))
    return config


def assert_faction_templates_clean(response: Any) -> None:
    for template in response.templates:
        if template.name and template.name.startswith("work/faction/"):
            assert not INLINE_SCRIPT_RE.search(template.source), template.name
            assert not ON_HANDLER_RE.search(template.source), template.name


@pytest.fixture
def chair(org: Any, make_member: Any) -> Any:
    return make_member(org, MANAGE_PERMISSIONS, email="vorsitz@example.org")


@pytest.fixture
def meeting(org: Any, chair: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung März",
        start=timezone.now() + timedelta(days=3),
        status="planned",
        created_by=chair,
    )


@pytest.fixture
def item(meeting: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt 2027", visibility="public")


@pytest.mark.django_db
def test_detail_page_provides_json_config_and_no_inline_script(
    org: Any, chair: Any, meeting: FactionMeeting, item: FactionAgendaItem, client_for: Any
) -> None:
    url = reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.id})
    response = client_for(chair.user).get(url)
    html = response.content.decode()

    assert response.status_code == 200
    assert 'x-data="factionDetail"' in html
    assert "function factionDetail" not in html
    assert "htmx.ajax(" not in html

    config = read_config(html, "faction-detail-config")
    expected = reverse(
        "work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": meeting.id, "item_id": PLACEHOLDER}
    )
    assert config == {"panelUrlTemplate": expected}
    assert PLACEHOLDER in expected

    # Modale und Panel kommen aus den Partials, Ereignisse aus den Agenda-Partials bleiben gemappt
    for marker in (
        '@open-item-panel.window="openItemPanel($event.detail.id)"',
        'x-show="showItemModal"',
        'x-show="showDeleteModal"',
        'x-show="showDecisionModal"',
        'x-show="showProposalModal"',
        'x-show="showAddAttendeeModal"',
        'x-show="showEditModal"',
        'id="agenda-panel-content"',
        'id="agenda-container"',
    ):
        assert marker in html, marker
    used = {t.name for t in response.templates if t.name}
    for partial in (
        "work/faction/partials/_detail_header_actions.html",
        "work/faction/partials/_detail_panel.html",
        "work/faction/partials/_detail_item_modals.html",
        "work/faction/partials/_detail_meeting_modals.html",
    ):
        assert partial in used, partial
    assert_faction_templates_clean(response)
    assert not re.search(r"<c-[a-z]", html)


@pytest.mark.django_db
def test_item_panel_provides_action_url_and_alpine_component(
    org: Any, chair: Any, meeting: FactionMeeting, item: FactionAgendaItem, client_for: Any
) -> None:
    kwargs = {"org_slug": org.slug, "meeting_id": meeting.id, "item_id": item.id}
    response = client_for(chair.user).get(reverse("work:faction_item_panel", kwargs=kwargs))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'x-data="agendaItemPanel"' in html
    assert read_config(html, "agenda-item-panel-config") == {
        "actionUrl": reverse("work:faction_item_panel_action", kwargs=kwargs)
    }
    assert "Haushalt 2027" in html
    assert '@input.debounce.300ms="searchPapers()"' in html
    assert '@panel-autosaved.window="markSaved()"' in html
    assert "fetch(" not in html
    assert "X-CSRFToken" not in html
    for section in (
        "Beschreibung",
        "Beschluss",
        "Protokoll",
        "Aufgaben",
        "Anhänge",
        "Verknüpfte Anträge",
        "RIS-Vorlagen",
        "Referenz-Links",
    ):
        assert section in html, section
    used = {t.name for t in response.templates if t.name}
    for partial in (
        "work/faction/partials/_agenda_item_panel_header.html",
        "work/faction/partials/_agenda_item_panel_records.html",
        "work/faction/partials/_agenda_item_panel_work.html",
        "work/faction/partials/_agenda_item_panel_references.html",
    ):
        assert partial in used, partial
    assert_faction_templates_clean(response)
    assert not re.search(r"<c-[a-z]", html)


@pytest.mark.django_db
def test_item_panel_read_only_for_viewer(
    org: Any, make_member: Any, meeting: FactionMeeting, item: FactionAgendaItem, client_for: Any
) -> None:
    viewer = make_member(org, ["faction.view_public"], email="leser@example.org")
    kwargs = {"org_slug": org.slug, "meeting_id": meeting.id, "item_id": item.id}
    response = client_for(viewer.user).get(reverse("work:faction_item_panel", kwargs=kwargs))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'x-data="agendaItemPanel"' in html
    assert "Keine Beschreibung" in html
    assert "Keine Abstimmung erfasst" in html
    assert 'name="title" value="Haushalt 2027"' not in html  # Titel nur lesend
    assert ">Haushalt 2027</h2>" in html
    assert "TOP löschen" not in html

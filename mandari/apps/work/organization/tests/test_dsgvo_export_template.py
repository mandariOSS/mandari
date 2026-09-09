# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rendering des DSGVO-Datenexports als PDF-Vorlage (Hotspot-Zerlegung, Issue #174):
`work/profile/export/dsgvo_export.html` bindet das Druck-Stylesheet und die Abschnitte
aus `work/profile/export/partials/_dsgvo_*.html` ein; Spaltenbreiten und Hinweise
kommen aus CSS-Klassen statt `style="…"`-Attributen.
"""

import re
from typing import Any

import pytest
from django.template.loader import render_to_string
from django.test.signals import template_rendered
from django.utils import timezone

from apps.work.organization.export_service import dsgvo_export_service

TEMPLATE = "work/profile/export/dsgvo_export.html"
PARTIALS = (
    "work/profile/export/partials/_dsgvo_styles.html",
    "work/profile/export/partials/_dsgvo_account.html",
    "work/profile/export/partials/_dsgvo_tasks_motions.html",
    "work/profile/export/partials/_dsgvo_meetings.html",
    "work/profile/export/partials/_dsgvo_misc.html",
)


@pytest.fixture
def member(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="export@example.org", first_name="Erika", last_name="Muster")


def render_export(org: Any, member: Any) -> str:
    data = dsgvo_export_service.collect_user_data(user=member.user, membership=member, organization=org)
    context = {"data": data, "user": member.user, "organization": org, "export_date": timezone.now()}
    return render_to_string(TEMPLATE, context)


@pytest.mark.django_db
def test_dsgvo_export_renders_all_sections_from_partials(org: Any, member: Any) -> None:
    rendered: list[str] = []

    def on_render(sender: Any, context: Any, **kwargs: Any) -> None:
        rendered.append(sender.name)

    template_rendered.connect(on_render)
    try:
        html = render_export(org, member)
    finally:
        template_rendered.disconnect(on_render)

    assert "Datenauskunft gem. DSGVO Art. 15/20" in html
    assert "export@example.org" in html
    assert "<style>" in html
    for heading in ("1. Kontodaten", "3. Sicherheit", "5. Antr", "7. Sitzungsvorbereitung", "11. Support-Tickets"):
        assert heading in html, heading
    assert 'id="page-footer"' in html
    assert ' style="' not in html
    assert ".col-18 { width: 18%; }" in html
    for partial in PARTIALS:
        assert partial in rendered, partial


@pytest.mark.django_db
def test_dsgvo_export_has_no_script_and_renders_without_request(org: Any, member: Any) -> None:
    # Der Export läuft ohne Request (Hintergrund-Task): render_to_string ohne request-Argument
    html = render_export(org, member)
    assert not re.search(r"<script", html)
    assert not re.search(r"<c-[a-z]", html)

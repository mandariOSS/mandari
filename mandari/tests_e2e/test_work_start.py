# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Startseite von Work im neuen Rahmen (Issue #852) im Browser: barrierefrei ohne schwere axe-Befunde (ganze Seite),
keine Fehler im Browser, ab 1.440 px rechts höchstens ein Viertel frei, am Handy kein seitliches Überlaufen.
Screenshots hell und dunkel als CI-Artefakt.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from apps.work.faction.models import FactionMeeting
from apps.work.tasks.models import Task
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import BrowserProblems

RECHTS = """() => {
  const el = document.querySelector('.work-page-content');
  const inhalt = [...el.querySelectorAll('section')].map((s) => s.getBoundingClientRect().right);
  return {breite: window.innerWidth, rechts: Math.max(...inhalt), scroll: document.documentElement.scrollWidth};
}"""


@pytest.fixture
def start(admin: Any) -> Any:
    organisation = admin.organization
    organisation.work_new_design = True
    organisation.save(update_fields=["work_new_design"])
    Task.objects.create(
        organization=organisation, title="Pressemitteilung abstimmen", assigned_to=admin, priority="high"
    )
    FactionMeeting.objects.create(
        organization=organisation, title="Fraktionssitzung", start=timezone.now() + timedelta(days=3)
    )
    return admin


@pytest.mark.parametrize("breite", [1440, 2560, 390])
def test_start(
    page: Any,
    goto: Any,
    login: Any,
    start: Any,
    axe: Any,
    screenshot: Any,
    dark_mode: Any,
    problems: BrowserProblems,
    breite: int,
) -> None:
    page.set_viewport_size({"width": breite, "height": 900 if breite > 500 else 844})
    login(start.user.email, PASSWORD)
    goto(f"/work/{start.organization.slug}/")
    expect(page.get_by_role("heading", name="Nächste Sitzungen")).to_be_visible()
    expect(page.get_by_role("heading", name="Für Sie")).to_be_visible()
    expect(page.get_by_text("Pressemitteilung abstimmen")).to_be_visible()
    messung = page.evaluate(RECHTS)
    assert messung["scroll"] <= breite, messung
    if breite >= 1440:
        assert (breite - messung["rechts"]) / breite <= 0.25, messung
    ergebnis = axe()
    assert not ergebnis.failing, ergebnis.describe()
    screenshot(f"work-start-{breite}")
    dark_mode(True)
    ergebnis = axe()
    assert not ergebnis.failing, ergebnis.describe()
    screenshot(f"work-start-{breite}-dunkel")
    problems.assert_clean("Startseite")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hinweise des Sitzungsdienstes in der Demo (Issue #745), im Browser und in beiden Rahmen.

Mit den Demo-Daten (setup_demo_environment) und eingeschaltetem Demo-Schalter: Start, Anträge, Einstellungen,
Datenschutz, Audit-Log, Pauschalen und das Sitzungsformular zeigen keine Betriebshinweise, die Fristen auf dem Start
bleiben, und der Hinweis der Demo steht weiter auf der Seite. Screenshots aus beiden Rahmen landen als CI-Artefakt (Vorher/Nachher im PR).
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.urls import reverse
from playwright.sync_api import expect

from apps.accounts.models import User
from apps.common.demo_daten import DEMO_USERS
from tests_e2e.conftest import BrowserProblems, login_via_form, wait_for_bundle

PASSWORT = "E2e-Session-Hinweise-1"
#: Mandant der Demo (apps/common/management/commands/setup_demo_environment.py, DEMO_SESSION_SLUG)
MANDANT = "stadtverwaltung-musterstadt-demo"
SEITEN = {
    "start": "dashboard",
    "antraege": "applications",
    "einstellungen": "settings",
    "datenschutz": "privacy_settings",
    "audit": "audit_log",
    "pauschalen": "allowances_monthly",
    "sitzung-anlegen": "meeting_create",
}
BETRIEBSHINWEISE = ("audit_chain_backfill", "send_session_reminders", "session_privacy_purge", "manage.py")


@pytest.fixture
def demo(settings: Any, tmp_path: Path) -> None:
    """Demo-Daten mit dem Schalter der öffentlichen Demo; Anmeldung mit Administrator- und Revisionsrolle."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.DEMO_INSTANCE = True
    call_command("setup_demo_environment", stdout=StringIO())
    nutzer = User.objects.get(email=DEMO_USERS["verwaltung"]["email"])
    nutzer.set_password(PASSWORT)
    nutzer.save(update_fields=["password"])


@pytest.mark.parametrize(("rahmen", "neu"), [("alt", False), ("neu", True)])
def test_demo_ohne_betriebshinweise(
    page: Any, live_server: Any, demo: None, problems: BrowserProblems, screenshot: Any, rahmen: str, neu: bool
) -> None:
    from apps.session.models import SessionTenant

    SessionTenant.objects.filter(slug=MANDANT).update(session_new_design=neu)
    login_via_form(page, live_server.url, DEMO_USERS["verwaltung"]["email"], PASSWORT)
    page.set_viewport_size({"width": 1280, "height": 900})
    texte = {}
    for name, url_name in SEITEN.items():
        antwort = page.goto(f"{live_server.url}{reverse(f'session:{url_name}', kwargs={'tenant_slug': MANDANT})}")
        assert antwort is not None and antwort.ok, f"{name}: {antwort.status if antwort else 'keine Antwort'}"
        wait_for_bundle(page)
        screenshot(f"session-hinweise-{rahmen}-{name}")
        texte[name] = page.locator("body").inner_text()
        # Der Hinweis der Demo bleibt: Er sagt, dass alles erfunden ist (Issue #99)
        expect(page.locator("#demo-hinweis")).to_be_visible()
    for name, text in texte.items():
        for hinweis in BETRIEBSHINWEISE:
            assert hinweis not in text, f"{name} im Rahmen {rahmen} zeigt {hinweis}"

    # Die Fristen bleiben auf dem Start, auch am Handy, ohne dass die Seite seitlich überläuft
    start = reverse("session:dashboard", kwargs={"tenant_slug": MANDANT})
    page.goto(f"{live_server.url}{start}")
    wait_for_bundle(page)
    expect(page.locator("[data-testid='start-fristen']")).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_server.url}{start}")
    wait_for_bundle(page)
    expect(page.locator("[data-testid='start-fristen']")).to_be_visible()
    masse = page.evaluate("() => ({scroll: document.documentElement.scrollWidth, breite: window.innerWidth})")
    assert masse["scroll"] <= masse["breite"] + 1, f"Start im Rahmen {rahmen} läuft seitlich über: {masse}"
    screenshot(f"session-hinweise-{rahmen}-start-390")
    problems.assert_clean(f"Hinweise im Rahmen {rahmen}")

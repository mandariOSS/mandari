# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neuer Rahmen des Sitzungsdienstes im Browser (Issue #944), eingeschaltet je Mandant.

Mit den Demo-Daten (setup_demo_environment) und eingeschaltetem Schalter: Start, Sitzungen, Sitzung, Vorlagen und
Vorlage stehen im neuen Rahmen und im neuen Erscheinungsbild und laufen am Handy (390 px) und am Tablet (768 px)
nicht seitlich über; der Rahmen hat keine schweren axe-Befunde, keine Fehler im Browser; das Blatt „Mehr“ öffnet sich
am Handy und gibt den Fokus beim Schließen an „Mehr“ zurück. Mit ausgeschaltetem Schalter bleibt der bisherige
Rahmen. Screenshots landen als CI-Artefakt.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from playwright.sync_api import expect

from apps.accounts.models import User
from apps.common.demo_daten import DEMO_USERS
from tests_e2e.conftest import AXE_PATH, AxeResult, BrowserProblems, login_via_form, wait_for_bundle

PASSWORT = "E2e-Session-Rahmen-1"
#: Mandant der Demo (apps/common/management/commands/setup_demo_environment.py, DEMO_SESSION_SLUG)
MANDANT = "stadtverwaltung-musterstadt-demo"
BREITEN = (390, 768)
#: Breite der Seite gegen die Breite des Fensters (seitlicher Überlauf)
UEBERLAUF = "() => ({scroll: document.documentElement.scrollWidth, breite: window.innerWidth})"


@pytest.fixture
def demo(settings: Any, tmp_path: Path) -> dict[str, str]:
    """Demo-Umgebung mit eingeschaltetem neuen Rahmen für den Session-Mandanten; liefert die Kernseiten."""
    from apps.session.models import SessionMeeting, SessionPaper, SessionTenant

    settings.MEDIA_ROOT = str(tmp_path / "media")
    call_command("setup_demo_environment", stdout=StringIO())
    tenant = SessionTenant.objects.get(slug=MANDANT)
    tenant.session_new_design = True
    tenant.save(update_fields=["session_new_design"])
    nutzer = User.objects.get(email=DEMO_USERS["verwaltung"]["email"])
    nutzer.set_password(PASSWORT)
    nutzer.save(update_fields=["password"])
    sitzung = SessionMeeting.objects.filter(tenant=tenant).order_by("-start").first()
    vorlage = SessionPaper.objects.filter(tenant=tenant, status="review").first() or SessionPaper.objects.first()
    assert sitzung is not None and vorlage is not None
    basis = f"/session/{MANDANT}/"
    return {
        "start": basis,
        "sitzungen": f"{basis}meetings/",
        "sitzung": f"{basis}meetings/{sitzung.pk}/",
        "vorlagen": f"{basis}papers/",
        "vorlage": f"{basis}papers/{vorlage.pk}/",
    }


def _axe_rahmen(page: Any) -> AxeResult:
    """axe-core auf dem Rahmen (Seitenleiste, Kopfzeile, Kopfband, Reiter, Leiste unten)."""
    page.add_script_tag(content=AXE_PATH.read_text(encoding="utf-8"))
    ergebnis = page.evaluate(
        "async () => await axe.run({ exclude: [['.work-page-content']] }, { resultTypes: ['violations'], "
        "runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })"
    )
    return AxeResult(violations=list(ergebnis.get("violations", [])))


def test_kernseiten_am_handy_und_tablet(
    page: Any, live_server: Any, demo: dict[str, str], problems: BrowserProblems, screenshot: Any
) -> None:
    login_via_form(page, live_server.url, DEMO_USERS["verwaltung"]["email"], PASSWORT)
    for breite in BREITEN:
        page.set_viewport_size({"width": breite, "height": 900})
        for name, pfad in demo.items():
            page.goto(f"{live_server.url}{pfad}")
            wait_for_bundle(page)
            expect(page.locator("html")).to_have_attribute("data-rahmen", "neu")
            expect(page.locator("main#inhalt")).to_have_attribute("data-gestaltung", "neu")
            masse = page.evaluate(UEBERLAUF)
            assert masse["scroll"] <= masse["breite"] + 1, f"{name} bei {breite} px läuft seitlich über: {masse}"
            # Am Handy und Tablet führt die Leiste unten, die Seitenleiste ist verborgen
            expect(page.locator("nav[aria-label='Hauptbereiche']")).to_be_visible()
            expect(page.locator("#session-navigation")).to_be_hidden()
            if breite == 390:
                screenshot(f"session-neu-{name}-390")
    problems.assert_clean("Kernseiten im neuen Rahmen")

    # Blatt „Mehr“: öffnet als Dialog, Escape schließt, der Fokus kehrt auf „Mehr“ zurück
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_server.url}{demo['start']}")
    wait_for_bundle(page)
    mehr = page.locator("button[aria-controls='session-mehr']")
    mehr.click()
    dialog = page.locator("#session-mehr")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("link", name="Gremien")).to_be_visible()
    expect(dialog.get_by_role("link", name="Einstellungen")).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(mehr).to_be_focused()

    # Breit: Seitenleiste mit den Bereichen, Rahmen ohne schwere axe-Befunde
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{live_server.url}{demo['sitzung']}")
    wait_for_bundle(page)
    leiste = page.locator("#session-navigation")
    expect(leiste).to_be_visible()
    expect(leiste.get_by_role("link", name="Sitzungen")).to_have_attribute("aria-current", "true")
    ergebnis = _axe_rahmen(page)
    assert not ergebnis.failing, ergebnis.describe()
    screenshot("session-neu-sitzung-1280")
    problems.assert_clean("Rahmen breit")


def test_ohne_schalter_bisheriger_rahmen(page: Any, live_server: Any, demo: dict[str, str]) -> None:
    from apps.session.models import SessionTenant

    SessionTenant.objects.filter(slug=MANDANT).update(session_new_design=False)
    login_via_form(page, live_server.url, DEMO_USERS["verwaltung"]["email"], PASSWORT)
    page.goto(f"{live_server.url}{demo['sitzung']}")
    wait_for_bundle(page)
    assert page.locator("html").get_attribute("data-rahmen") is None
    expect(page.locator("aside.session-sidebar")).to_have_count(1)

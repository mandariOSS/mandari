# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Work-Erscheinungsbild Stufe 0 im Browser (Issue #851).

- Inter lädt aus dem eigenen Ursprung (keine Schriftanfrage an Dritte).
- Die Meldung nach der Anmeldung liegt unter der Kopfzeile und verdeckt Glocke und Kürzel nicht.
- Am Handy ist das Menü ein Dialog: Escape schließt es, der Fokus kehrt auf den Menüknopf zurück, geschlossen
  ist die Leiste nicht per Tastatur erreichbar.
- Start, Dokumente, Fraktionssitzungen und RIS-Übersicht: axe ohne kritische/schwere Befunde, kein
  waagerechtes Überlaufen bei 1280, 1440 und 390 px; Screenshots hell/dunkel als Artefakt.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.work.faction.models import FactionMeeting
from apps.work.motions.models import Motion
from tests_e2e.conftest import ADMIN_PASSWORD, BrowserProblems, wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def vorsitz(admin: Any) -> Any:
    org = admin.organization
    Motion.objects.create(organization=org, author=admin, title="Antrag: Musterweg (Demo)", status="draft")
    FactionMeeting.objects.create(
        organization=org, title="Wochensitzung (Demo)", start=timezone.now() + timedelta(days=3), status="planned"
    )
    return admin


def _anmelden(page: Any, live_server: Any, membership: Any) -> None:
    page.goto(f"{live_server.url}/accounts/login/")
    wait_for_bundle(page)
    page.fill("input[name=email]", membership.user.email)
    page.fill("input[name=password]", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def _box(page: Any, selector: str) -> dict[str, float]:
    box = page.locator(selector).first.bounding_box()
    assert box, selector
    return dict(box)


def test_schrift_und_meldung_nach_der_anmeldung(page: Any, live_server: Any, vorsitz: Any) -> None:
    schriften: list[str] = []
    page.on("request", lambda r: schriften.append(r.url) if r.resource_type == "font" else None)
    page.set_viewport_size({"width": 1440, "height": 900})
    _anmelden(page, live_server, vorsitz)

    # Meldung „Erfolgreich angemeldet“: unterhalb der Kopfzeile, nicht über Glocke und Kürzel
    meldung = page.locator('[x-data="toastManager"] > div').first
    expect(meldung).to_be_visible()
    kopf = _box(page, ".work-topbar")
    toast = dict(meldung.bounding_box() or {})
    assert toast["y"] >= kopf["y"] + kopf["height"], (toast, kopf)

    page.evaluate("() => document.fonts.ready")
    assert page.evaluate("() => document.fonts.check('16px Inter')")
    assert schriften, "Inter wurde nicht geladen"
    assert all(url.startswith(live_server.url) for url in schriften), schriften


def test_menue_am_handy_ist_ein_dialog(page: Any, live_server: Any, vorsitz: Any, problems: BrowserProblems) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _anmelden(page, live_server, vorsitz)
    page.goto(f"{live_server.url}/work/{vorsitz.organization.slug}/documents/")
    wait_for_bundle(page)

    knopf = page.get_by_role("button", name="Menü", exact=True)
    leiste = page.locator("#work-navigation")
    expect(knopf).to_have_attribute("aria-expanded", "false")
    # Geschlossen: Links der Leiste sind nicht erreichbar
    expect(leiste).to_be_hidden()

    knopf.click()
    expect(leiste).to_have_attribute("role", "dialog")
    expect(leiste).to_have_attribute("aria-modal", "true")
    expect(knopf).to_have_attribute("aria-expanded", "true")
    aktiv = leiste.locator('a[aria-current="page"]')
    expect(aktiv).to_have_count(1)
    expect(aktiv).to_contain_text("Dokumente")
    # Der Fokus bleibt in der Leiste
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.getElementById('work-navigation').contains(document.activeElement)")

    page.keyboard.press("Escape")
    expect(leiste).to_be_hidden()
    expect(knopf).to_be_focused()
    expect(knopf).to_have_attribute("aria-expanded", "false")
    problems.assert_clean("Menü am Handy")


@pytest.mark.parametrize(
    ("name", "pfad"),
    [
        ("start", ""),
        ("dokumente", "documents/"),
        ("fraktionssitzungen", "faction/"),
        ("ris-uebersicht", "ris/"),
    ],
)
def test_geaenderte_seiten_barrierefrei_ohne_ueberlauf(
    page: Any,
    live_server: Any,
    vorsitz: Any,
    axe: Any,
    screenshot: Any,
    problems: BrowserProblems,
    name: str,
    pfad: str,
) -> None:
    _anmelden(page, live_server, vorsitz)
    url = f"{live_server.url}/work/{vorsitz.organization.slug}/{pfad}"
    for breite, hoehe in ((1280, 900), (1440, 900), (390, 844)):
        page.set_viewport_size({"width": breite, "height": hoehe})
        page.goto(url)
        wait_for_bundle(page)
        ueberlauf = page.evaluate("() => document.scrollingElement.scrollWidth - window.innerWidth")
        assert ueberlauf <= 0, f"{name} bei {breite} px: {ueberlauf} px waagerecht übergelaufen"
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url)
    wait_for_bundle(page)
    ergebnis = axe()
    assert not ergebnis.failing, f"axe auf {name}:\n{ergebnis.describe()}"
    screenshot(f"work-stufe0-{name}-hell")
    page.evaluate("() => localStorage.setItem('darkMode', 'true')")
    page.reload()
    wait_for_bundle(page)
    screenshot(f"work-stufe0-{name}-dunkel")
    page.evaluate("() => localStorage.setItem('darkMode', 'false')")
    problems.assert_clean(name)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Meldungen unten rechts im Browser (Issue #1454): jede einmal, gut zu schließen.

- Eine ungelesene Benachrichtigung blendet sich nicht bei jedem Seitenaufruf ein. Eine neue erscheint einmal, in der
  gemeinsamen Anzeige unten rechts, mit Titel und Ziel; auf der nächsten Seite nicht mehr.
- Der Schließen-Knopf hat mindestens 36 px Kantenlänge und entfernt die Meldung.
- Dieselbe Meldung zweimal ausgelöst steht einmal da.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.cache import cache

from apps.work.notifications.models import Notification, NotificationType
from tests_e2e.conftest import ADMIN_PASSWORD, wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)

MELDUNGEN = "[x-data='toastManager'] [x-show='toast.visible']"
DESKTOP = {"width": 1440, "height": 900}


def _benachrichtigung(mitglied: Any, titel: str, link: str = "") -> None:
    Notification.objects.create(
        recipient=mitglied,
        notification_type=NotificationType.TASK_ASSIGNED,
        title=titel,
        message="Bitte bis Freitag prüfen",
        link=link,
    )
    # Direkt angelegt, am Hub vorbei: die gemerkte Zahl der ungelesenen gilt dann nicht mehr
    cache.clear()


def _anmelden(page: Any, live_server: Any, membership: Any) -> None:
    page.goto(f"{live_server.url}/accounts/login/")
    wait_for_bundle(page)
    page.fill("input[name=email]", membership.user.email)
    page.fill("input[name=password]", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def _oeffnen(page: Any, live_server: Any, org: Any, pfad: str) -> None:
    """Seite öffnen und ihre erste Abfrage der Benachrichtigungen abwarten."""
    with page.expect_response(lambda antwort: "/notifications/latest/" in antwort.url) as abfrage:
        page.goto(f"{live_server.url}/work/{org.slug}/{pfad}")
    assert abfrage.value.ok
    wait_for_bundle(page)


def test_benachrichtigung_wird_einmal_eingeblendet(page: Any, live_server: Any, admin: Any) -> None:
    org = admin.organization
    page.set_viewport_size(DESKTOP)
    _benachrichtigung(admin, "Alte Aufgabe")
    _anmelden(page, live_server, admin)

    # Erster Besuch in diesem Browser: Ungelesenes steht in der Glocke, eingeblendet wird nichts
    _oeffnen(page, live_server, org, "tasks/")
    expect(page.locator(MELDUNGEN).filter(has_text="Alte Aufgabe")).to_have_count(0)
    assert page.title().startswith("(1) ")

    # Eine neue Benachrichtigung erscheint mit dem nächsten Abruf: unten rechts, mit Titel, Text und Ziel
    ziel = f"/work/{org.slug}/tasks/"
    # Erst die Seite verlassen: Ihr regelmäßiger Abruf könnte die neue Benachrichtigung sonst schon dort zeigen
    page.goto("about:blank")
    _benachrichtigung(admin, "Neue Aufgabe", link=ziel)
    _oeffnen(page, live_server, org, "tasks/")
    hinweis = page.locator(MELDUNGEN).filter(has_text="Neue Aufgabe")
    expect(hinweis).to_have_count(1)
    expect(hinweis).to_be_visible()
    expect(hinweis).to_contain_text("Bitte bis Freitag prüfen")
    expect(hinweis.locator("a")).to_have_attribute("href", ziel)
    page.wait_for_timeout(400)  # Einblenden abwarten
    box = dict(hinweis.bounding_box() or {})
    assert box["y"] > DESKTOP["height"] / 2, box
    assert box["x"] > DESKTOP["width"] / 2, box
    # Die frühere eigene Anzeige oben rechts gibt es nicht mehr
    expect(page.locator("#notification-toast-container")).to_have_count(0)

    # Jede weitere Seite: ungelesen bleibt sie, eingeblendet wird sie nicht noch einmal
    for pfad in ("", "tasks/"):
        _oeffnen(page, live_server, org, pfad)
        expect(page.locator(MELDUNGEN).filter(has_text="Neue Aufgabe")).to_have_count(0)
        assert page.title().startswith("(2) ")


def test_meldung_schliessen_und_nicht_doppelt(page: Any, live_server: Any, admin: Any) -> None:
    org = admin.organization
    page.set_viewport_size(DESKTOP)
    _anmelden(page, live_server, admin)
    _oeffnen(page, live_server, org, "tasks/")

    page.evaluate("() => { window.showToast('Probe eins', 'info'); window.showToast('Probe zwei', 'success') }")
    eins = page.locator(MELDUNGEN).filter(has_text="Probe eins")
    zwei = page.locator(MELDUNGEN).filter(has_text="Probe zwei")
    expect(eins).to_be_visible()
    expect(zwei).to_be_visible()
    page.wait_for_timeout(400)  # Einblenden abwarten

    knopf = eins.get_by_role("button", name="Meldung schließen")
    flaeche = dict(knopf.bounding_box() or {})
    assert flaeche["width"] >= 36 and flaeche["height"] >= 36, flaeche
    knopf.click()
    expect(eins).to_have_count(0)
    expect(zwei).to_be_visible()

    # Dieselbe Meldung noch einmal: keine zweite
    page.evaluate("() => { for (let i = 0; i < 3; i++) window.showToast('Keine Verbindung zum Server.', 'error') }")
    expect(page.locator(MELDUNGEN).filter(has_text="Keine Verbindung zum Server.")).to_have_count(1)

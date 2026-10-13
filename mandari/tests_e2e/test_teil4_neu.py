# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benachrichtigungen, Hilfe und Team im neuen Erscheinungsbild im Browser (Issue #951, Teilpaket 4).

- Benachrichtigungen: „Gelesen“ nimmt Punkt und Knopf weg (data-parent statt Abstandsklasse), der Reiter „Ungelesen“
  filtert, „Alle als gelesen markieren“ lädt neu, „Anzeigen“ folgt nach dem Markieren dem Link.
- Ticket: neue Nachrichten erscheinen beim Nachladen (ticketVerlauf), Neues Ticket nimmt Anhänge über das Dateifeld.
- Alle Seiten: keine Alpine-Fehler, axe ohne kritische/schwere Befunde, kein waagerechtes Überlaufen bei 390 px.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.work.notifications.models import Notification
from apps.work.support.models import SupportTicket, SupportTicketMessage
from tests_e2e.conftest import ADMIN_PASSWORD, BrowserProblems, wait_for_bundle, wait_for_component

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def vorsitz(admin: Any) -> Any:
    org = admin.organization
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    admin.user.first_name, admin.user.last_name = "Vera", "Beispiel"
    admin.user.save(update_fields=["first_name", "last_name"])
    return admin


def _anmelden(page: Any, live_server: Any, membership: Any) -> str:
    page.goto(f"{live_server.url}/accounts/login/")
    wait_for_bundle(page)
    page.fill("input[name=email]", membership.user.email)
    page.fill("input[name=password]", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    return f"{live_server.url}/work/{membership.organization.slug}"


def _oeffnen(page: Any, url: str) -> None:
    page.goto(url)
    wait_for_bundle(page)
    expect(page.locator('main[data-gestaltung="neu"]')).to_have_count(1)


def _ohne_ueberlauf(page: Any, wo: str) -> None:
    breite = page.evaluate("document.documentElement.scrollWidth")
    assert breite <= page.viewport_size["width"], f"{wo}: {breite}px breit"


def test_benachrichtigung_als_gelesen(
    page: Any, live_server: Any, vorsitz: Any, axe: Any, problems: BrowserProblems
) -> None:
    hinweis = Notification.objects.create(
        recipient=vorsitz, notification_type="task_assigned", title="Neue Aufgabe für Sie", message="Bitte ansehen."
    )
    zweiter = Notification.objects.create(
        recipient=vorsitz, notification_type="task_assigned", title="Frist morgen", message="Haushaltsrede."
    )
    basis = _anmelden(page, live_server, vorsitz)
    _oeffnen(page, f"{basis}/notifications/")
    eintrag = page.locator("[data-benachrichtigung]", has_text="Neue Aufgabe für Sie")
    expect(eintrag.locator("[data-ungelesen-punkt]")).to_have_count(2)
    with page.expect_response(lambda r: "/mark-read/" in r.url):
        eintrag.get_by_role("button").click()
    expect(eintrag.locator("[data-ungelesen-punkt]")).to_have_count(0)
    expect(eintrag.get_by_role("button")).to_have_count(0)
    hinweis.refresh_from_db()
    assert hinweis.is_read
    assert not axe().failing, axe().describe()

    # Reiter „Ungelesen“ zeigt nur Ungelesenes; „Alle als gelesen markieren“ lädt die Seite neu (data-after-request)
    page.get_by_role("link", name="Ungelesen").click()
    page.wait_for_load_state("networkidle")
    expect(page.locator("[data-benachrichtigung]")).to_have_count(1)
    expect(page.locator("[data-benachrichtigung]")).to_contain_text("Frist morgen")
    with page.expect_navigation():
        page.get_by_role("button", name="Alle als gelesen markieren").click()
    expect(page.get_by_test_id("benachrichtigungen-leer")).to_contain_text("alle Benachrichtigungen gelesen")
    zweiter.refresh_from_db()
    assert zweiter.is_read
    problems.assert_clean("Benachrichtigungen")


def test_benachrichtigung_anzeigen_folgt_dem_link(
    page: Any, live_server: Any, vorsitz: Any, problems: BrowserProblems
) -> None:
    # „Anzeigen“ markiert per hx-post als gelesen und folgt danach dem Link (data-after-request="follow-href")
    ziel = f"/work/{vorsitz.organization.slug}/team/"
    hinweis = Notification.objects.create(
        recipient=vorsitz, notification_type="task_assigned", title="Neues Mitglied", message="", link=ziel
    )
    basis = _anmelden(page, live_server, vorsitz)
    _oeffnen(page, f"{basis}/notifications/")
    eintrag = page.locator("[data-benachrichtigung]", has_text="Neues Mitglied")
    with page.expect_navigation(url=f"**{ziel}"):
        eintrag.get_by_role("link", name="Anzeigen").click()
    hinweis.refresh_from_db()
    assert hinweis.is_read
    problems.assert_clean("Benachrichtigung anzeigen")


def test_ticket_laedt_nachrichten_nach_und_neues_ticket(
    page: Any, live_server: Any, vorsitz: Any, axe: Any, problems: BrowserProblems
) -> None:
    ticket = SupportTicket(organization=vorsitz.organization, subject="Kalender", status="open", created_by=vorsitz)
    ticket.set_description_encrypted("Fehlt.")
    ticket.save()
    basis = _anmelden(page, live_server, vorsitz)
    _oeffnen(page, f"{basis}/support/{ticket.id}/")
    wait_for_component(page, "ticketVerlauf")
    antwort = SupportTicketMessage(ticket=ticket, author_membership=vorsitz)
    antwort.set_content_encrypted("Nachtrag aus einem anderen Fenster")
    antwort.save()
    expect(page.get_by_test_id("ticket-nachrichten")).to_contain_text(
        "Nachtrag aus einem anderen Fenster", timeout=12000
    )
    expect(page.locator("#message-thread")).to_have_attribute(
        "hx-get", f"/work/{vorsitz.organization.slug}/support/{ticket.id}/messages/?count=1"
    )
    assert not axe().failing, axe().describe()

    _oeffnen(page, f"{basis}/support/create/")
    wait_for_component(page, "ticketForm")
    page.set_input_files("#attachments", files=[{"name": "notiz.txt", "mimeType": "text/plain", "buffer": b"hallo"}])
    expect(page.get_by_text("notiz.txt")).to_be_visible()
    expect(page.get_by_text("5 B", exact=True)).to_be_visible()
    page.get_by_role("button", name="Anhang entfernen: notiz.txt").click()
    expect(page.get_by_text("notiz.txt")).to_have_count(0)
    problems.assert_clean("Hilfe")


def test_team_und_hilfe_ohne_ueberlauf(page: Any, live_server: Any, vorsitz: Any, problems: BrowserProblems) -> None:
    """Team, Mitgliedsprofil und Hilfe laden ohne Alpine-Fehler und laufen bei 390 px nicht über."""
    basis = _anmelden(page, live_server, vorsitz)
    page.set_viewport_size({"width": 390, "height": 844})
    for pfad in ("team/", f"team/{vorsitz.id}/", "support/"):
        _oeffnen(page, f"{basis}/{pfad}")
        _ohne_ueberlauf(page, pfad)
    problems.assert_clean("Team und Hilfe")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benachrichtigungen, Hilfe, Team, Profil und Aufgaben im neuen Erscheinungsbild im Browser (Issue #951, Teilpaket 4).

- Benachrichtigungen: „Gelesen“ nimmt Punkt und Knopf weg (data-parent statt Abstandsklasse), der Reiter „Ungelesen“
  filtert, „Alle als gelesen markieren“ lädt neu, „Anzeigen“ folgt nach dem Markieren dem Link.
- Ticket: neue Nachrichten erscheinen beim Nachladen (ticketVerlauf), Neues Ticket nimmt Anhänge über das Dateifeld.
- Profil: Reiter führen zu allen Seiten, „2FA einrichten“ öffnet den Dialog, Escape schließt ihn.
- Aufgaben: Karte öffnet das Panel im Seitenblatt (Klick und Enter), Escape schließt; Schnellanlage setzt eine Karte
  der neuen Fassung in die Spalte und zählt neu; Ziehen in eine andere Spalte speichert den Status (SortableJS);
  Speichern und Label entfernen im Panel tauschen die Karte (OOB) in der neuen Fassung, ohne doppelte Kennung.
- Neue Aufgabe: Zuweisen mit Rückfrage; Escape im Dialog stellt die vorige Auswahl wieder her.
- Alle Seiten: keine Alpine-Fehler, axe ohne kritische/schwere Befunde, kein waagerechtes Überlaufen bei 390 px.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.work.notifications.models import Notification
from apps.work.support.models import SupportTicket, SupportTicketMessage
from apps.work.tasks.models import Task, TaskLabel
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


def test_profil_reiter_und_sicherheit(
    page: Any, live_server: Any, vorsitz: Any, axe: Any, problems: BrowserProblems
) -> None:
    basis = _anmelden(page, live_server, vorsitz)
    _oeffnen(page, f"{basis}/profile/")
    reiter = page.get_by_role("navigation", name="Mein Profil")
    expect(reiter.get_by_role("link")).to_have_count(6)
    page.get_by_role("link", name="Sichtbarkeit und Kontakt").click()
    page.wait_for_load_state("networkidle")
    expect(
        page.get_by_role("main").locator('[aria-current="page"]', has_text="Sichtbarkeit und Kontakt")
    ).to_have_count(1)
    reiter.get_by_role("link", name="Daten und Aktivität").click()
    page.wait_for_load_state("networkidle")
    page.get_by_role("link", name="Aktivität", exact=True).click()
    page.wait_for_load_state("networkidle")
    assert page.url.endswith("/profile/activity/")

    _oeffnen(page, f"{basis}/profile/security/")
    wait_for_component(page, "securitySettings")
    page.get_by_role("button", name="2FA einrichten").click()
    dialog = page.get_by_role("dialog", name="2FA einrichten")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert not axe().failing, axe().describe()
    _oeffnen(page, f"{basis}/profile/notifications/")
    expect(page.get_by_role("checkbox", name="per E-Mail").first).to_be_visible()
    assert not axe().failing, axe().describe()
    page.set_viewport_size({"width": 390, "height": 844})
    for pfad in ("profile/", "profile/notifications/", "profile/security/", "profile/requests/", "profile/data/"):
        _oeffnen(page, f"{basis}/{pfad}")
        _ohne_ueberlauf(page, pfad)
    problems.assert_clean("Profil")


def test_aufgaben_board(page: Any, live_server: Any, vorsitz: Any, axe: Any, problems: BrowserProblems) -> None:
    org = vorsitz.organization
    label = TaskLabel.objects.create(organization=org, name="Presse", color="blue")
    aufgabe = Task.objects.create(
        organization=org, title="Rede vorbereiten", created_by=vorsitz, assigned_to=vorsitz, visibility="organization"
    )
    aufgabe.labels.add(label)
    page.set_viewport_size({"width": 1440, "height": 900})
    basis = _anmelden(page, live_server, vorsitz)
    _oeffnen(page, f"{basis}/tasks/")
    wait_for_component(page, "kanbanBoard")
    karte = page.locator(f"#task-card-{aufgabe.id}")
    expect(karte).to_contain_text("Presse")

    # Panel im Seitenblatt: Klick öffnet (das Blatt zeigt erst „Lädt …“, dann das Panel aus der Antwort)
    blatt = page.get_by_role("dialog", name="Aufgabe")
    with page.expect_response(lambda r: r.url.endswith("/panel/")):
        karte.click()
    expect(blatt).to_be_visible()
    titel = blatt.get_by_role("textbox", name="Aufgabentitel")
    expect(titel).to_have_value("Rede vorbereiten")

    # Speichern im Panel tauscht die Karte in der neuen Fassung (OOB über X-Work-Gestaltung)
    titel.fill("Rede für den Rat vorbereiten")
    with page.expect_response(lambda r: "/panel/action/" in r.url and r.request.method == "POST") as antwort:
        titel.press("Tab")
    assert antwort.value.ok, antwort.value.status
    expect(karte).to_contain_text("Rede für den Rat vorbereiten")
    assert "border-l-4" not in (karte.get_attribute("class") or "")
    expect(karte).to_have_count(1)  # die Karte selbst wird getauscht, keine Hülle mit derselben Kennung

    # Label im Panel entfernen: die Karte verliert es (OOB), der Label-Bereich lädt neu
    with page.expect_response(lambda r: "/panel/action/" in r.url and r.request.method == "POST"):
        blatt.locator('#panel-labels button[hx-vals*="toggle_label"]').first.click()
    expect(karte.locator("[data-card-label]")).to_have_count(0)

    # Escape schließt, Enter auf der Karte öffnet per Tastatur wieder
    page.keyboard.press("Escape")
    expect(blatt).to_be_hidden()
    karte.focus()
    with page.expect_response(lambda r: r.url.endswith("/panel/")):
        page.keyboard.press("Enter")
    expect(blatt).to_be_visible()
    expect(titel).to_have_value("Rede für den Rat vorbereiten")
    page.keyboard.press("Escape")
    expect(blatt).to_be_hidden()

    # Schnellanlage: Karte der neuen Fassung in der Spalte, Zahl neu
    spalte = page.locator("#column-in_progress")
    page.get_by_label("Neue Aufgabe in „In Bearbeitung“").fill("Presse anfragen")
    with page.expect_response(lambda r: r.url.endswith("/tasks/api/")):
        page.get_by_role("button", name="Aufgabe in „In Bearbeitung“ hinzufügen").click()
    neue = spalte.locator(".task-card", has_text="Presse anfragen")
    expect(neue).to_have_count(1)
    assert "rounded-md" in (neue.get_attribute("class") or "")
    expect(page.locator("#count-in_progress")).to_have_text("1 Aufgabe")
    expect(page.get_by_label("Neue Aufgabe in „In Bearbeitung“")).to_have_value("")

    # Ziehen in „Erledigt“ speichert den Status
    with page.expect_response(lambda r: r.url.endswith("/tasks/api/") and r.request.method == "POST") as zug:
        karte.drag_to(page.locator("#column-done"))
    assert zug.value.ok, zug.value.status
    aufgabe.refresh_from_db()
    assert aufgabe.status == "done"

    befunde = axe().failing
    assert not befunde, axe().describe()
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload()
    wait_for_bundle(page)
    _ohne_ueberlauf(page, "Aufgaben")
    problems.assert_clean("Aufgaben")


def test_neue_aufgabe_zuweisen(
    page: Any, live_server: Any, vorsitz: Any, make_member: Any, problems: BrowserProblems
) -> None:
    kollegin = make_member(vorsitz.organization, ["tasks.view"], email="kollegin@example.org")
    kollegin.user.first_name, kollegin.user.last_name = "Karla", "Kollegin"
    kollegin.user.save(update_fields=["first_name", "last_name"])
    basis = _anmelden(page, live_server, vorsitz)
    _oeffnen(page, f"{basis}/tasks/create/")
    wait_for_component(page, "assignmentHandler")
    auswahl = page.locator('select[name="assigned_to"]')
    dialog = page.get_by_role("dialog", name="Aufgabe zuweisen")

    auswahl.select_option(str(kollegin.id))
    expect(dialog).to_contain_text("Karla Kollegin")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(auswahl).to_have_value("")

    auswahl.select_option(str(kollegin.id))
    dialog.get_by_role("button", name="Zuweisen").click()
    expect(dialog).to_be_hidden()
    expect(auswahl).to_have_value(str(kollegin.id))
    page.get_by_label("Titel").fill("Haushaltsrede")
    page.get_by_role("button", name="Aufgabe erstellen").click()
    page.wait_for_load_state("networkidle")
    assert Task.objects.get(title="Haushaltsrede").assigned_to_id == kollegin.id

    # Sichtbarkeit: Vorauswahl vom Server, in der bisherigen Seite färbt Alpine die Wahl und zeigt den Hinweis
    _oeffnen(page, f"{basis}/tasks/create/")
    expect(page.locator('input[name="visibility"][value="private"]')).to_be_checked()
    page.goto(f"{basis}/tasks/create/?ansicht=bisher")
    wait_for_bundle(page)
    expect(page.locator('input[name="visibility"][value="private"]')).to_be_checked()
    hinweis = page.get_by_text("Nach dem Erstellen können Sie Personen zum Teilen hinzufügen.")
    expect(hinweis).to_be_hidden()
    page.locator('input[name="visibility"][value="shared"]').check()
    expect(hinweis).to_be_visible()
    problems.assert_clean("Neue Aufgabe")

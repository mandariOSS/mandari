# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumente im neuen Erscheinungsbild im Browser (Issue #951, Teilpaket 1).

- Liste: Ordner anlegen über den gemeinsamen Dialog (Escape schließt, Fokus kehrt zurück), Ansicht Tabelle/Kacheln,
  Mehrfachauswahl verschiebt in einen Ordner, ganze Zeile öffnet den Editor, Filter sendet bei Auswahl ab.
- Neues Dokument: „Weiter zum Editor“ erst mit Titel, Standardvorlage der gewählten Art (createMotion über das
  Feldmodell der Bausteine), Anlegen führt in den Editor.
- Alle sechs Seiten: axe ohne kritische/schwere Befunde, kein waagerechtes Überlaufen bei 1280, 1920 und 390 px.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.work.motions.models import DocumentFolder, Motion, MotionTemplate, MotionType
from tests_e2e.conftest import ADMIN_PASSWORD, BrowserProblems, wait_for_bundle

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def vorsitz(admin: Any) -> Any:
    org = admin.organization
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    Motion.objects.create(organization=org, author=admin, title="Antrag: Musterweg (Demo)", visibility="organization")
    return admin


def _anmelden(page: Any, live_server: Any, membership: Any) -> None:
    page.goto(f"{live_server.url}/accounts/login/")
    wait_for_bundle(page)
    page.fill("input[name=email]", membership.user.email)
    page.fill("input[name=password]", ADMIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def test_liste_ordner_ansicht_und_verschieben(
    page: Any, live_server: Any, vorsitz: Any, problems: BrowserProblems
) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    _anmelden(page, live_server, vorsitz)
    page.goto(f"{live_server.url}/work/{vorsitz.organization.slug}/documents/")
    wait_for_bundle(page)
    expect(page.locator('main[data-gestaltung="neu"]')).to_have_count(1)

    # Ordner anlegen: gemeinsamer Dialog, Escape schließt ihn
    ordner = page.get_by_role("complementary", name="Ordner")
    knopf = ordner.get_by_role("button", name="Ordner anlegen")
    knopf.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(dialog).to_contain_text("Neuer Ordner")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    knopf.click()
    dialog.get_by_label("Name").fill("Haushalt 2027")
    dialog.get_by_role("button", name="Ordner anlegen").click()
    page.wait_for_load_state("networkidle")
    assert DocumentFolder.objects.filter(organization=vorsitz.organization, name="Haushalt 2027").exists()
    expect(ordner).to_contain_text("Haushalt 2027")

    page.goto(f"{live_server.url}/work/{vorsitz.organization.slug}/documents/")
    wait_for_bundle(page)

    # Ansicht: Kacheln und zurück (gemerkt in localStorage)
    tabelle, kacheln = page.get_by_test_id("dokumente-tabelle"), page.get_by_test_id("dokumente-kacheln")
    expect(tabelle).to_be_visible()
    page.get_by_role("button", name="Kacheln").click()
    expect(kacheln).to_be_visible()
    expect(tabelle).to_be_hidden()
    page.get_by_role("button", name="Tabelle").click()
    expect(tabelle).to_be_visible()

    # Mehrfachauswahl: verschieben in den neuen Ordner
    tabelle.get_by_label("„Antrag: Musterweg (Demo)“ zum Verschieben auswählen").check()
    leiste = page.get_by_role("region", name="Auswahl verschieben")
    expect(leiste).to_contain_text("1 ausgewählt")
    leiste.get_by_label("Verschieben nach").select_option(label="Haushalt 2027")
    with page.expect_response(lambda r: "move-to-folder" in r.url) as antwort:
        leiste.get_by_role("button", name="Verschieben").click()
    assert antwort.value.ok, antwort.value.status
    page.wait_for_load_state("networkidle")
    antrag = Motion.objects.get(title="Antrag: Musterweg (Demo)")
    assert antrag.folder is not None and antrag.folder.name == "Haushalt 2027"

    # Filter senden bei Auswahl ab und behalten die Auswahl
    page.locator("#filter-status").select_option("draft")
    page.wait_for_url("**status=draft**")
    expect(page.locator("#filter-status")).to_have_value("draft")

    # Die ganze Zeile öffnet den Editor
    page.goto(f"{live_server.url}/work/{vorsitz.organization.slug}/documents/")
    wait_for_bundle(page)
    page.get_by_test_id("dokumente-tabelle").locator("tbody tr").first.locator("td").nth(2).click()
    page.wait_for_url(f"**/documents/{antrag.id}/**")
    problems.assert_clean("Dokumentenliste")


def test_neues_dokument_mit_standardvorlage(
    page: Any, live_server: Any, vorsitz: Any, problems: BrowserProblems
) -> None:
    org = vorsitz.organization
    art = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage", is_default=True)
    MotionTemplate.objects.create(
        organization=org, name="Anfrage kurz", motion_type=art, is_default=True, content_template="<p>Frage</p>"
    )
    page.set_viewport_size({"width": 1440, "height": 900})
    _anmelden(page, live_server, vorsitz)
    page.goto(f"{live_server.url}/work/{org.slug}/documents/create/")
    wait_for_bundle(page)

    weiter = page.get_by_role("button", name="Weiter zum Editor")
    expect(weiter).to_be_disabled()
    expect(page.locator("#template")).to_have_value(str(MotionTemplate.objects.get().id))
    expect(page.get_by_test_id("vorlage-angaben")).to_contain_text("Art: Anfrage")
    page.locator("#title").fill("Anfrage zur Straßenbeleuchtung")
    expect(weiter).to_be_enabled()
    weiter.click()
    page.wait_for_url("**/documents/*/")
    neu = Motion.objects.get(title="Anfrage zur Straßenbeleuchtung")
    assert neu.document_type == art and neu.template is not None
    problems.assert_clean("Neues Dokument")


@pytest.mark.parametrize(
    ("name", "pfad"),
    [
        ("dokumente", "documents/"),
        ("neues-dokument", "documents/create/"),
        ("import", "documents/import/"),
        ("papierkorb", "documents/trash/"),
        ("freigaben", "freigaben/"),
        ("einreichen", "submit"),
    ],
)
def test_seiten_barrierefrei_ohne_ueberlauf(
    page: Any,
    live_server: Any,
    vorsitz: Any,
    axe: Any,
    screenshot: Any,
    problems: BrowserProblems,
    name: str,
    pfad: str,
) -> None:
    org = vorsitz.organization
    if pfad == "submit":
        pfad = f"documents/{Motion.objects.get(organization=org).id}/submit-ris/"
    _anmelden(page, live_server, vorsitz)
    url = f"{live_server.url}/work/{org.slug}/{pfad}"
    for breite, hoehe in ((1280, 900), (1920, 1080), (390, 844)):
        page.set_viewport_size({"width": breite, "height": hoehe})
        page.goto(url)
        wait_for_bundle(page)
        expect(page.locator('main[data-gestaltung="neu"]')).to_have_count(1)
        ueberlauf = page.evaluate("() => document.scrollingElement.scrollWidth - window.innerWidth")
        assert ueberlauf <= 0, f"{name} bei {breite} px: {ueberlauf} px waagerecht übergelaufen"
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(url)
    wait_for_bundle(page)
    ergebnis = axe()
    assert not ergebnis.failing, f"axe auf {name}:\n{ergebnis.describe()}"
    screenshot(f"work-951-{name}")
    problems.assert_clean(name)

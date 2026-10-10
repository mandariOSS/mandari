# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Navigation des neuen Rahmens im Browser, Teil 2 (Issue #852, Entscheidung vom 06.10.2026).

Geprüft: „Fraktionssitzungen“ ist ein eigener Eintrag der Seitenleiste. Die Unterpunkte der Recherche klappen per
Knopf (auch per Tastatur) auf und zu, führen auf die Seiten des Ratsinformationssystems (aria-current „page“ am
Unterpunkt, „true“ am Bereich) und bleiben offen, bis man sie wieder zuklappt. Im Druck fehlen Seitenleiste,
Kopfzeile, Reiter und Leiste unten, der Inhalt beginnt am linken Rand. axe ohne schwere Befunde im Rahmen.
Brotkrumen nennen auf Formularseiten den Seitentitel und führen zurück; die Sitzungen der Gremien stehen nur unter
„Sitzungen › Alle Gremien“.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import expect

from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import AXE_PATH, AxeResult, BrowserProblems


@pytest.fixture
def neu(admin: Any) -> Any:
    organisation = admin.organization
    organisation.work_new_design = True
    organisation.save(update_fields=["work_new_design"])
    return admin


def _axe_rahmen(page: Any) -> AxeResult:
    """axe-core nur auf dem Rahmen (der Inhalt der Seiten gehört zu ihren eigenen Issues)."""
    page.add_script_tag(content=AXE_PATH.read_text(encoding="utf-8"))
    ergebnis = page.evaluate(
        "async () => await axe.run({ exclude: [['.work-page-content']] }, { resultTypes: ['violations'], "
        "runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })"
    )
    return AxeResult(violations=list(ergebnis.get("violations", [])))


class TestNavigation:
    def test_fraktionssitzungen_und_recherche_aufklappen(
        self, page: Any, goto: Any, login: Any, neu: Any, screenshot: Any, problems: BrowserProblems
    ) -> None:
        login(neu.user.email, PASSWORD)
        slug = neu.organization.slug
        goto(f"/work/{slug}/documents/")
        leiste = page.locator("#work-navigation")
        knopf = leiste.locator("button[aria-controls=leiste-recherche]")
        unterpunkte = page.locator("#leiste-recherche")
        expect(leiste.get_by_role("link", name="Fraktionssitzungen", exact=True)).to_be_visible()
        expect(knopf).to_have_attribute("aria-expanded", "false")
        expect(unterpunkte).to_be_hidden()

        knopf.click()
        expect(knopf).to_have_attribute("aria-expanded", "true")
        expect(unterpunkte).to_be_visible()
        ergebnis = _axe_rahmen(page)
        assert not ergebnis.failing, ergebnis.describe()
        screenshot("work-navigation-recherche-offen")

        unterpunkte.get_by_role("link", name="Vorgänge", exact=True).click()
        page.wait_for_url(f"**/work/{slug}/ris/papers/")
        expect(unterpunkte.get_by_role("link", name="Vorgänge", exact=True)).to_have_attribute("aria-current", "page")
        expect(leiste.get_by_role("link", name="Recherche", exact=True)).to_have_attribute("aria-current", "true")

        # Aufgeklappt bleibt aufgeklappt, auch in anderen Bereichen
        goto(f"/work/{slug}/faction/")
        expect(unterpunkte).to_be_visible()
        expect(leiste.get_by_role("link", name="Fraktionssitzungen", exact=True)).to_have_attribute(
            "aria-current", "page"
        )
        knopf.focus()
        page.keyboard.press("Enter")
        expect(unterpunkte).to_be_hidden()
        expect(knopf).to_be_focused()
        goto(f"/work/{slug}/")
        expect(unterpunkte).to_be_hidden()
        problems.assert_clean("Fraktionssitzungen und Recherche aufklappen")

    @pytest.mark.parametrize("breite", [1280, 1920, 2560])
    def test_brotkrumen_nennen_die_seite_und_fuehren_zurueck(
        self, page: Any, goto: Any, login: Any, neu: Any, screenshot: Any, problems: BrowserProblems, breite: int
    ) -> None:
        """Formularseiten nennen ihren Titel als letzte Krume; der Bereich davor führt zurück (Issue #951)."""
        page.set_viewport_size({"width": breite, "height": 900})
        login(neu.user.email, PASSWORD)
        slug = neu.organization.slug
        goto(f"/work/{slug}/organization/members/")
        krumen = page.locator("nav[aria-label=Brotkrumen]")
        expect(krumen.locator("[aria-current=page]")).to_have_text("Mitglieder")
        screenshot(f"work-brotkrumen-mitglieder-{breite}")
        krumen.get_by_role("link", name="Einstellungen", exact=True).click()
        page.wait_for_url(f"**/work/{slug}/organization/")
        expect(krumen.locator("[aria-current=page]")).to_have_text("Einstellungen")
        expect(krumen.locator("li")).to_have_count(2)
        problems.assert_clean("Brotkrumen")

    def test_sitzungen_der_gremien_nur_unter_sitzungen(
        self, page: Any, goto: Any, login: Any, neu: Any, screenshot: Any
    ) -> None:
        """Die Recherche verweist nicht ein zweites Mal auf die Sitzungen der Gremien (Issue #951)."""
        page.set_viewport_size({"width": 390, "height": 844})
        login(neu.user.email, PASSWORD)
        slug = neu.organization.slug
        goto(f"/work/{slug}/meetings/")
        reiter = page.locator("nav[aria-label=Sitzungen]")
        expect(reiter.get_by_role("link", name="Alle Gremien", exact=True)).to_be_visible()
        screenshot("work-sitzungen-reiter-390")
        page.set_viewport_size({"width": 1280, "height": 900})
        goto(f"/work/{slug}/ris/")
        expect(page.locator("#leiste-recherche")).to_be_visible()
        expect(page.locator("#leiste-recherche").get_by_role("link", name="Vorgänge", exact=True)).to_be_visible()
        expect(page.locator("#work-navigation").locator(f"a[href='/work/{slug}/ris/meetings/']")).to_have_count(0)

    @pytest.mark.parametrize("breite", [1280, 390])
    def test_druck_ohne_rahmen(self, page: Any, goto: Any, login: Any, neu: Any, breite: int) -> None:
        page.set_viewport_size({"width": breite, "height": 900})
        login(neu.user.email, PASSWORD)
        goto(f"/work/{neu.organization.slug}/meetings/")
        expect(page.locator("nav[aria-label=Sitzungen]")).to_be_visible()
        page.emulate_media(media="print")
        for auswahl in (
            "#work-navigation",
            "header.sticky",
            "nav[aria-label=Hauptbereiche]",
            "nav[aria-label=Sitzungen]",
        ):
            expect(page.locator(auswahl)).to_be_hidden()
        expect(page.locator("h1.work-page-title")).to_be_visible()
        links = page.evaluate("() => document.getElementById('inhalt').getBoundingClientRect().left")
        assert links < 1, links
        page.emulate_media(media="screen")
        expect(page.locator("nav[aria-label=Sitzungen]")).to_be_visible()

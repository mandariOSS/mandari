# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neuer Rahmen von Work im Browser (Issue #852), eingeschaltet je Organisation.

Geprüft: Bereiche, Reiter und Brotkrumen auf den Kernseiten ohne schwere axe-Befunde und ohne Fehler im Browser,
Strg+K setzt den Fokus in die Suche, Raum-Dialog und Blatt „Mehr“ schließen mit Escape und geben den Fokus an den
auslösenden Knopf zurück, die schmale Leiste bleibt bedienbar, der Inhalt nutzt breite Bildschirme (ab 1.440 px
rechts höchstens ein Viertel frei, #841) und läuft am Handy nicht seitlich über. Ohne Schalter bleibt der bisherige
Rahmen. Screenshots hell und dunkel landen als CI-Artefakt.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import expect

from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import AXE_PATH, AxeResult, BrowserProblems

#: Rechter Rand des Inhalts von Kopfband und Inhalt (ohne Innenabstand) und seitliches Überlaufen
MESSUNG = """() => {
  const rechts = [...document.querySelectorAll('.work-page-header, .work-page-content')].map((el) => {
    const r = el.getBoundingClientRect();
    return r.right - parseFloat(getComputedStyle(el).paddingRight);
  });
  return {breite: window.innerWidth, rechts: Math.max(...rechts), scroll: document.documentElement.scrollWidth};
}"""

SEITEN = ("", "meetings/", "faction/", "ris/", "ris/search/", "documents/", "tasks/", "team/")
#: Höchstwartezeit, bis der Fokus nach dem Schließen eines Dialogs wieder auf dem auslösenden Knopf liegt
FOKUS_RUECKGABE_MS = 2000


@pytest.fixture
def neu(admin: Any) -> Any:
    organisation = admin.organization
    organisation.work_new_design = True
    organisation.save(update_fields=["work_new_design"])
    return admin


def _axe_rahmen(page: Any) -> AxeResult:
    """axe-core nur auf dem Rahmen: Der Inhalt der Seiten gehört zu ihren eigenen Issues (#851 ff.)."""
    page.add_script_tag(content=AXE_PATH.read_text(encoding="utf-8"))
    ergebnis = page.evaluate(
        "async () => await axe.run({ exclude: [['.work-page-content']] }, { resultTypes: ['violations'], "
        "runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'] } })"
    )
    return AxeResult(violations=list(ergebnis.get("violations", [])))


def _fokus(page: Any) -> str:
    return str(
        page.evaluate(
            "() => document.activeElement && (document.activeElement.id || document.activeElement.getAttribute('aria-controls') || '')"
        )
    )


class TestNeuerRahmen:
    def test_kernseiten_barrierefrei(
        self,
        page: Any,
        goto: Any,
        login: Any,
        neu: Any,
        screenshot: Any,
        dark_mode: Any,
        problems: BrowserProblems,
    ) -> None:
        login(neu.user.email, PASSWORD)
        slug = neu.organization.slug
        for pfad in SEITEN:
            goto(f"/work/{slug}/{pfad}")
            expect(page.locator('html[data-rahmen="neu"]')).to_have_count(1)
            expect(page.locator('#work-navigation [aria-current="page"]').first).to_be_visible()
            ergebnis = _axe_rahmen(page)
            assert not ergebnis.failing, f"{pfad or 'start'}:\n{ergebnis.describe()}"
            screenshot(f"work-rahmen-{pfad.strip('/').replace('/', '-') or 'start'}")
        dark_mode(True)
        ergebnis = _axe_rahmen(page)
        assert not ergebnis.failing, ergebnis.describe()
        screenshot("work-rahmen-team-dunkel")
        problems.assert_clean("Kernseiten im neuen Rahmen")

    def test_suche_mit_strg_k_und_raum_dialog(
        self, page: Any, goto: Any, login: Any, neu: Any, problems: BrowserProblems
    ) -> None:
        login(neu.user.email, PASSWORD)
        goto(f"/work/{neu.organization.slug}/documents/")
        page.locator("body").click(position={"x": 600, "y": 500})
        page.keyboard.press("Control+k")
        assert _fokus(page) == "kopf-suche"

        knopf = page.locator("#work-navigation button[aria-controls=raum-dialog]")
        knopf.click()
        expect(page.locator("#raum-dialog")).to_be_visible()
        expect(page.locator('#raum-dialog a[aria-current="true"]')).to_contain_text(neu.organization.name)
        page.keyboard.press("Escape")
        expect(page.locator("#raum-dialog")).to_be_hidden()
        # Der Fokus kehrt nach dem Schließen asynchron zurück ($nextTick bzw. Rückgabe der Fokusfalle, je ein
        # setTimeout): abwarten statt sofort abfragen, ein verlorener Fokus scheitert weiterhin
        expect(knopf).to_be_focused(timeout=FOKUS_RUECKGABE_MS)
        problems.assert_clean("Suche und Raum-Dialog")

    def test_schmale_leiste(self, page: Any, goto: Any, login: Any, neu: Any, screenshot: Any) -> None:
        login(neu.user.email, PASSWORD)
        goto(f"/work/{neu.organization.slug}/ris/")
        page.locator("button[aria-controls=work-navigation]").click()
        expect(page.locator('html[data-leiste="schmal"]')).to_have_count(1)
        breite = page.evaluate("() => document.getElementById('work-navigation').getBoundingClientRect().width")
        assert breite == 64
        screenshot("work-rahmen-schmal")
        # Die Wahl bleibt beim nächsten Aufruf erhalten (gleicher Schlüssel wie im bisherigen Rahmen)
        goto(f"/work/{neu.organization.slug}/")
        expect(page.locator('html[data-leiste="schmal"]')).to_have_count(1)

    @pytest.mark.parametrize("breite", [1280, 1440, 1920, 2560])
    def test_breite_bildschirme(self, page: Any, goto: Any, login: Any, neu: Any, screenshot: Any, breite: int) -> None:
        login(neu.user.email, PASSWORD)
        page.set_viewport_size({"width": breite, "height": 900})
        for pfad in ("faction/", "ris/papers/", "team/"):
            goto(f"/work/{neu.organization.slug}/{pfad}")
            messung = page.evaluate(MESSUNG)
            assert messung["scroll"] <= breite, (pfad, messung)
            if breite >= 1440:
                frei = (breite - messung["rechts"]) / breite
                assert frei <= 0.25, (pfad, messung)
        screenshot(f"work-rahmen-team-{breite}")

    def test_handy_leiste_unten_und_blatt_mehr(
        self, page: Any, goto: Any, login: Any, neu: Any, screenshot: Any, problems: BrowserProblems
    ) -> None:
        page.set_viewport_size({"width": 390, "height": 844})
        login(neu.user.email, PASSWORD)
        goto(f"/work/{neu.organization.slug}/faction/")
        leiste = page.get_by_role("navigation", name="Hauptbereiche")
        expect(leiste).to_be_visible()
        expect(leiste.locator('[aria-current="page"]')).to_contain_text("Sitzungen")
        assert page.evaluate("() => document.documentElement.scrollWidth") <= 390
        screenshot("work-rahmen-handy")

        mehr = page.locator("button[aria-controls=work-mehr]")
        mehr.click()
        blatt = page.locator("#work-mehr")
        expect(blatt).to_be_visible()
        expect(blatt.get_by_role("link", name="Aufgaben")).to_be_visible()
        ergebnis = _axe_rahmen(page)
        assert not ergebnis.failing, ergebnis.describe()
        screenshot("work-rahmen-handy-mehr")
        page.keyboard.press("Escape")
        expect(blatt).to_be_hidden()
        expect(mehr).to_be_focused(timeout=FOKUS_RUECKGABE_MS)
        problems.assert_clean("Leiste unten und Blatt Mehr")


def test_ohne_schalter_bleibt_der_bisherige_rahmen(page: Any, goto: Any, login: Any, admin: Any) -> None:
    login(admin.user.email, PASSWORD)
    goto(f"/work/{admin.organization.slug}/")
    expect(page.locator("aside.work-sidebar")).to_have_count(1)
    expect(page.locator('html[data-rahmen="neu"]')).to_have_count(0)

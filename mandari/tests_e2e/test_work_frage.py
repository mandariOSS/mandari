# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fragen an die Ratsdaten in Work (Issue #853) im Browser: Fragefeld auf der Recherche-Suche aufklappen, fragen, die
Antwort steht per HTMX über den Treffern mit Links und Quellen in Work; barrierefrei ohne schwere axe-Befunde, ab
1.280 px ohne leere rechte Hälfte, am Handy ohne seitliches Überlaufen. Der KI-Anbieter ist ersetzt (kein Aufruf
ins Netz), der Suchdienst ebenso.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import expect

from apps.work.ris import fragen
from insight_ai.tests.anbieter import SkriptAnbieter, antwort_sitzungen
from insight_ai.tests.musterstadt import FakeSuche, Musterstadt, baue_musterstadt
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import BrowserProblems

AUSGETAUSCHT = "() => !document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling')"
#: Wartet, bis die Seite nicht mehr scrollt (htmx scrollt die Antwort ins Bild), dann nach oben
RUHE_DANN_OBEN = """async () => {
  const bild = () => new Promise((fertig) => requestAnimationFrame(() => requestAnimationFrame(fertig)));
  let vorher = -1;
  while (window.scrollY !== vorher) { vorher = window.scrollY; await bild(); await bild(); }
  window.scrollTo({top: 0, behavior: 'instant'});
}"""


class _Suchdienst(FakeSuche):
    """Suchdienst der Werkzeuge (search_all aus den Tests von #899), die Suchseite selbst ohne Treffer."""

    def facet_counts(self, query: str, **_: Any) -> dict[str, Any]:
        return {"paper_types": {}, "periods": {}}

    def search_grouped(self, query: str, **_: Any) -> dict[str, Any]:
        return {
            "groups": [],
            "counts": {"vorgaenge": 0, "unterlagen": 0, "meetings": 0},
            "totals_by_index": {},
            "similar_spelling": False,
            "has_more": False,
        }


@pytest.fixture
def frage(admin: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    stadt: Musterstadt = baue_musterstadt()
    organisation = admin.organization
    organisation.body = stadt.body
    organisation.work_new_design = True
    organisation.ai_enabled = True
    organisation.save(update_fields=["body", "work_new_design", "ai_enabled"])
    werkzeuge = _Suchdienst()
    monkeypatch.setattr("insight_core.services.search_service.get_search_service", lambda: werkzeuge)
    schritte: list[Any] = [
        lambda _m: [("sitzungen_im_zeitraum", {"von": "2026-10-05", "bis": "2026-10-11"})],
        antwort_sitzungen,
    ]
    monkeypatch.setattr(fragen, "anbieter", lambda organization: SkriptAnbieter(list(schritte)))
    return {"admin": admin, "stadt": stadt, "slug": organisation.slug}


@pytest.mark.parametrize("breite", [1280, 1440, 1920, 2560, 390])
def test_frage_an_die_ratsdaten(
    page: Any,
    goto: Any,
    login: Any,
    frage: dict[str, Any],
    axe: Any,
    screenshot: Any,
    problems: BrowserProblems,
    breite: int,
) -> None:
    page.set_viewport_size({"width": breite, "height": 900 if breite > 500 else 844})
    login(frage["admin"].user.email, PASSWORD)
    goto(f"/work/{frage['slug']}/ris/search/")

    page.get_by_text("Fragen an die Ratsdaten").click()
    page.get_by_label("Ihre Frage an die Ratsdaten").fill("Welche Sitzungen finden diese Woche statt?")
    page.get_by_role("button", name="Fragen", exact=True).click()

    antwort = page.locator("#ki-antwort section")
    expect(antwort).to_contain_text("maßgeblich sind die Quellen")
    rat = f"/work/{frage['slug']}/ris/meetings/{frage['stadt'].rat_sitzung.pk}/"
    expect(antwort.locator(f'a[href="{rat}"]').first).to_be_visible()
    expect(antwort.get_by_role("heading", name="Quellen")).to_be_visible()
    assert page.evaluate("() => document.documentElement.scrollWidth") <= breite
    if breite >= 1280:
        # Eine Fläche über die Breite des Inhalts, Quellen daneben: keine leere rechte Hälfte
        rechts = page.evaluate("() => document.querySelector('#ki-antwort section').getBoundingClientRect().right")
        assert (breite - rechts) / breite <= 0.25, f"Rechts mehr als ein Viertel frei bei {breite} px: {rechts}"
    # Die Antwort rückt nach dem Einschwingen ins Bild (show:#ki-antwort:top); axe zählt Bedienelemente unter der
    # klebenden Kopfzeile als verdeckt, also erst nach dem Austausch und dem Scrollen von oben prüfen
    page.wait_for_function(AUSGETAUSCHT)
    page.evaluate(RUHE_DANN_OBEN)
    page.wait_for_function("() => window.scrollY === 0")
    ergebnis = axe()
    assert not ergebnis.failing, ergebnis.describe()
    screenshot(f"work-frage-{breite}")
    problems.assert_clean("Frage an die Ratsdaten")

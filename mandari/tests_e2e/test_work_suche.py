# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Recherche-Suche in Work im neuen Rahmen (Issue #853) im Browser.

Geprüft: Treffer nach Vorgang gruppiert mit Kontextzeile und Bezug („Eigener Antrag“), barrierefrei ohne schwere
axe-Befunde (ganze Seite, hell und dunkel), keine Fehler im Browser; ab 1.280 px rechts höchstens ein Viertel frei,
ab 1.536 px Filterspalte neben den Treffern, am Handy kein seitliches Überlaufen. Gremium und Reiter filtern per HTMX
und schreiben die Adresse fort. Der Suchdienst ist nachgebaut (kein Elasticsearch in der CI).
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import expect

from apps.work.motions.models import Motion
from insight_core.models import OParlBody, OParlOrganization, OParlPaper, OParlSource
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import BrowserProblems

RIS = "https://ris.suche.e2e/oparl"

#: Rechter Rand von Treffern und Filtern, Lage der Filterspalte und seitliches Überlaufen
MESSUNG = """() => {
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return {l: r.left, r: r.right, h: r.height}; };
  const teile = [...document.querySelectorAll('#treffer-liste > li, #suchergebnis select, #suchergebnis summary, #suchergebnis nav')]
    .map((el) => el.getBoundingClientRect()).filter((r) => r.width > 0);
  return {
    breite: window.innerWidth,
    rechts: Math.max(...teile.map((r) => r.right)),
    scroll: document.documentElement.scrollWidth,
    liste: box(document.querySelector('#treffer-liste')),
    filter: box(document.querySelector('#suche-sortierung')?.closest('[class*="2xl:sticky"]')),
    // Höchste sichtbare Bedienelemente im Ergebnisbereich (Filterknöpfe, Auswahllisten, Reiter): Maße von Work
    bedien: Math.max(...[...document.querySelectorAll('#suchergebnis select, #suchergebnis summary, #suchergebnis nav a')]
      .map((el) => el.getBoundingClientRect()).filter((r) => r.width > 0).map((r) => r.height)),
  };
}"""
AUSGETAUSCHT = "() => !document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling')"


class _Suchdienst:
    """Suchdienst ohne Elasticsearch: jeder Vorgang trifft mit einem Ausschnitt; merkt sich die Aufrufe."""

    def __init__(self, papiere: list[OParlPaper]) -> None:
        self.papiere = papiere
        self.aufrufe: list[dict[str, Any]] = []

    def facet_counts(self, query: str, **_: Any) -> dict[str, Any]:
        return {
            "paper_types": {"Beschlussvorlage": 4, "Antrag": 2},
            "periods": {"12m": 5, "2y": 6, "5y": 6, "older": 0},
        }

    def search_grouped(self, query: str, **kwargs: Any) -> dict[str, Any]:
        from insight_core.services.search_service import HIGHLIGHT_POST, HIGHLIGHT_PRE

        self.aufrufe.append(kwargs)
        gruppen = [
            {
                "kind": "paper",
                "key": str(p.pk),
                "paper": {
                    "id": str(p.pk),
                    "name": p.name,
                    "reference": p.reference,
                    "paper_type": p.paper_type,
                    "organization_names": ["Ausschuss für Umwelt und Klimaschutz"],
                },
                "file": {
                    "id": f"00000000-0000-4000-8000-{n:012d}",
                    "name": "Anlage 2 – Begründung",
                    "text_content": "roh",
                    "_formatted": {
                        "text_content": f"Die Verwaltung schlägt vor, am {HIGHLIGHT_PRE}Stadtpark{HIGHLIGHT_POST} "
                        "öffentliche Trinkwasserbrunnen aufzustellen und die Wege barrierefrei zu erneuern."
                    },
                },
                "others": [],
            }
            for n, p in enumerate(self.papiere)
        ]
        return {
            "groups": gruppen if kwargs.get("page_size", 20) > 1 else [],
            "counts": {"vorgaenge": len(gruppen), "unterlagen": 0, "meetings": 0},
            "totals_by_index": {"meetings": 2, "files": 5, "persons": 1, "organizations": 1},
            "similar_spelling": False,
            "has_more": False,
        }


@pytest.fixture
def suche(admin: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from insight_core.services import search_service

    organisation = admin.organization
    quelle, _ = OParlSource.objects.get_or_create(url=f"{RIS}/system", defaults={"name": "E2E-RIS Suche"})
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=quelle, name="Suchstadt")
    for key, name in (("umwelt", "Ausschuss für Umwelt und Klimaschutz"), ("bau", "Ausschuss für Bauen und Verkehr")):
        OParlOrganization.objects.create(
            external_id=f"{RIS}/org/{key}", body=body, name=name, classification="Ausschuss"
        )
    papiere = [
        OParlPaper.objects.create(
            external_id=f"{RIS}/paper/{n}",
            body=body,
            name=name,
            reference=f"V/2026/{n:04d}",
            paper_type="Antrag" if "Antrag" in name else "Beschlussvorlage",
        )
        for n, name in enumerate(
            (
                "Antrag: Trinkwasserbrunnen im Stadtpark",
                "Neugestaltung des Stadtparks",
                "Beleuchtung der Wege im Stadtpark",
                "Spielplatz am Stadtpark, Sanierung",
                "Veranstaltungsfläche am Teich im Stadtpark",
            ),
            start=1,
        )
    ]
    Motion.objects.create(
        organization=organisation,
        author=admin,
        title="Unser Antrag",
        visibility="organization",
        related_paper=papiere[0],
    )
    organisation.body = body
    organisation.work_new_design = True
    organisation.save(update_fields=["body", "work_new_design"])
    dienst = _Suchdienst(papiere)
    monkeypatch.setattr(search_service, "get_search_service", lambda: dienst)
    return {"admin": admin, "dienst": dienst, "slug": organisation.slug}


@pytest.mark.parametrize("breite", [1280, 1440, 1536, 1920, 2560, 390])
def test_suche(
    page: Any,
    goto: Any,
    login: Any,
    suche: dict[str, Any],
    axe: Any,
    screenshot: Any,
    dark_mode: Any,
    problems: BrowserProblems,
    breite: int,
) -> None:
    page.set_viewport_size({"width": breite, "height": 900 if breite > 500 else 844})
    login(suche["admin"].user.email, PASSWORD)
    goto(f"/work/{suche['slug']}/ris/search/?q=Stadtpark")

    erster = page.locator("#treffer-liste > li").first
    expect(erster).to_contain_text("Trinkwasserbrunnen im Stadtpark")
    expect(erster).to_contain_text("Eigener Antrag")
    expect(erster).to_contain_text("Ausschuss für Umwelt und Klimaschutz")
    expect(page.locator("#suche-zahl")).to_have_text("5 Vorgänge")
    m = page.evaluate(MESSUNG)
    assert m["scroll"] <= breite, f"Seitliches Überlaufen bei {breite} px: {m}"
    if breite >= 1280:
        assert (breite - m["rechts"]) / breite <= 0.25, f"Rechts mehr als ein Viertel frei bei {breite} px: {m}"
    # Bedienelemente einer Arbeitsplattform: 32 px statt 44 px wie im Bürgerportal (Gestaltungsregeln, Issue #853)
    assert m["bedien"] <= 34, f"Bedienelemente höher als 32 px bei {breite} px: {m}"
    if breite >= 1536:
        assert m["filter"] and m["filter"]["h"] >= 200, "Filterspalte neben den Treffern"
        assert m["filter"]["l"] - m["liste"]["r"] <= 64, "Trefferspalte reicht bis an die Filterspalte"
    ergebnis = axe()
    assert not ergebnis.failing, ergebnis.describe()
    screenshot(f"work-suche-{breite}")
    dark_mode(True)
    ergebnis = axe()
    assert not ergebnis.failing, ergebnis.describe()
    screenshot(f"work-suche-{breite}-dunkel")
    problems.assert_clean("Suche")


def test_gremium_und_reiter_filtern_per_htmx(
    page: Any, goto: Any, login: Any, suche: dict[str, Any], problems: BrowserProblems
) -> None:
    login(suche["admin"].user.email, PASSWORD)
    goto(f"/work/{suche['slug']}/ris/search/?q=Stadtpark")

    page.select_option("#suche-gremium", "Ausschuss für Bauen und Verkehr")
    page.wait_for_url("**gremium=Ausschuss**")
    page.wait_for_function(AUSGETAUSCHT)
    expect(page.get_by_text("Gremium: Ausschuss für Bauen und Verkehr")).to_be_visible()
    assert suche["dienst"].aufrufe[-1]["organization_name"] == "Ausschuss für Bauen und Verkehr"

    page.get_by_role("navigation", name="Arten der Treffer").get_by_role("link", name="Vorgänge").click()
    page.wait_for_url("**result_type=papers**")
    page.wait_for_function(AUSGETAUSCHT)
    assert "gremium=Ausschuss" in page.url, "Der Reiter behält den Gremium-Filter"
    expect(page.locator("#treffer-liste > li")).to_have_count(5)
    problems.assert_clean("Suche filtern")

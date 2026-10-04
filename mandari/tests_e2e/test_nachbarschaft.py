# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nachbarschaftssuche im Insight-Portal (frontend/alpine/neighborhood.ts).

Regressionstest: Die Komponente las die Adressen für Vorschläge und Ergebnisse aus
``this.$el.dataset``. In ``@input``- und ``@click``-Handlern ist ``$el`` aber das auslösende
Element (Eingabefeld, Knopf), nicht die Wurzel mit den Datenattributen – Tippen im Adressfeld
brachte keine Vorschläge, ein Wechsel des Umkreises lud die falsche Seite in die Ergebnisliste.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from playwright.sync_api import expect

from insight_core.models import OParlBody, OParlPaper, OParlSource, Street
from insight_core.services.gazetteer import normalize_street_name
from insight_core.services.paper_locations import sync_paper_locations
from tests_e2e.conftest import BrowserProblems

RIS = "https://ris.e2e.example/oparl"
LAT = 51.9600
LON = 7.6200
# 0.001° Breite ≈ 111 m: rund 670 m nördlich, nur im Umkreis 1 km
FAR_LAT = LAT + 0.006


def _kommune() -> OParlBody:
    source, _ = OParlSource.objects.get_or_create(url=f"{RIS}/system", defaults={"name": "E2E-RIS"})
    body = OParlBody.objects.create(
        external_id=f"{RIS}/body/nachbarschaft",
        source=source,
        name="Musterstadt",
        slug="e2e-nachbarschaft",
        is_listed=True,
        latitude=LAT,
        longitude=LON,
    )
    Street.objects.create(
        body=body,
        osm_id=1,
        name="Lindenallee",
        normalized_name=normalize_street_name("Lindenallee"),
        latitude=LAT,
        longitude=LON,
    )
    for name, lat, year in (("Neue Bänke Lindenallee", LAT, 2025), ("Radweg am Stadtpark", FAR_LAT, 2024)):
        paper = OParlPaper.objects.create(
            external_id=f"{RIS}/paper/{year}",
            body=body,
            name=name,
            reference=f"V/{year}/001",
            date=date(year, 5, 1),
            locations=[{"lat": lat, "lon": LON, "name": "Lindenallee", "source": "street_match", "confidence": 0.9}],
        )
        sync_paper_locations(paper)
    return body


def _open(page: Any, goto: Any, body: OParlBody, query: str = "") -> None:
    # Kartenkacheln nicht von außen laden
    page.route("**/insight/tiles/**", lambda route: route.fulfill(status=204, body=""))
    goto(f"/insight/kommune/{body.id}/")
    goto(f"/insight/nachbarschaft/{query}")


def test_adressfeld_schlaegt_vor_und_zeigt_ergebnisse(page: Any, goto: Any, problems: BrowserProblems) -> None:
    body = _kommune()
    _open(page, goto, body)

    page.locator('input[x-model="searchQuery"]').fill("Linden")
    vorschlag = page.get_by_role("button", name="Lindenallee")
    expect(vorschlag).to_be_visible()
    vorschlag.click()

    ergebnisse = page.locator("#results-container")
    expect(ergebnisse).to_contain_text("1 Vorgang im Umkreis von 500")
    expect(ergebnisse.get_by_text("Neue Bänke Lindenallee")).to_be_visible()
    problems.assert_clean("Nachbarschaft: Adressfeld")


def test_umkreis_wechsel_laedt_ergebnisse_neu(page: Any, goto: Any, problems: BrowserProblems) -> None:
    body = _kommune()
    _open(page, goto, body, f"?lat={LAT}&lon={LON}&name=Lindenallee&radius=250")

    ergebnisse = page.locator("#results-container")
    expect(ergebnisse).to_contain_text("1 Vorgang im Umkreis von 250")

    page.get_by_role("button", name="1 km").click()
    expect(ergebnisse).to_contain_text("2 Vorgänge im Umkreis von 1000")
    expect(ergebnisse.get_by_text("Radweg am Stadtpark")).to_be_visible()
    # Nicht die ganze Seite in die Liste geladen
    expect(ergebnisse.locator("#neighborhood-map")).to_have_count(0)
    problems.assert_clean("Nachbarschaft: Umkreis")

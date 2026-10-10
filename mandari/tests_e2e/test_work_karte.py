# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Karte der Recherche in Work und Karte des Bürgerportals (Issue #853) im Browser – beide auf dem gemeinsamen
Kartenmodul (frontend/js/vorgangskarte.ts) und derselben Abfrage der Punkte.

- Keine Anfrage an fremde Hosts: Kacheln über den Kachel-Proxy, Leaflet aus den eigenen statischen Dateien.
- Punkte je Zeitraum (Work: Standard 12 Monate, umschaltbar), gelöschte Vorgänge fehlen; ein Klick auf einen Punkt
  führt zum Vorgang in Work bzw. in Insight.
- Neues und bisheriges Erscheinungsbild; axe ohne schwere Befunde, kein seitliches Überlaufen. Screenshots bei 390,
  1.280, 1.920 und 2.560 px.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import pytest
from django.utils import timezone
from playwright.sync_api import expect

from insight_core.models import OParlBody, OParlPaper, OParlSource, PaperLocation
from tests_e2e.conftest import ADMIN_PASSWORD as PASSWORD
from tests_e2e.conftest import BrowserProblems, wait_for_component

RIS = "https://ris.karte-e2e.example/oparl"
LAT, LON = 51.96, 7.62


def _verortet(body: OParlBody, name: str, tage: int, lat: float, lon: float, **felder: Any) -> OParlPaper:
    vorgang = OParlPaper.objects.create(
        external_id=f"{RIS}/paper/{name}",
        body=body,
        name=name,
        reference=f"V/{tage}",
        date=timezone.localdate() - timedelta(days=tage),
        **felder,
    )
    PaperLocation.objects.create(paper=vorgang, body=body, latitude=lat, longitude=lon, name=f"Ort {name}")
    return vorgang


@pytest.fixture
def karte(admin: Any) -> Any:
    source = OParlSource.objects.create(name="Karten-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(
        external_id=f"{RIS}/body/1",
        source=source,
        name="Kartenstadt",
        latitude=LAT,
        longitude=LON,
        bbox_north=LAT + 0.08,
        bbox_south=LAT - 0.08,
        bbox_east=LON + 0.12,
        bbox_west=LON - 0.12,
    )
    _verortet(body, "Spielplatz am Markt", 20, LAT, LON)
    _verortet(body, "Radweg Ring", 40, LAT + 0.04, LON + 0.06)
    _verortet(body, "Alter Brunnen", 600, LAT - 0.04, LON - 0.06)
    _verortet(body, "Gelöschter Vorgang", 10, LAT + 0.02, LON - 0.03, deleted=True)
    organisation = admin.organization
    organisation.body = body
    organisation.save(update_fields=["body"])
    return admin


def _nur_eigene_hosts(page: Any, live_server: Any) -> list[str]:
    """Merkt sich alle Anfragen an andere Hosts als den Testserver (Kacheln, Skripte, Schriften, Daten)."""
    eigener = urlparse(live_server.url).netloc
    fremde: list[str] = []

    def anfrage(request: Any) -> None:
        adresse = urlparse(request.url)
        if adresse.scheme in ("http", "https") and adresse.netloc != eigener:
            fremde.append(request.url)

    page.on("request", anfrage)
    # Kacheln liefert der Proxy im Test nicht aus dem Netz, sondern leer
    page.route("**/insight/tiles/**", lambda route: route.fulfill(status=204, body=""))
    return fremde


def _status(page: Any) -> Any:
    return page.get_by_role("status").filter(has_text="Ausschnitt")


@pytest.mark.parametrize("breite", [390, 1280, 1920, 2560])
def test_karte_neu(
    page: Any,
    goto: Any,
    login: Any,
    live_server: Any,
    karte: Any,
    axe: Any,
    screenshot: Any,
    problems: BrowserProblems,
    breite: int,
) -> None:
    organisation = karte.organization
    organisation.work_new_design = True
    organisation.save(update_fields=["work_new_design"])
    fremde = _nur_eigene_hosts(page, live_server)
    page.set_viewport_size({"width": breite, "height": 900 if breite > 500 else 844})
    login(karte.user.email, PASSWORD)

    goto(f"/work/{organisation.slug}/ris/map/")
    wait_for_component(page, "risKarte")

    expect(page.get_by_role("heading", name="Karte")).to_be_visible()
    expect(_status(page)).to_contain_text("2 Vorgänge an 2 Orten")
    page.get_by_role("button", name="3 Jahre").click()
    expect(page.get_by_role("button", name="3 Jahre")).to_have_attribute("aria-pressed", "true")
    expect(_status(page)).to_contain_text("3 Vorgänge an 3 Orten")
    assert "zeitraum=36" in page.url
    assert page.evaluate("document.documentElement.scrollWidth") <= breite
    assert not fremde, fremde

    ergebnis = axe()
    assert not ergebnis.failing, ergebnis.describe()
    screenshot(f"work-karte-{breite}")

    if breite == 1280:
        # Ein Punkt öffnet den Vorgang in Work (Popup mit Link, Titel nur als Text)
        page.locator(".leaflet-interactive").first.click(force=True)
        link = page.locator(".ris-karte-popup a")
        expect(link).to_be_visible()
        link.click()
        page.wait_for_url(f"**/work/{organisation.slug}/ris/papers/**")
    problems.assert_clean("Karte im neuen Erscheinungsbild")


def test_karte_bisher(
    page: Any, goto: Any, login: Any, live_server: Any, karte: Any, problems: BrowserProblems
) -> None:
    organisation = karte.organization
    organisation.work_new_design = False
    organisation.save(update_fields=["work_new_design"])
    fremde = _nur_eigene_hosts(page, live_server)
    page.set_viewport_size({"width": 1280, "height": 900})
    login(karte.user.email, PASSWORD)

    goto(f"/work/{organisation.slug}/ris/map/?zeitraum=alle")
    wait_for_component(page, "risKarte")

    expect(_status(page)).to_contain_text("3 Vorgänge an 3 Orten")
    expect(page.get_by_text("Gelöschter Vorgang")).to_have_count(0)
    assert not fremde, fremde
    problems.assert_clean("Karte im bisherigen Erscheinungsbild")


@pytest.mark.parametrize("breite", [390, 1280, 1920, 2560])
def test_karte_im_buergerportal_mit_dem_gemeinsamen_modul(
    page: Any,
    goto: Any,
    live_server: Any,
    karte: Any,
    axe: Any,
    screenshot: Any,
    problems: BrowserProblems,
    breite: int,
) -> None:
    body = karte.organization.body
    fremde = _nur_eigene_hosts(page, live_server)
    page.set_viewport_size({"width": breite, "height": 900 if breite > 500 else 844})
    goto(f"/insight/kommune/{body.id}/")

    goto("/insight/karte/")
    wait_for_component(page, "insightKarte")

    # Wie bisher: Zeitraum in Wochen (Standard 3 Monate), Zähler der Orte, Marker; gelöschte Vorgänge fehlen
    expect(page.get_by_text("2 Orte", exact=True)).to_be_visible()
    page.get_by_role("button", name="Alle", exact=True).click()
    expect(page.get_by_text("3 Orte", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") <= breite
    assert not fremde, fremde
    ergebnis = axe()
    assert not ergebnis.failing, ergebnis.describe()
    screenshot(f"insight-karte-{breite}")

    if breite == 1280:
        # Ein Marker öffnet das Popup (Titel nur als Text) mit dem Weg zum Vorgang in Insight
        page.get_by_role("button", name="4 Wo.", exact=True).click()
        expect(page.get_by_text("1 Orte", exact=True)).to_be_visible()
        page.locator(".custom-marker").first.click(force=True)
        popup = page.locator(".leaflet-popup-content")
        expect(popup).to_contain_text("Spielplatz am Markt")
        expect(popup).to_contain_text("Ort Spielplatz am Markt")
        popup.get_by_role("link", name="Details ansehen").click()
        page.wait_for_url("**/insight/vorgaenge/**")
    problems.assert_clean(f"Karte im Bürgerportal bei {breite} px")

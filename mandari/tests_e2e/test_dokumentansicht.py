# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokumentansicht im Browser nach Issue #914 (Soft-404): Lade- und Fehlertexte erst im jeweiligen Zustand.

Vor dem Öffnen steht im Seitentext – auch nach dem Start von Alpine, so wie rendernde Suchmaschinen die Seite lesen –
weder „Dokument wird geladen …“ noch „Vorschau nicht verfügbar“. Beim Öffnen zeigt die Ansicht den Ladetext, nach dem
Laden das Dokument und ohne Ladeereignis nach 30 Sekunden den Fehlerfall mit „Dokument öffnen“. Escape schließt sie.
Ebenso steht „noch nicht verfügbar“ im Kommunenwechsel nur noch als Datenattribut und erscheint wie bisher an
Kommunen ohne Daten.
"""

from __future__ import annotations

import io
import re
from datetime import date
from typing import Any

from playwright.sync_api import expect

from insight_core.models import OParlBody, OParlFile, OParlPaper, OParlSource
from insight_core.services.kommunenverzeichnis_import import importieren

RIS = "https://ris.dokumentansicht.e2e/oparl"
LADEN = "Dokument wird geladen …"
ANSEHEN = re.compile(r"^Ansehen\W+Beschlussvorlage Grundschule$")
FEHLER = ("Vorschau nicht verfügbar", "Das Dokument kann nicht eingebettet angezeigt werden.", "Dokument öffnen")

#: Alle Textknoten der Seite außer Skripten und Stilen, auch in verborgenen Elementen (ohne <template>-Inhalt,
#: der nicht im DOM steht)
SEITENTEXT = """() => {
  const texte = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const eltern = walker.currentNode.parentElement;
    if (eltern && eltern.closest('script, style')) continue;
    texte.push(walker.currentNode.textContent);
  }
  return texte.join(' ').replace(/\\s+/g, ' ');
}"""


def _vorgang() -> tuple[OParlPaper, OParlFile]:
    source, _ = OParlSource.objects.get_or_create(url=f"{RIS}/system", defaults={"name": "E2E-RIS Dokumentansicht"})
    body = OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Ansichtstadt", slug="e2e-ansicht", is_listed=True
    )
    paper = OParlPaper.objects.create(
        external_id=f"{RIS}/paper/1",
        body=body,
        name="Neubau der Grundschule am Park",
        reference="V/2026/100",
        paper_type="Beschlussvorlage",
        date=date(2026, 9, 1),
    )
    datei = OParlFile.objects.create(
        external_id=f"{RIS}/file/1",
        body=body,
        paper=paper,
        name="Beschlussvorlage Grundschule",
        file_name="vorlage.pdf",
        mime_type="application/pdf",
        access_url="https://ris.dokumentansicht.e2e/files/vorlage.pdf",
    )
    return paper, datei


def _ohne_zustandstexte(page: Any) -> None:
    text = page.evaluate(SEITENTEXT)
    for teil in (LADEN, *FEHLER):
        assert teil not in text, f"„{teil}“ steht im Seitentext"


def _halten(page: Any, datei: OParlFile) -> list[Any]:
    """Hält den Abruf des Dokuments für die Ansicht zurück, bis der Test ihn freigibt (oder nie)."""
    gehalten: list[Any] = []

    def halten(route: Any) -> None:
        gehalten.append(route)

    page.route(f"**/dokumente/{datei.id}/preview/", halten)
    return gehalten


def _warten(page: Any, gehalten: list[Any]) -> Any:
    for _ in range(100):
        if gehalten:
            return gehalten[0]
        page.wait_for_timeout(50)
    raise AssertionError("Abruf des Dokuments wurde nicht abgefangen")


def test_laden_und_anzeigen(page: Any, goto: Any, screenshot: Any) -> None:
    paper, datei = _vorgang()
    gehalten = _halten(page, datei)
    goto(f"/insight/vorgaenge/{paper.pk}/")
    _ohne_zustandstexte(page)

    page.get_by_role("button", name=ANSEHEN).click()
    expect(page.get_by_text(LADEN)).to_be_visible()
    expect(page.get_by_text("Vorschau nicht verfügbar")).to_have_count(0)
    route = _warten(page, gehalten)
    # Einblenden (200 ms, Fenster 75 ms später) abwarten, dann zeigt das Bild den Ladezustand
    page.wait_for_timeout(400)
    screenshot("dokumentansicht-laden")

    route.fulfill(
        status=200,
        content_type="text/html; charset=utf-8",
        body="<!doctype html><title>Vorlage</title><p>Inhalt der Beschlussvorlage</p>",
    )
    ansicht = page.frame_locator('iframe[title="Dokumentvorschau"]')
    expect(ansicht.get_by_text("Inhalt der Beschlussvorlage")).to_be_visible()
    expect(page.get_by_text(LADEN)).to_have_count(0)
    _ohne_zustandstexte(page)
    screenshot("dokumentansicht-geladen")

    page.keyboard.press("Escape")
    expect(page.locator('iframe[title="Dokumentvorschau"]')).to_have_count(0)
    _ohne_zustandstexte(page)


def test_fehlerfall_ohne_ladeereignis(page: Any, goto: Any, screenshot: Any) -> None:
    paper, datei = _vorgang()
    page.clock.install()
    gehalten = _halten(page, datei)
    goto(f"/insight/vorgaenge/{paper.pk}/")
    _ohne_zustandstexte(page)

    oeffnen = page.get_by_role("button", name=ANSEHEN)
    oeffnen.click()
    expect(page.get_by_text(LADEN)).to_be_visible()
    _warten(page, gehalten)

    # Ohne Ladeereignis zeigt die Ansicht nach DOC_TIMEOUT_MS (30 s, frontend/alpine/insight-shell.ts) den Fehlerfall
    page.clock.fast_forward(31_000)
    expect(page.get_by_text("Vorschau nicht verfügbar")).to_be_visible()
    expect(page.get_by_text("Das Dokument kann nicht eingebettet angezeigt werden.")).to_be_visible()
    link = page.get_by_role("link", name="Dokument öffnen")
    expect(link).to_be_visible()
    expect(link).to_have_attribute("href", f"/insight/dokumente/{datei.id}/preview/")
    expect(link).to_have_attribute("target", "_blank")
    expect(page.get_by_text(LADEN)).to_have_count(0)
    screenshot("dokumentansicht-fehler")

    # Erneut öffnen: wieder Ladezustand, die Fehlertexte sind weg
    page.keyboard.press("Escape")
    expect(page.get_by_text("Vorschau nicht verfügbar")).to_be_hidden()
    oeffnen.click()
    expect(page.get_by_text(LADEN)).to_be_visible()
    for teil in FEHLER:
        expect(page.get_by_text(teil, exact=True)).to_have_count(0)
    page.unroute_all(behavior="ignoreErrors")


def test_kommunenwechsel_zeigt_kommunen_ohne_daten(page: Any, goto: Any, screenshot: Any) -> None:
    paper, _ = _vorgang()
    importieren(
        io.StringIO(
            "schluessel;name;art;kreis;breite;laenge;plz;ortsteile\n"
            "033995401014;Moorbach;Gemeinde;Landkreis Heideland;52.6194;10.2453;29331;\n"
        )
    )
    goto(f"/insight/vorgaenge/{paper.pk}/")
    assert "noch nicht verfügbar" not in page.evaluate(SEITENTEXT)

    page.locator("[data-kommune-wechseln]:visible").first.click()
    dialog = page.get_by_role("dialog", name="Kommune wechseln")
    expect(dialog).to_be_visible()
    eingabe = page.locator("#kommune-dialog-eingabe")
    expect(eingabe).to_be_focused()
    eingabe.fill("Moorbach")
    zeile = dialog.locator("li", has_text="Moorbach")
    expect(zeile).to_have_count(1)
    expect(zeile.get_by_text("noch nicht verfügbar", exact=True)).to_be_visible()
    expect(zeile.locator("a")).to_have_count(0)
    screenshot("dokumentansicht-kommunenwechsel")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Insight nutzt die Breite (Issue #841): Auf breiten Bildschirmen endet der Inhalt nicht mehr bei rund 1.100 px.

Gemessen wird der rechte Rand des tatsächlichen Inhalts im Hauptbereich (Text, Bilder, Formularfelder, Tabellen):
Ab 1.440 px bleibt rechts höchstens ein Viertel der Fensterbreite frei. Am Handy und Tablet läuft nichts seitlich
über, und keine Tabelle hat eine Spalte, die nur „—“ zeigt.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from django.utils import timezone

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
)

RIS = "https://ris.breite.e2e/oparl"

#: Rechter Rand des Inhalts und Tabellenspalten, die nur Platzhalter zeigen
MESSUNG = """() => {
  const main = document.querySelector('main');
  let rechts = 0;
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  while (walker.nextNode()) {
    const knoten = walker.currentNode;
    if (!knoten.textContent.trim()) continue;
    const eltern = knoten.parentElement;
    if (!eltern || eltern.closest('[aria-hidden="true"], .sr-only, [hidden]')) continue;
    range.selectNodeContents(knoten);
    const r = range.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) rechts = Math.max(rechts, r.right);
  }
  main.querySelectorAll('img, input, select, textarea, button, table').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden') rechts = Math.max(rechts, r.right);
  });
  const leereSpalten = [...main.querySelectorAll('table')].flatMap((tabelle) => {
    const zeilen = [...tabelle.querySelectorAll('tbody tr')];
    if (zeilen.length < 2) return [];
    return [...zeilen[0].children].map((_, i) => i).filter((i) => zeilen.every((z) => {
      const zelle = z.children[i];
      return zelle && zelle.offsetParent !== null && /^[\\s—–-]*$/.test(zelle.innerText) && !zelle.querySelector('a, button, img, svg, i');
    }));
  });
  return {breite: window.innerWidth, rechts: Math.round(rechts), scroll: document.documentElement.scrollWidth, leereSpalten};
}"""


def _kommune() -> dict[str, Any]:
    source, _ = OParlSource.objects.get_or_create(url=f"{RIS}/system", defaults={"name": "E2E-RIS Breite"})
    body = OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Breitenstadt", slug="e2e-breite", is_listed=True
    )
    rat = OParlOrganization.objects.create(
        external_id=f"{RIS}/org/rat", body=body, name="Rat der Stadt Breitenstadt", classification="Rat"
    )
    fraktionen = [
        OParlOrganization.objects.create(
            external_id=f"{RIS}/org/f{n}", body=body, name=f"Fraktion {name}", organization_type="Fraktion"
        )
        for n, name in enumerate(("Mitte", "Zukunft", "Bürgerforum"))
    ]
    ausschuesse = [
        OParlOrganization.objects.create(
            external_id=f"{RIS}/org/a{n}", body=body, name=name, classification="Ausschuss"
        )
        for n, name in enumerate(("Hauptausschuss", "Ausschuss für Umwelt und Klimaschutz", "Jugendhilfeausschuss"))
    ]
    personen = []
    for n, (vorname, nachname) in enumerate(
        [
            ("Anna", "Albers"),
            ("Bernd", "Brink"),
            ("Clara", "Claßen"),
            ("Dieter", "Dahl"),
            ("Elif", "Erden"),
            ("Frank", "Feld"),
            ("Gisela", "Grote"),
            ("Hakan", "Heller"),
        ]
    ):
        person = OParlPerson.objects.create(
            external_id=f"{RIS}/person/{n}",
            body=body,
            name=f"{vorname} {nachname}",
            given_name=vorname,
            family_name=nachname,
        )
        personen.append(person)
        rollen = [
            (rat, "Oberbürgermeisterin" if n == 0 else "Ratsmitglied"),
            (fraktionen[n % 3], "Mitglied"),
            (ausschuesse[n % 3], "Mitglied"),
        ]
        for k, (org, rolle) in enumerate(rollen):
            OParlMembership.objects.create(
                external_id=f"{RIS}/membership/{n}-{k}",
                person=person,
                organization=org,
                role=rolle,
                start_date=date(2024, 7, 1),
            )
    jetzt = timezone.now()
    papiere = []
    for n in range(8):
        papier = OParlPaper.objects.create(
            external_id=f"{RIS}/paper/{n}",
            body=body,
            name=f"Sanierung des Stadtparks, Bauabschnitt {n + 1}",
            reference=f"V/2026/{n:03d}",
            paper_type="Beschlussvorlage",
            date=date(2026, 9, 1) - timedelta(days=n),
        )
        papiere.append(papier)
    sitzungen = []
    for n, tage in enumerate((-20, -6, 4, 11)):
        sitzung = OParlMeeting.objects.create(
            external_id=f"{RIS}/meeting/{n}",
            body=body,
            name="Sitzung",
            start=jetzt + timedelta(days=tage),
            location_name="Rathaus, Ratssaal",
        )
        sitzung.organizations.add(rat if n % 2 else ausschuesse[0])
        sitzungen.append(sitzung)
        for k in range(2):
            papier = papiere[(n * 2 + k) % len(papiere)]
            top = OParlAgendaItem.objects.create(
                external_id=f"{RIS}/item/{n}-{k}",
                meeting=sitzung,
                number=str(k + 1),
                order=k,
                name=papier.name,
                result="beschlossen" if tage < 0 else "",
            )
            OParlConsultation.objects.create(
                external_id=f"{RIS}/consultation/{n}-{k}",
                body=body,
                paper=papier,
                paper_external_id=papier.external_id,
                meeting_external_id=sitzung.external_id,
                agenda_item_external_id=top.external_id,
                role="Entscheidung",
            )
    return {"body": body, "rat": rat, "person": personen[0], "papier": papiere[0], "sitzung": sitzungen[2]}


def _seiten(welt: dict[str, Any]) -> list[str]:
    return [
        "/insight/",
        "/insight/termine/",
        "/insight/vorgaenge/",
        "/insight/gremien/",
        "/insight/personen/",
        f"/insight/gremien/{welt['rat'].pk}/",
        f"/insight/personen/{welt['person'].pk}/",
        f"/insight/termine/{welt['sitzung'].pk}/",
        f"/insight/vorgaenge/{welt['papier'].pk}/",
    ]


def _messen(page: Any, goto: Any, breite: int, seiten: list[str]) -> dict[str, dict[str, Any]]:
    page.set_viewport_size({"width": breite, "height": 900})
    page.route("**/insight/tiles/**", lambda route: route.fulfill(status=204, body=""))
    ergebnisse = {}
    for pfad in seiten:
        goto(pfad)
        ergebnisse[pfad] = page.evaluate(MESSUNG)
    return ergebnisse


@pytest.mark.parametrize("breite", [1440, 1920, 2560])
def test_inhalt_nutzt_die_breite(page: Any, goto: Any, breite: int) -> None:
    welt = _kommune()
    goto(f"/insight/kommune/{welt['body'].pk}/")
    ergebnisse = _messen(page, goto, breite, _seiten(welt))
    zu_schmal = {
        pfad: f"{(m['breite'] - m['rechts']) / m['breite']:.0%} frei"
        for pfad, m in ergebnisse.items()
        if (m["breite"] - m["rechts"]) / m["breite"] >= 0.25
    }
    assert not zu_schmal, f"Rechts bleibt mehr als ein Viertel leer bei {breite} px: {zu_schmal}"
    leer = {pfad: m["leereSpalten"] for pfad, m in ergebnisse.items() if m["leereSpalten"]}
    assert not leer, f"Spalten nur mit „—“: {leer}"


@pytest.mark.parametrize("breite", [360, 768, 1024])
def test_handy_und_tablet_ohne_seitlichen_ueberlauf(page: Any, goto: Any, breite: int) -> None:
    welt = _kommune()
    goto(f"/insight/kommune/{welt['body'].pk}/")
    ergebnisse = _messen(page, goto, breite, _seiten(welt))
    ueberlauf = {pfad: m["scroll"] for pfad, m in ergebnisse.items() if m["scroll"] > breite + 1}
    assert not ueberlauf, f"Seitlicher Überlauf bei {breite} px: {ueberlauf}"

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Insight nutzt die Breite (Issue #841): Auf breiten Bildschirmen endet der Inhalt nicht mehr bei rund 1.100 px.

Gemessen wird der rechte Rand des tatsächlichen Inhalts im Hauptbereich (Text, Bilder, Formularfelder, Tabellen):
Ab 1.440 px bleibt rechts höchstens ein Viertel der Fensterbreite frei. Am Handy und Tablet läuft nichts seitlich
über, und keine Tabelle hat eine Spalte, die nur „—“ zeigt.

Nachmessung nach #848: Die Suche füllt ab 1.366 px die Breite (Trefferspalte wächst, Filterspalte mit offenen
Listen bleibt beim Scrollen sichtbar), und auf sehr breiten Bildschirmen steht der Rahmen mittig neben der Seitenleiste.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pytest
from django.utils import timezone

from insight_core.models import (
    Municipality,
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
from insight_core.services.kommunenverzeichnis import LAENDER

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
  const flaeche = main.getBoundingClientRect();
  const rahmen = [...document.querySelectorAll('.max-w-insight')].map((el) => el.getBoundingClientRect())
    .filter((r) => r.width > 0).map((r) => ({l: r.left - flaeche.left, r: flaeche.right - r.right}));
  return {breite: window.innerWidth, rechts: Math.round(rechts), scroll: document.documentElement.scrollWidth, leereSpalten, rahmen};
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


@pytest.mark.parametrize("breite", [1366, 1440, 1680, 1913, 2560])
def test_inhalt_nutzt_die_breite(page: Any, goto: Any, breite: int) -> None:
    """Alle Seitentypen: Inhalt reicht in die Breite, und der Rahmen steht überall gleich (mittig ab 116rem)."""
    welt = _kommune()
    # Verzeichnis wie nach dem Import: Die Kommunenwahl zeigt die Länder als Raster statt des Hinweises „nicht geladen“
    for n, land in enumerate(LAENDER):
        Municipality.objects.create(
            key=f"{land}{n:010d}", name=f"Gemeinde {n}", district_key=f"{land}001", state_key=land
        )
    goto(f"/insight/kommune/{welt['body'].pk}/")
    ergebnisse = _messen(page, goto, breite, [*_seiten(welt), "/insight/kommunen/", "/insight/karte/"])
    zu_schmal = {
        pfad: f"{(m['breite'] - m['rechts']) / m['breite']:.0%} frei"
        for pfad, m in ergebnisse.items()
        if (m["breite"] - m["rechts"]) / m["breite"] >= 0.25
    }
    assert not zu_schmal, f"Rechts bleibt mehr als ein Viertel leer bei {breite} px: {zu_schmal}"
    leer = {pfad: m["leereSpalten"] for pfad, m in ergebnisse.items() if m["leereSpalten"]}
    assert not leer, f"Spalten nur mit „—“: {leer}"
    # Kopfzeile, Hinweise, Bänder, Inhalt und Fuß: links und rechts gleich weit vom Rand, auf jeder Seite gleich weit
    schief = {pfad: m["rahmen"] for pfad, m in ergebnisse.items() if any(abs(r["l"] - r["r"]) > 1 for r in m["rahmen"])}
    assert not schief, f"Rahmen nicht mittig bei {breite} px: {schief}"
    abstaende = {round(r["l"]) for m in ergebnisse.values() for r in m["rahmen"]}
    assert len(abstaende) == 1, f"Rahmen verschieden weit eingerückt bei {breite} px: {abstaende}"


@pytest.mark.parametrize("breite", [360, 768, 1024])
def test_handy_und_tablet_ohne_seitlichen_ueberlauf(page: Any, goto: Any, breite: int) -> None:
    welt = _kommune()
    goto(f"/insight/kommune/{welt['body'].pk}/")
    ergebnisse = _messen(page, goto, breite, _seiten(welt))
    ueberlauf = {pfad: m["scroll"] for pfad, m in ergebnisse.items() if m["scroll"] > breite + 1}
    assert not ueberlauf, f"Seitlicher Überlauf bei {breite} px: {ueberlauf}"


@pytest.mark.parametrize("breite", [1280, 1366, 1536])
def test_lange_adresse_ohne_seitlichen_ueberlauf(page: Any, goto: Any, breite: int) -> None:
    """Eine lange E-Mail-Adresse in der Personenliste lässt die Seite nicht seitlich scrollen (Prüfung #848)."""
    welt = _kommune()
    person = OParlPerson.objects.get(external_id=f"{RIS}/person/1")
    person.email = "vorname.nachname-doppelname.sehr-lange-adresse@fraktion-buergerforum.breitenstadt.example"
    person.save()
    goto(f"/insight/kommune/{welt['body'].pk}/")
    ergebnisse = _messen(page, goto, breite, ["/insight/personen/"])
    assert ergebnisse["/insight/personen/"]["scroll"] <= breite + 1, ergebnisse


def test_gremium_randspalte_erst_ab_1440(page: Any, goto: Any) -> None:
    """Bis 1.440 px steht die Randspalte unter den Mitgliedern, die Tabelle behält die volle Breite (Prüfung #848)."""
    welt = _kommune()
    goto(f"/insight/kommune/{welt['body'].pk}/")
    lagen = {}
    for breite in (1280, 1440):
        page.set_viewport_size({"width": breite, "height": 900})
        goto(f"/insight/gremien/{welt['rat'].pk}/")
        lagen[breite] = page.evaluate(
            """() => {
              const tabelle = document.querySelector('main table').getBoundingClientRect();
              const rand = document.querySelector('main aside').getBoundingClientRect();
              return {tabelleUnten: tabelle.bottom, randOben: rand.top, randLinks: rand.left, tabelleRechts: tabelle.right};
            }"""
        )
    assert lagen[1280]["randOben"] >= lagen[1280]["tabelleUnten"], lagen
    assert lagen[1440]["randLinks"] > lagen[1440]["tabelleRechts"], lagen


# =============================================================================
# Nachmessung nach #848: Suche ab 2xl und sehr breite Bildschirme
# =============================================================================

#: Hauptfläche, rechter Rand des Inhalts, Rahmen (max-w-insight) und die Spalten der Suche
MESSUNG_RAHMEN = """() => {
  const main = document.querySelector('main').getBoundingClientRect();
  let rechts = 0;
  const walker = document.createTreeWalker(document.querySelector('main'), NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  while (walker.nextNode()) {
    const eltern = walker.currentNode.parentElement;
    if (!walker.currentNode.textContent.trim() || !eltern || eltern.closest('[aria-hidden="true"], .sr-only, [hidden]')) continue;
    range.selectNodeContents(walker.currentNode);
    const r = range.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) rechts = Math.max(rechts, r.right);
  }
  document.querySelectorAll('main aside, main table, main input, main select, main button').forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.right > 0) rechts = Math.max(rechts, r.right);
  });
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return {l: r.left, r: r.right, t: r.top, h: r.height}; };
  const rahmen = [...document.querySelectorAll('.max-w-insight')].map(box).filter((r) => r.r - r.l > 0);
  const ausschnitte = [...document.querySelectorAll('#treffer-liste p.leading-relaxed')].map((p) => p.getBoundingClientRect().width);
  return {
    mainL: main.left, mainR: main.right, rechts,
    rahmen, liste: box(document.querySelector('#treffer-liste')),
    filter: box(document.querySelector('#suche-sortierung')?.closest('[class*="2xl:sticky"]')),
    ausschnitt: Math.max(0, ...ausschnitte),
  };
}"""


class _Suchdienst:
    """Suchdienst ohne Elasticsearch: jeder Vorgang der Kommune trifft mit einem Ausschnitt aus einer Anlage."""

    def __init__(self, papiere: list[OParlPaper]) -> None:
        self.papiere = papiere

    def facet_counts(self, query: str, **_: Any) -> dict[str, Any]:
        return {
            "paper_types": {"Beschlussvorlage": 6, "Antrag": 3, "Mitteilungsvorlage": 2, "Anfrage": 1},
            "periods": {"12m": 7, "2y": 9, "5y": 11, "older": 1},
        }

    def search_grouped(self, query: str, **_: Any) -> dict[str, Any]:
        from insight_core.services.search_service import HIGHLIGHT_POST, HIGHLIGHT_PRE

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
                    "date": str(p.date),
                },
                "file": {
                    "id": f"00000000-0000-4000-8000-{n:012d}",
                    "name": "Anlage 2 – Begründung",
                    "text_content": "roh",
                    "_formatted": {
                        "text_content": f"Die Verwaltung schlägt vor, den {HIGHLIGHT_PRE}Stadtpark{HIGHLIGHT_POST} neu zu "
                        "gestalten: Wege werden barrierefrei, die Beleuchtung wird erneuert und am Teich entsteht "
                        "eine Fläche für Veranstaltungen mit Sitzstufen und schattigen Bäumen."
                    },
                },
                "others": [],
            }
            for n, p in enumerate(self.papiere)
        ]
        return {
            "groups": gruppen,
            "counts": {"vorgaenge": len(gruppen), "unterlagen": 0},
            "totals_by_index": {"meetings": 2, "files": 5, "persons": 1, "organizations": 1},
            "similar_spelling": False,
            "has_more": False,
        }


def _mit_suche(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from insight_core.services import search_service

    welt = _kommune()
    papiere = list(OParlPaper.objects.filter(body=welt["body"]).order_by("reference"))
    monkeypatch.setattr(search_service, "get_search_service", lambda: _Suchdienst(papiere))
    return welt


@pytest.mark.parametrize("breite", [1366, 1440, 1536, 1680, 1913, 2560])
def test_suche_und_detailseite_fuellen_die_breite(
    page: Any, goto: Any, monkeypatch: pytest.MonkeyPatch, breite: int
) -> None:
    """Rechter Rand der Inhalte bei mindestens 85 % der Hauptfläche; Rahmen mittig; Filterspalte trägt Inhalt."""
    welt = _mit_suche(monkeypatch)
    goto(f"/insight/kommune/{welt['body'].pk}/")
    page.set_viewport_size({"width": breite, "height": 1000})
    seiten = ["/insight/suche/?q=Stadtpark"]
    if breite >= 1913:
        seiten.append(f"/insight/vorgaenge/{welt['papier'].pk}/")
    for pfad in seiten:
        goto(pfad)
        m = page.evaluate(MESSUNG_RAHMEN)
        anteil = (m["rechts"] - m["mainL"]) / (m["mainR"] - m["mainL"])
        assert anteil >= 0.85, f"{pfad} bei {breite} px: Inhalt endet bei {anteil:.0%} der Hauptfläche"
        # Kopfzeile, Bänder, Inhalt und Fuß: derselbe Rahmen, links und rechts gleich weit vom Rand der Hauptfläche
        for r in m["rahmen"]:
            assert abs((r["l"] - m["mainL"]) - (m["mainR"] - r["r"])) <= 1, (pfad, breite, r)
        assert len({round(r["l"]) for r in m["rahmen"]}) == 1, (pfad, breite, m["rahmen"])
        if m["liste"]:
            assert m["ausschnitt"] <= 800, "Ausschnitte behalten ihre Lesebreite"
        if m["liste"] and breite >= 1536:
            assert m["filter"]["h"] >= 300, "Filterspalte mit offenen Listen statt drei Knöpfen über leerer Fläche"
            assert m["filter"]["l"] - m["liste"]["r"] <= 64, "Trefferspalte reicht bis an die Filterspalte"


def test_filterspalte_bleibt_sichtbar_und_filtert(page: Any, goto: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    welt = _mit_suche(monkeypatch)
    goto(f"/insight/kommune/{welt['body'].pk}/")
    page.set_viewport_size({"width": 1913, "height": 1000})
    goto("/insight/suche/?q=Stadtpark")
    page.mouse.wheel(0, 1200)
    page.wait_for_function("() => window.scrollY > 600")
    oben = page.evaluate(MESSUNG_RAHMEN)["filter"]["t"]
    assert 64 <= oben <= 100, f"Filterspalte nach dem Scrollen bei {oben} px statt unter der Kopfzeile"

    page.get_by_role("group", name="Zeitraum").get_by_role("link", name="Letzte 2 Jahre").click()
    page.wait_for_url("**period=2y**")
    gewaehlt = page.get_by_role("group", name="Zeitraum").locator('[aria-current="true"]')
    assert "Letzte 2 Jahre" in gewaehlt.inner_text()
    # Die Felder der Ausklapplisten tragen den Stand weiter, wenn das Suchfeld abgeschickt wird
    assert page.evaluate("() => document.querySelector('input[name=period]:checked').value") == "2y"

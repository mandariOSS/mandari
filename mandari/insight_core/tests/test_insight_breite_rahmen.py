# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Insight auf breiten Bildschirmen, Nachmessung nach #848 (Issue #841).

- Sehr breite Bildschirme: Kopfzeile, Hinweise, Bänder, Inhaltsrahmen und Fuß stehen mittig in der Fläche neben der
  Seitenleiste (``max-w-insight mx-auto``) statt links mit einseitiger Leerfläche rechts.
- Suche ab 2xl: Die Trefferspalte wächst mit der Breite, die Filterspalte zeigt Zeitraum und Art als offene Listen mit
  Zählern und bleibt beim Scrollen sichtbar. Die Formularfelder der Ausklapplisten tragen weiter den Stand. Was in der
  klebenden Spalte filtert oder sortiert, springt danach an den Anfang der Suche (Anker nur ab 2xl, darunter wie
  bisher); jede Option hat eine feste, eindeutige id (Tastaturfokus).
- Treffer ordnen ihre Kontextzeile ab 54rem Listenbreite als Spalte rechts an (Container-Abfrage, nicht Fensterbreite).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from django.http import QueryDict
from django.template import engines
from django.test import Client
from django_cotton.compiler_regex import CottonCompiler

from insight_core.models import OParlBody, OParlPaper, OParlSource
from insight_core.services.search_page import SearchParams, build_context

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
RIS = "https://ris.rahmen.example/oparl"
BREIT = "[@container(min-width:54rem)]:"
NACH_OBEN = 'hx-swap="innerHTML show:#suche-anfang:top"'


class _Dienst:
    """Suchdienst mit festen Zählern und einem Treffer zu einem Vorgang."""

    def __init__(self, paper: OParlPaper | None = None) -> None:
        self.paper = paper

    def facet_counts(self, query: str, **_: Any) -> dict[str, Any]:
        return {"paper_types": {"Vorlagen": 7, "Antrag": 2}, "periods": {"12m": 3, "2y": 5, "5y": 8, "older": 2}}

    def search_grouped(self, query: str, **_: Any) -> dict[str, Any]:
        gruppen = []
        if self.paper is not None:
            gruppen.append(
                {
                    "kind": "paper",
                    "key": str(self.paper.pk),
                    "paper": {
                        "id": str(self.paper.pk),
                        "name": self.paper.name,
                        "reference": self.paper.reference,
                        "paper_type": "Vorlagen",
                        "organization_names": ["Ausschuss für Umwelt"],
                        "date": "2026-09-01",
                    },
                    "others": [],
                }
            )
        return {
            "groups": gruppen,
            "counts": {"vorgaenge": len(gruppen), "unterlagen": 0},
            "totals_by_index": {"meetings": 0, "files": 0, "persons": 0, "organizations": 0},
            "similar_spelling": False,
            "has_more": False,
        }


@pytest.fixture
def body(db: Any) -> OParlBody:
    source = OParlSource.objects.create(name="Rahmen-RIS", url=f"{RIS}/system")
    return OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Stadt Rahmen", slug="rahmen")


@pytest.fixture
def paper(body: OParlBody) -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=f"{RIS}/paper/1", body=body, name="Sanierung des Stadtparks", reference="V/2026/001"
    )


def _render(quelle: str, **kontext: Any) -> str:
    return engines["django"].from_string(CottonCompiler().process(quelle)).render(kontext)


# --- Rahmen mittig ----------------------------------------------------------------------------------------


def test_jeder_rahmen_mit_obergrenze_steht_mittig() -> None:
    """Jede Klassenliste mit ``max-w-insight`` zentriert auch (eine Regel für alle Seiten)."""
    funde: dict[str, str] = {}
    for datei in TEMPLATES.rglob("*.html"):
        for klassen in re.findall(r'class="([^"]*\bmax-w-insight\b[^"]*)"', datei.read_text(encoding="utf-8")):
            funde[f"{datei.relative_to(TEMPLATES)}: {klassen[:60]}"] = klassen
    ohne = [stelle for stelle, klassen in funde.items() if "mx-auto" not in klassen.split()]
    assert not ohne, ohne
    assert len(funde) >= 8, funde  # Kopfzeile, Hinweise, Inhalt, Band, Kopfband, Übersicht, Ortsband, Fuß


def test_kopfzeile_und_hinweise_am_selben_rahmen() -> None:
    """Brotkrumen und Suchfeld der Kopfzeile fluchten mit dem Inhalt, auch wenn er mittig steht."""
    layout = (TEMPLATES / "base_insight.html").read_text(encoding="utf-8")
    assert '<div class="max-w-insight mx-auto h-16 px-4 sm:px-8 ' in layout
    hinweise = (TEMPLATES / "partials/insight_hinweise.html").read_text(encoding="utf-8")
    assert '<div class="max-w-insight mx-auto px-4 sm:px-8 pt-5' in hinweise


# --- Filterspalte der Suche -------------------------------------------------------------------------------


def test_optionen_tragen_ihre_adresse(db: Any) -> None:
    params = SearchParams.from_get(QueryDict("q=Kita&period=2y&paper_type=Antrag&page=3"))

    kontext = build_context(_Dienst(), params, None, None, date(2026, 10, 5))

    zeitraum = {o["value"]: o["url"] for o in kontext["period_options"]}
    assert zeitraum[""] == "?q=Kita&paper_type=Antrag"
    assert zeitraum["12m"] == "?q=Kita&period=12m&paper_type=Antrag"
    art = {o["value"]: (o["checked"], o["url"]) for o in kontext["art_options"]}
    # gewählte Art wird abgewählt, andere kommt hinzu; Seite beginnt wieder bei 1
    assert art["Antrag"] == (True, "?q=Kita&period=2y")
    assert art["Vorlage"] == (False, "?q=Kita&period=2y&paper_type=Antrag&paper_type=Vorlage")
    assert [o["kennung"] for o in kontext["art_options"]] == ["vorlage", "antrag"]


class _DienstMitAehnlichenArten(_Dienst):
    def facet_counts(self, query: str, **_: Any) -> dict[str, Any]:
        arten = {"Ergänzung": 4, "Erganzung": 3, "Ergänzung-2": 2, "§§": 1, "": 1}
        return {"paper_types": arten, "periods": {}}


def test_arten_mit_gleicher_kurzform_bekommen_eindeutige_kennungen(db: Any) -> None:
    """Die id der Option hängt am Wert (Fokus nach dem Austausch), darf aber nie doppelt vorkommen."""
    params = SearchParams.from_get(QueryDict("q=Kita"))

    kontext = build_context(_DienstMitAehnlichenArten(), params, None, None, date(2026, 10, 5))

    kennungen = [o["kennung"] for o in kontext["art_options"]]
    assert kennungen == ["erganzung", "erganzung-2", "erganzung-2-2", "art", "art-2"]


def test_filterliste_als_links_mit_gewaehlter_option() -> None:
    html = _render(
        '<c-suche.facette-liste titel="Art" name="paper_type" :optionen="o" mehrfach />',
        o=[
            {"value": "Antrag", "count": "2", "checked": True, "url": "?q=x"},
            {"value": "Vorlage", "count": "7", "checked": False, "url": "?q=x&paper_type=Antrag&paper_type=Vorlage"},
        ],
    )
    assert 'role="group" aria-labelledby="filterliste-paper_type"' in html
    assert 'id="filterliste-paper_type"' in html
    assert html.count('aria-current="true"') == 1 and "(gewählt)" in html
    assert html.count('hx-target="#suchergebnis"') == 2 and 'hx-push-url="true"' in html
    # Die Spalte klebt: nach dem Austausch an den Seitenanfang statt ans Ende der neuen Liste (Prüfung #867)
    assert html.count(NACH_OBEN) == 2
    # Feste id je Option: HTMX setzt den Tastaturfokus nach dem Austausch darauf zurück (WCAG 2.4.3)
    assert 'id="filter-paper_type-antrag"' in html and 'id="filter-paper_type-vorlage"' in html
    assert 'href="?q=x&amp;paper_type=Antrag&amp;paper_type=Vorlage"' in html
    # keine Formularfelder: den Stand tragen die Felder der Ausklapplisten
    assert "<input" not in html and 'data-lucide="check"' in html


def test_suchseite_mit_wachsender_trefferspalte_und_fester_filterspalte(
    body: OParlBody, paper: OParlPaper, monkeypatch: pytest.MonkeyPatch
) -> None:
    from insight_core.services import search_service

    monkeypatch.setattr(search_service, "get_search_service", lambda: _Dienst(paper))
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")

    html = client.get("/insight/suche/?q=Stadtpark&period=2y").content.decode()

    assert "2xl:grid-cols-[minmax(0,1fr)_16rem]" in html
    assert "2xl:sticky 2xl:top-20 2xl:max-h-[calc(100vh-6rem)] 2xl:overflow-y-auto" in html
    assert 'id="treffer-liste" class="mt-2 [container-type:inline-size]"' in html
    # Ausklapplisten bis 2xl, offene Listen ab 2xl
    assert '<div class="contents 2xl:hidden">' in html and '<div class="hidden 2xl:block' in html
    zeitraum = html.split('aria-labelledby="filterliste-period"', 1)[1].split("</ul>", 1)[0]
    assert zeitraum.count('aria-current="true"') == 1
    assert re.search(r'aria-current="true"[^>]*>.*?Letzte 2 Jahre', zeitraum, re.S)
    # Die Radiofelder (ab 2xl unsichtbar) tragen den gewählten Zeitraum weiter ins Suchformular
    assert re.search(r'name="period" value="2y" form="suche-form" checked', html)
    kennungen = re.findall(r'\sid="([^"]+)"', html)
    assert len(kennungen) == len(set(kennungen))
    assert {"filter-period-alle", "filter-period-2y", "filter-paper_type-vorlage", "filter-paper_type-antrag"} <= set(
        kennungen
    )
    # Sortierung und „entfernen“ stehen ebenfalls in der klebenden Spalte
    sortierung = html.split('id="suche-sortierung"', 1)[1].split(">", 1)[0]
    assert NACH_OBEN in sortierung
    entfernen = html.split("Zeitraum: ", 1)[1].split("entfernen", 1)[0]
    assert NACH_OBEN in entfernen
    # Sprungziel vor dem Kopfband, nur ab 2xl mit Box: Handy und Tablet springen nicht (Ausklapplisten wie bisher)
    anker = '<div id="suche-anfang" class="hidden 2xl:block scroll-mt-20" aria-hidden="true"></div>'
    assert anker in html and html.index(anker) < html.index('id="suche-form"')
    ausklappliste = html.split('<div class="contents 2xl:hidden">', 1)[1].split('id="suche-sortierung"', 1)[0]
    assert "hx-swap=" not in ausklappliste


# --- Treffer mit Kontextspalte ----------------------------------------------------------------------------


def test_treffer_kontext_als_spalte_ab_54rem_listenbreite() -> None:
    treffer = {
        "url": "/insight/vorgaenge/1/",
        "title": "Sanierung des Stadtparks",
        "context": ["Vorlage", "V/2026/001", "Ausschuss für Umwelt", "15.09.2026"],
        "status": "Am 15.09.2026 beschlossen.",
        "status_kind": "decided",
        "snippet": "… Stadtpark …",
    }
    html = _render('<ol><c-suche.treffer-vorgang :treffer="t" /></ol>', t=treffer)

    assert f"{BREIT}grid {BREIT}grid-cols-[minmax(0,1fr)_14rem]" in html
    # Kontext bleibt im Quelltext vor dem Titel (Vorlesereihenfolge wie am Handy)
    assert html.index("V/2026/001") < html.index("<h3")
    assert html.count(f'<span class="{BREIT}hidden"> · </span>') == 3
    assert f'<span class="{BREIT}block font-medium' in html
    person = _render(
        '<ol><c-suche.treffer-person :treffer="t" /></ol>',
        t={"url": "/insight/personen/1/", "title": "Anna Albers", "context": ["Person"]},
    )
    assert f"{BREIT}col-start-2" in person and f"{BREIT}col-start-1" in person


def test_neue_klassen_stehen_im_gebauten_css() -> None:
    """styles.css ist eingecheckt: Container-Abfrage, Spalte und Rahmen müssen im Build gelandet sein."""
    css = (ROOT / "static/css/styles.css").read_text(encoding="utf-8")
    for teil in (
        "container-type:inline-size",
        "@container(min-width:54rem)",
        r".\32xl\:sticky{position:sticky}",
        r".\32xl\:grid-cols-\[minmax\(0\2c 1fr\)_16rem\]",
        r".\[\@container\(min-width\:54rem\)\]\:grid-cols-\[minmax\(0\2c 1fr\)_14rem\]",
    ):
        assert teil in css, teil

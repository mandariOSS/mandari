# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suchseite mit Ortsband und Filterleiste (Konzept Insight-Suche, P0.8/P0.9)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from django.http import QueryDict
from django.test import Client

from insight_core.models import OParlBody, OParlPaper, PaperLocation, Street
from insight_core.services import search_places
from insight_core.services.paper_locations import nearby_papers
from insight_core.services.search_page import SearchParams, papers_of_types
from insight_core.tests.conftest import CENTER_LAT, CENTER_LON

M_LAT = 1 / 111_320  # ein Meter in Grad Breite

# --- Parameter ------------------------------------------------------------------------------------------


def test_parameter_wie_searchquery_und_alte_links() -> None:
    params = SearchParams.from_get(
        QueryDict("q=Kita&type=paper&period=2y&paper_type=Antrag&paper_type=Vorlage&sort=newest&page=3")
    )

    assert (params.result_type, params.period, params.paper_types, params.sort, params.page) == (
        "papers",
        "2y",
        ["Antrag", "Vorlage"],
        "newest",
        3,
    )
    assert params.date_range(date(2026, 10, 4)) == ("2024-10-04", None)
    # Seite beginnt beim Ändern wieder bei 1, leere Werte entfallen
    assert params.url(sort="relevance") == "?q=Kita&result_type=papers&period=2y&paper_type=Antrag&paper_type=Vorlage"

    unsinn = SearchParams.from_get(QueryDict("q=x&result_type=geheim&period=99y&sort=zufall&page=abc"))
    assert (unsinn.result_type, unsinn.period, unsinn.sort, unsinn.page) == ("", "", "relevance", 1)
    assert SearchParams.from_get(QueryDict("q=x&period=older")).date_range(date(2026, 10, 4)) == (None, "2021-10-04")


# --- Ort erkennen ----------------------------------------------------------------------------------------


@pytest.fixture
def strassen(geo_body: OParlBody, make_street: Callable[..., Street]) -> OParlBody:
    make_street(geo_body, "Von-Witzleben-Straße", CENTER_LAT, CENTER_LON)
    make_street(geo_body, "Hauptstraße", CENTER_LAT + 0.01, CENTER_LON)
    make_street(geo_body, "Hauptstraße", CENTER_LAT + 0.01, CENTER_LON + 0.001)  # zweiter Abschnitt, gleicher Name
    make_street(geo_body, "Alte Hauptstraße", CENTER_LAT + 0.02, CENTER_LON)
    make_street(geo_body, "Schulenburgstraße", CENTER_LAT + 0.03, CENTER_LON)
    make_street(geo_body, "Am Spielplatz", CENTER_LAT + 0.04, CENTER_LON)
    return geo_body


@pytest.mark.django_db
@pytest.mark.parametrize(
    "eingabe",
    [
        "von-witzleben",
        "witzleben",
        "Von-Witzleben-Straße",
        "Witzlebenstraße",
        "Witzleben Str.",
        "Radweg Von-Witzleben-Straße 12",
    ],
)
def test_eindeutige_strasse_wird_erkannt(strassen: OParlBody, eingabe: str) -> None:
    ort = search_places.detect_place(strassen, eingabe)

    assert ort is not None and ort.name == "Von-Witzleben-Straße"


@pytest.mark.django_db
@pytest.mark.parametrize("eingabe", ["Hauptstraße", "Schule", "Spielplatz", "Kita", "Haushalt 2026", "von"])
def test_mehrdeutig_oder_allgemein_bleibt_ohne_ort(strassen: OParlBody, eingabe: str) -> None:
    assert search_places.detect_place(strassen, eingabe) is None


@pytest.mark.django_db
def test_schalter_aus(strassen: OParlBody, settings: Any) -> None:
    settings.INSIGHT_SEARCH_PLACES = False
    assert search_places.detect_place(strassen, "witzleben") is None


def _vorgang(body: OParlBody, name: str, tag: date, orte: list[tuple[float, float]]) -> OParlPaper:
    paper = OParlPaper.objects.create(
        external_id=f"https://ris.beispielstadt.example/oparl/papers/{uuid.uuid4()}",
        body=body,
        name=name,
        reference=f"V/{tag.year}",
        paper_type="Vorlagen",
        date=tag,
    )
    for lat, lon in orte:
        PaperLocation.objects.create(paper=paper, body=body, name=name, latitude=lat, longitude=lon)
    return paper


@pytest.mark.django_db
def test_neueste_in_der_naehe_vor_der_kappung(geo_body: OParlBody) -> None:
    """Nach Entfernung gekappt fielen gerade die neuen Vorgänge heraus (452 Vorgänge in 500 m)."""
    for n in range(5):
        _vorgang(geo_body, f"alt-{n}", date(2012, 1, 1 + n), [(CENTER_LAT + n * M_LAT, CENTER_LON)])
    _vorgang(geo_body, "neu", date(2026, 8, 13), [(CENTER_LAT + 400 * M_LAT, CENTER_LON)])

    assert nearby_papers(geo_body, CENTER_LAT, CENTER_LON, 500, limit=3)[-1]["name"] != "neu"
    nach_datum = nearby_papers(geo_body, CENTER_LAT, CENTER_LON, 500, limit=3, order="date")
    assert [r["name"] for r in nach_datum] == ["neu", "alt-4", "alt-3"]


@pytest.mark.django_db
def test_ortsband_fasst_zusammen_und_stellt_sammelvorlagen_zurueck(geo_body: OParlBody) -> None:
    for bezirk in range(12):  # dieselbe Programmvorlage je Bezirksvertretung
        _vorgang(geo_body, f"Programm {bezirk}".replace(str(bezirk), ""), date(2026, 3, 25), [(CENTER_LAT, CENTER_LON)])
    weit = [(CENTER_LAT + 0.05 + i * 0.001, CENTER_LON) for i in range(25)]
    _vorgang(
        geo_body, "Kindertagesbetreuungsbericht", date(2026, 9, 1), [(CENTER_LAT + 150 * M_LAT, CENTER_LON), *weit]
    )
    _vorgang(geo_body, "Fahrradverkehr im Westen", date(2026, 8, 13), [(CENTER_LAT + 409 * M_LAT, CENTER_LON)])
    ort = search_places.Place("Von-Witzleben-Straße", CENTER_LAT, CENTER_LON)

    band = search_places.place_band(geo_body, ort)

    assert band is not None
    assert [(r["title"], r["count"], r["nennt"]) for r in band["rows"]] == [
        ("Fahrradverkehr im Westen", 1, False),
        ("Programm ", 12, True),
    ]
    assert band["sammel"] == 1 and band["sammel_beispiel"] == "Kindertagesbetreuungsbericht"
    assert band["total"] == 14
    assert "lat=51.96066" in band["map_url"] and "type=location" in band["abo_url"]


@pytest.mark.django_db
def test_kein_ortsband_bei_geringer_verortung(geo_body: OParlBody) -> None:
    _vorgang(geo_body, "verortet", date(2026, 1, 1), [(CENTER_LAT, CENTER_LON)])
    for n in range(3):
        _vorgang(geo_body, f"ohne Ort {n}", date(2026, 1, 1), [])
    ort = search_places.Place("Von-Witzleben-Straße", CENTER_LAT, CENTER_LON)

    assert search_places.place_band(geo_body, ort) is None  # 25 % verortet (Bonn: 3 %)


@pytest.mark.django_db
def test_art_filter_fuer_dateien_ueber_den_vorgang(geo_body: OParlBody) -> None:
    vorlage = _vorgang(geo_body, "Vorlage", date(2026, 1, 1), [])
    antrag = OParlPaper.objects.create(
        external_id="https://ris.beispielstadt.example/oparl/papers/antrag",
        body=geo_body,
        paper_type="Antrag an die BV Mitte",
    )

    erlaubt = papers_of_types(["Antrag"], geo_body.slug or "")({str(vorlage.id), str(antrag.id), "kaputt"})

    assert erlaubt == {str(antrag.id)}


# --- Seite --------------------------------------------------------------------------------------------


class _Dienst:
    """Nachbau des Suchdienstes für die Seite: merkt sich die Aufrufe."""

    def __init__(self, treffer: bool = True) -> None:
        self.aufrufe: list[dict[str, Any]] = []
        self.treffer = treffer

    def facet_counts(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "paper_types": {"Vorlagen": 7, "Antrag an den Rat": 2, "Antrag an die BV Mitte": 1},
            "periods": {"12m": 3, "2y": 5, "5y": 8, "older": 2},
        }

    def search_grouped(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.aufrufe.append(kwargs)
        gruppen = []
        if self.treffer and kwargs.get("page_size", 20) > 1:
            gruppen = [
                {
                    "kind": "file",
                    "key": "f1",
                    "file": {"id": "6d3f1f0a-0000-4000-8000-000000000001", "name": "Anlage 1"},
                    "others": [],
                }
            ]
        return {
            "groups": gruppen,
            "counts": {"vorgaenge": 9 if self.treffer else 0, "unterlagen": 1, "meetings": 0},
            "page": kwargs.get("page", 1),
            "pages": 1,
            "has_more": False,
            "similar_spelling": False,
            "totals_by_index": {"papers": 9, "files": 12, "meetings": 0, "persons": 2, "organizations": 0},
        }


@pytest.fixture
def dienst(monkeypatch: pytest.MonkeyPatch) -> _Dienst:
    from insight_core.services import search_service

    fake = _Dienst()
    monkeypatch.setattr(search_service, "get_search_service", lambda: fake)
    return fake


def _seite(client: Client, adresse: str, **headers: str) -> str:
    client.get("/insight/k/beispielstadt/")
    antwort = client.get(adresse, headers=headers)
    assert antwort.status_code == 200
    return antwort.content.decode()


@pytest.mark.django_db
def test_seite_ohne_javascript_mit_ortsband_reitern_und_filtern(
    strassen: OParlBody, dienst: _Dienst, make_paper: Callable[..., OParlPaper]
) -> None:
    _vorgang(strassen, "Fahrradverkehr im Westen", date(2026, 8, 13), [(CENTER_LAT + 409 * M_LAT, CENTER_LON)])

    html = _seite(Client(), "/insight/suche/?q=von-witzleben")

    assert "„von-witzleben“ in Beispielstadt" in html
    assert "9 Vorgänge und 1 Sitzungsunterlage" in html
    assert "Als Straße erkannt" in html and "Fahrradverkehr im Westen" in html and re.search(r"40\d m entfernt", html)
    assert 'id="suche-ortskarte"' in html and 'id="suche-ort-daten"' in html
    # Reiter mit Zählern, ohne leere Reiter; Filter gehören zum Formular; keine doppelten Kennungen
    reiter = html.split('aria-label="Arten der Treffer"', 1)[1].split("</nav>", 1)[0]
    assert re.search(r"Personen<span[^>]*>2</span>", reiter) and "Sitzungen" not in reiter
    assert html.count('form="suche-form"') >= 8
    assert "Antrag</span>" in html and ">3</span>" in html  # Arten zusammengefasst, Zähler addiert
    kennungen = re.findall(r'\sid="([^"]+)"', html)
    assert len(kennungen) == len(set(kennungen))
    assert "<script>" not in html.split("<main", 1)[-1].split("</main>", 1)[0]


@pytest.mark.django_db
def test_htmx_tauscht_nur_den_ergebnisbereich(strassen: OParlBody, dienst: _Dienst) -> None:
    client = Client()
    teil = _seite(client, "/insight/suche/?q=Kita&period=2y&paper_type=Antrag&sort=newest", HX_Request="true")

    assert "<html" not in teil and 'hx-swap-oob="innerHTML:#suche-titel"' in teil
    assert "Zeitraum: Letzte 2 Jahre" in teil and "Art: Antrag" in teil
    aufruf = dienst.aufrufe[-1]
    assert aufruf["sort"] == "newest" and aufruf["date_from"] and aufruf["file_paper_filter"] is not None
    assert sorted(aufruf["paper_type"]) == ["Antrag an den Rat", "Antrag an die BV Mitte"]

    weiter = _seite(client, "/insight/suche/?q=Kita&page=2", HX_Request="true")
    assert "<ol" not in weiter and 'hx-swap-oob="true"' in weiter


@pytest.mark.django_db
def test_nur_wortsuche_und_nulltreffer_hilfe(strassen: OParlBody, dienst: _Dienst) -> None:
    _vorgang(strassen, "Fahrradverkehr im Westen", date(2026, 8, 13), [(CENTER_LAT, CENTER_LON)])
    assert "Als Straße erkannt" not in _seite(Client(), "/insight/suche/?q=witzleben&ort=aus")

    dienst.treffer = False
    html = _seite(Client(), "/insight/suche/?q=Kita&period=12m")
    assert "Keine Treffer für „Kita“" in html and "Ohne Filter" in html


def test_komponenten_reiter_facette_textlink() -> None:
    from django.template import engines
    from django_cotton.compiler_regex import CottonCompiler

    def render(quelle: str, **kontext: Any) -> str:
        return engines["django"].from_string(CottonCompiler().process(quelle)).render(kontext)

    reiter = render(
        '<c-suche.reiter :tabs="t" />',
        t=[
            {"label": "Alle", "count": "7", "url": "?q=x", "active": True},
            {"label": "Personen", "count": "1", "url": "?q=x&result_type=persons", "active": False},
        ],
    )
    assert reiter.count('aria-current="page"') == 1 and 'hx-push-url="true"' in reiter
    facette = render(
        '<c-suche.facette titel="Art" name="paper_type" :optionen="o" />',
        o=[{"value": "Antrag", "count": "3", "checked": True}, {"value": "Vorlage", "count": "9", "checked": False}],
    )
    # zwei Felder und „Anwenden“ gehören zum Suchformular
    assert "<details" in facette and facette.count('form="suche-form"') == 3 and "(gewählt: Antrag)" in facette
    link = render('<c-suche.textlink href="?q=x" hx-get="?q=x">entfernen</c-suche.textlink>')
    assert 'href="?q=x"' in link and 'hx-get="?q=x"' in link and ">entfernen</a>" in link

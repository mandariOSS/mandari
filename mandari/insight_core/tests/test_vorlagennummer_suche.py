# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Suche nach Vorlagennummern im Bürgerportal (Issue #1102): Vorgangsliste (auch ``/insight/k/<slug>/vorgaenge/``),
globale Suche und ehrlicher Leerzustand.

Die Normalisierung selbst prüft ``hub/ris/tests/test_vorlagennummer.py``; hier geht es um das, was man sieht: genau
passende Vorlage oben und hervorgehoben, Eingabetaste bei genau einem Treffer direkt zum Vorgang (nicht bei HTMX),
Treffer der Datenbank in der Suche oben angeheftet ohne Dublette, „Die Vorlage … liegt uns noch nicht vor.“
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from insight_core.models import OParlBody, OParlPaper, OParlSource
from insight_core.services import search_service

pytestmark = pytest.mark.django_db

FEHLT = "liegt uns noch nicht vor."


def _kommune(n: int, name: str, slug: str) -> OParlBody:
    quelle = OParlSource.objects.create(name=name, url=f"https://ris{n}.example.org/system")
    return OParlBody.objects.create(
        external_id=f"https://ris{n}.example.org/body/1", source=quelle, name=name, slug=slug, is_listed=True
    )


def _vorgang(body: OParlBody, reference: str, name: str, tag: int) -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=f"{body.external_id}/paper/{reference}",
        body=body,
        reference=reference,
        name=name,
        paper_type="Vorlage",
        date=date(2026, 3, tag),
    )


@pytest.fixture
def bestand(db: Any) -> dict[str, Any]:
    muenster, koeln = _kommune(1, "Münster", "muenster"), _kommune(2, "Köln", "koeln")
    return {
        "muenster": muenster,
        "koeln": koeln,
        "vorlage": _vorgang(muenster, "V/0591/2026", "Neubau der Feuerwache", 2),
        "folge": _vorgang(muenster, "V/0591/2026/1", "Neubau der Feuerwache, Ergänzung", 20),
        "andere": _vorgang(muenster, "V/0700/2026", "Radweg am Kanal", 10),
        "koeln_an": _vorgang(koeln, "AN/0591/2026", "Antrag Spielplatz", 5),
    }


def _zeilen(html: str) -> list[str]:
    return re.findall(r"<tr class=\"([^\"]*)\"\s+data-href=\"[^\"]*\">(.*?)</tr>", html, re.S)


@pytest.mark.parametrize("eingabe", ["V/0591/2026", "V 0591 2026", "V/591/2026", "v0591/2026", "0591/2026"])
def test_liste_der_kommune_findet_jede_schreibweise(bestand: dict[str, Any], eingabe: str) -> None:
    antwort = Client().get("/insight/k/muenster/vorgaenge/", {"q": eingabe})

    assert antwort.status_code == 200
    zeilen = _zeilen(antwort.content.decode())
    # Die Liste je Kommune beachtet q (nur die beiden passenden Vorgänge), genau passende Nummer oben und hinterlegt,
    # die Folgevorlage direkt darunter, ohne Hervorhebung; Köln nicht
    assert [("Ergänzung" in inhalt) for _klassen, inhalt in zeilen] == [False, True]
    assert "bg-primary-50/70" in zeilen[0][0] and "bg-primary-50/70" not in zeilen[1][0]
    assert "(gesuchte Nummer)" in zeilen[0][1]
    assert "Spielplatz" not in antwort.content.decode()


def test_liste_mit_gewaehlter_kommune_und_htmx(bestand: dict[str, Any]) -> None:
    client = Client()
    client.get("/insight/k/koeln/")
    teil = client.get("/insight/vorgaenge/", {"q": "an 591 2026"}, headers={"HX-Request": "true"}).content.decode()

    assert "Antrag Spielplatz" in teil and "Feuerwache" not in teil and "<html" not in teil


def test_eingabetaste_oeffnet_den_einzigen_treffer(bestand: dict[str, Any]) -> None:
    client = Client()
    client.get("/insight/k/koeln/")

    direkt = client.get("/insight/vorgaenge/", {"q": "AN 591 2026", "direkt": "1"})
    assert direkt.status_code == 302 and direkt["Location"] == f"/insight/vorgaenge/{bestand['koeln_an'].pk}/"
    # Live-Suche per HTMX ersetzt nur die Liste und leitet nie weiter; ohne direkt=1 (geteilter Link) auch nicht
    teil = client.get("/insight/vorgaenge/", {"q": "AN 591 2026", "direkt": "1"}, headers={"HX-Request": "true"})
    assert teil.status_code == 200
    assert client.get("/insight/vorgaenge/", {"q": "AN 591 2026"}).status_code == 200
    # Mehrere Treffer (Vorlage und Folgevorlage) oder nur Textsuche: Liste statt Weiterleitung
    assert Client().get("/insight/k/muenster/vorgaenge/", {"q": "V/591/2026", "direkt": "1"}).status_code == 200
    assert client.get("/insight/vorgaenge/", {"q": "Spielplatz", "direkt": "1"}).status_code == 200


def test_formular_schickt_direkt_nur_mit_der_eingabetaste(bestand: dict[str, Any]) -> None:
    html = Client().get("/insight/k/muenster/vorgaenge/").content.decode()
    formular = html.split('id="paper-filter-form"', 1)[1].split("</form>", 1)[0]

    assert '<input type="hidden" name="direkt" value="1">' in formular
    # Die Live-Suche schickt nur das Suchfeld und die Art (hx-include), nicht direkt=1
    assert 'hx-include="#filter-type"' in formular


def test_leerzustand_bei_fehlender_nummer(bestand: dict[str, Any]) -> None:
    html = Client().get("/insight/k/muenster/vorgaenge/", {"q": "V 999 2026"}).content.decode()

    assert f"Die Vorlage V 999 2026 {FEHLT}" in html
    assert "über ihre Schnittstelle frei" in html and "Keine Vorgänge gefunden" not in html
    # Mit Filter der Art könnte die Vorlage nur ausgefiltert sein: dann der bisherige Hinweis
    gefiltert = Client().get("/insight/k/muenster/vorgaenge/", {"q": "V 999 2026", "type": "Antrag"}).content.decode()
    assert FEHLT not in gefiltert and "Keine Vorgänge gefunden" in gefiltert
    # Ein gewöhnlicher Suchbegriff ohne Treffer bleibt beim bisherigen Leerzustand
    assert FEHLT not in Client().get("/insight/k/muenster/vorgaenge/", {"q": "Schwimmbad"}).content.decode()


def test_nummernsuche_kostet_keine_weitere_abfrage(bestand: dict[str, Any]) -> None:
    """Die Nummern stecken als Unterabfrage in der Liste: so viele Abfragen wie bei einem gewöhnlichen Suchbegriff."""
    client = Client()
    client.get("/insight/k/muenster/vorgaenge/", {"q": "Radweg"})

    def abfragen(q: str) -> int:
        with CaptureQueriesContext(connection) as erfasst:
            assert client.get("/insight/k/muenster/vorgaenge/", {"q": q}).status_code == 200
        return len(erfasst)

    assert abfragen("V 0591 2026") == abfragen("Radweg")


# --- Globale Suche --------------------------------------------------------------------------------------------


class _Dienst:
    """Suchdienst, der die Nummer nur in einer anderen Schreibweise kennt: liefert ``vorgaenge`` als Treffer."""

    def __init__(self, vorgaenge: list[OParlPaper]) -> None:
        self.vorgaenge = vorgaenge

    def facet_counts(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {"paper_types": {}, "periods": {}}

    def search_grouped(self, query: str, **kwargs: Any) -> dict[str, Any]:
        gruppen = [
            {
                "kind": "paper",
                "key": str(p.pk),
                "paper": {"id": str(p.pk), "name": p.name, "reference": p.reference, "paper_type": p.paper_type},
                "others": [],
            }
            for p in self.vorgaenge
        ]
        return {
            "groups": gruppen,
            "counts": {"vorgaenge": len(gruppen), "unterlagen": 0, "meetings": 0},
            "page": kwargs.get("page", 1),
            "pages": 1,
            "has_more": False,
            "similar_spelling": False,
            "totals_by_index": {"papers": len(gruppen), "files": 0, "meetings": 0, "persons": 0, "organizations": 0},
        }


def _suche(monkeypatch: pytest.MonkeyPatch, kommune: str, q: str, treffer: list[OParlPaper]) -> str:
    monkeypatch.setattr(search_service, "get_search_service", lambda: _Dienst(treffer))
    client = Client()
    client.get(f"/insight/k/{kommune}/")
    antwort = client.get("/insight/suche/", {"q": q})
    assert antwort.status_code == 200
    return antwort.content.decode()


def test_suche_heftet_die_nummer_aus_der_datenbank_oben_an(
    bestand: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Der Suchdienst findet „V/591/2026“ nicht als Nummer, aber die Folgevorlage und einen Vorgang mit Textbezug
    html = _suche(monkeypatch, "muenster", "V/591/2026", [bestand["andere"], bestand["folge"]])

    liste = html.split('id="treffer-liste"', 1)[1].split("</ol>", 1)[0]
    titel = re.findall(r'<a href="/insight/vorgaenge/[^/]+/"[^>]*>([^<]+)</a>', liste)
    # Genau passend vorn und markiert, dann die Folgevorlage, dann die übrigen Treffer – jede nur einmal
    assert titel == ["Neubau der Feuerwache", "Neubau der Feuerwache, Ergänzung", "Radweg am Kanal"]
    assert liste.count("Gesuchte Vorlagennummer") == 1
    assert liste.index("Gesuchte Vorlagennummer") < liste.index("Neubau der Feuerwache, Ergänzung")
    assert "Spielplatz" not in liste


def test_suche_heftet_nur_aus_der_gewaehlten_kommune_an(
    bestand: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    html = _suche(monkeypatch, "koeln", "V/591/2026", [])

    assert "Feuerwache" not in html
    assert f"Die Vorlage V/591/2026 {FEHLT}" in html


def test_suche_ohne_nummer_wie_bisher(bestand: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    html = _suche(monkeypatch, "muenster", "Feuerwache", [])

    assert "Gesuchte Vorlagennummer" not in html and FEHLT not in html and "Keine Treffer für „Feuerwache“" in html

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verortung ohne Adresspunkte für Privatanschriften (Issue #1269).

Eingaben aus der Einwohnerschaft und Bauanträge bekommen höchstens den Punkt der Straße, Eingaben aus der Einwohnerschaft
werden nur aus dem Titel verortet. Dazu: Orte nur aus dem Hauptteil des Hauptdokuments, Sammellisten und Wirtschaftspläne
ohne Ort, Gattungsnamen nur mit Hausnummer, Fundstelle je Ort.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from insight_core.models import (
    Address,
    OParlBody,
    OParlFile,
    OParlLocation,
    OParlPaper,
    PaperLocation,
    Street,
)
from insight_core.services import verortung_pruefung
from insight_core.services.gazetteer import StreetGazetteer
from insight_core.services.georeferencing import (
    collect_paper_text,
    extract_with_gazetteer,
    hauptteil,
    process_paper_georef,
    titel_ohne_ort,
    update_paper_georef,
)
from insight_core.services.oparl_locations import apply_oparl_locations

pytestmark = pytest.mark.django_db

STRASSE = (51.9500, 7.6400)
HAUS = (51.9530, 7.6450)
ANDERE_STRASSE = (51.9400, 7.6300)


@pytest.fixture
def kommune(geo_body: OParlBody, make_street: Callable[..., Street], make_address: Callable[..., Address]) -> OParlBody:
    make_street(geo_body, "Wolbecker Straße", *STRASSE)
    make_street(geo_body, "Hafenweg", *ANDERE_STRASSE)
    make_street(geo_body, "Markt", 51.9610, 7.6280)
    make_address(geo_body, "Wolbecker Straße", "12a", *HAUS)
    return geo_body


def _datei(paper: OParlPaper, text: str, nummer: int = 1) -> OParlFile:
    return OParlFile.objects.create(
        external_id=f"{paper.external_id}/file/{nummer}",
        body=paper.body,
        paper=paper,
        name=f"Dokument {nummer}",
        text_content=text,
        text_extraction_status="completed",
    )


def _orte(paper: OParlPaper) -> list[dict[str, Any]]:
    paper.refresh_from_db()
    return list(paper.locations or [])


def _punkte(ergebnis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {eintrag["name"]: eintrag for eintrag in ergebnis.get("locations", [])}


class TestAdresspunkte:
    def test_gewoehnlicher_vorgang_behaelt_den_adresspunkt(
        self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        paper = make_paper(kommune, name="Sanierung der Schule Wolbecker Straße 12a", paper_type="Beschlussvorlage")
        orte = _punkte(process_paper_georef(paper, mode="regex"))
        assert orte["Wolbecker Straße 12a"]["source"] == "address_match"
        assert (orte["Wolbecker Straße 12a"]["lat"], orte["Wolbecker Straße 12a"]["lon"]) == HAUS
        assert orte["Wolbecker Straße 12a"]["fundstelle"] == "titel"

    def test_buergerantrag_nur_strasse_und_nur_aus_dem_titel(
        self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        paper = make_paper(kommune, name="Parkverbot vor Wolbecker Straße 12a", paper_type="Bürgerantrag")
        # Im Volltext steht die Anschrift der Einreichenden: sie wird nicht verortet
        _datei(paper, "Absender: Erika Mustermann, Hafenweg 3\n\nIch beantrage ein Parkverbot.")
        ergebnis = process_paper_georef(paper, mode="regex")
        orte = _punkte(ergebnis)
        assert list(orte) == ["Wolbecker Straße"]
        assert orte["Wolbecker Straße"]["source"] == "street_match"
        assert (orte["Wolbecker Straße"]["lat"], orte["Wolbecker Straße"]["lon"]) == STRASSE
        assert not any("12a" in name for name in orte)

    def test_bauantrag_volltext_aber_nur_strasse(
        self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        paper = make_paper(kommune, name="Bauvoranfrage Neubau Garage", paper_type="Vorlage")
        _datei(paper, "Das Grundstück Wolbecker Str. 12a liegt im Innenbereich.")
        orte = _punkte(process_paper_georef(paper, mode="regex"))
        assert orte["Wolbecker Straße"]["source"] == "street_match"
        assert (orte["Wolbecker Straße"]["lat"], orte["Wolbecker Straße"]["lon"]) == STRASSE
        assert orte["Wolbecker Straße"]["fundstelle"] == "dokument"

    def test_extract_ohne_adresspunkte(self, kommune: OParlBody) -> None:
        orte = extract_with_gazetteer(
            "Grundstück Wolbecker Straße 12a", StreetGazetteer(kommune), set(), adresspunkte=False
        )
        assert [(o["name"], o["source"], o["lat"]) for o in orte] == [("Wolbecker Straße", "street_match", STRASSE[0])]

    def test_neuer_lauf_entfernt_den_alten_adresspunkt(
        self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        paper = make_paper(kommune, name="Ampel Wolbecker Straße 12a", paper_type="Einwohnerfrage")
        update_paper_georef(
            paper,
            {
                "status": "completed",
                "method": "gazetteer",
                "locations": [
                    {"lat": HAUS[0], "lon": HAUS[1], "name": "Wolbecker Straße 12a", "source": "address_match"}
                ],
            },
        )
        update_paper_georef(paper, process_paper_georef(paper, mode="regex"))
        assert [(o["name"], o["lat"]) for o in _orte(paper)] == [("Wolbecker Straße", STRASSE[0])]
        assert list(PaperLocation.objects.filter(paper=paper).values_list("name", flat=True)) == ["Wolbecker Straße"]


class TestVolltext:
    def test_nur_hauptdokument_bis_zu_den_anlagen(
        self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        paper = make_paper(kommune, name="Spielplatz", paper_type="Beschlussvorlage")
        haupt = _datei(paper, "Der Spielplatz am Hafenweg wird erneuert.\n\nAnlagen:\nLageplan Wolbecker Straße\n", 1)
        _datei(paper, "Teilnehmende: Markt 2, Wolbecker Straße 12a", 2)
        paper.raw_json = {"mainFile": {"id": haupt.external_id}}
        paper.save(update_fields=["raw_json"])
        text = collect_paper_text(paper)
        assert "Hafenweg" in text and "Wolbecker" not in text and "Markt" not in text
        assert list(_punkte(process_paper_georef(paper, mode="regex"))) == ["Hafenweg"]

    def test_ohne_hauptdokument_alle_dokumente(self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
        paper = make_paper(kommune, name="Spielplatz", paper_type="Beschlussvorlage")
        _datei(paper, "Hafenweg", 1)
        _datei(paper, "Wolbecker Straße", 2)
        assert set(_punkte(process_paper_georef(paper, mode="regex"))) == {"Hafenweg", "Wolbecker Straße"}

    def test_hauptteil(self) -> None:
        assert hauptteil("A\nAnlage/n:\nB") == "A\n"
        assert hauptteil("Anlagen wurden geprüft") == "Anlagen wurden geprüft"

    @pytest.mark.parametrize(
        "titel",
        [
            "Punkte der nichtöffentlichen Sitzung",
            "Wirtschaftsplan 2027 der Stadtwerke",
            "Jahresabschluss 2025",
            "Entsendung in den Aufsichtsrat",
        ],
    )
    def test_titel_ohne_ort(self, titel: str, kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
        assert titel_ohne_ort(titel)
        paper = make_paper(kommune, name=titel, paper_type="Vorlage")
        _datei(paper, "Sitz der Gesellschaft: Hafenweg 3")
        ergebnis = process_paper_georef(paper, mode="regex")
        assert ergebnis["status"] == "no_locations" and not ergebnis["locations"]

    def test_gattungsname_nur_mit_hausnummer(self, kommune: OParlBody) -> None:
        gazetteer = StreetGazetteer(kommune)
        assert extract_with_gazetteer("Stände auf dem Markt", gazetteer, set()) == []
        assert [o["name"] for o in extract_with_gazetteer("Rathaus, Markt 2", gazetteer, set())] == ["Markt 2"]


class TestOrteDerKommune:
    def _ort(self, paper: OParlPaper, adresse: str, lat: float, lon: float) -> None:
        ort = OParlLocation.objects.create(
            external_id=f"{paper.external_id}/location",
            body=paper.body,
            street_address=adresse,
            geojson={"type": "Point", "coordinates": [lon, lat]},
        )
        paper.oparl_locations.add(ort)

    def test_privatanschrift_der_kommune_wird_strasse(
        self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        paper = make_paper(kommune, name="Gefahrenstelle", paper_type="Bürgerantrag")
        self._ort(paper, "Wolbecker Straße 12a", *HAUS)
        assert apply_oparl_locations(paper)
        assert [(o["name"], o["lat"], o["lon"], o["source"]) for o in _orte(paper)] == [
            ("Wolbecker Straße", *STRASSE, "oparl")
        ]

    def test_unbekannte_strasse_entfaellt(self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
        paper = make_paper(kommune, name="Gefahrenstelle", paper_type="Bürgerantrag")
        self._ort(paper, "Unbekannter Weg 7", *HAUS)
        apply_oparl_locations(paper)
        paper.refresh_from_db()
        assert not paper.locations

    def test_andere_vorgaenge_behalten_den_punkt(
        self, kommune: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        paper = make_paper(kommune, name="Neubau Feuerwache", paper_type="Beschlussvorlage")
        self._ort(paper, "Wolbecker Straße 12a", *HAUS)
        apply_oparl_locations(paper)
        assert [(o["name"], o["lat"]) for o in _orte(paper)] == [("Wolbecker Straße 12a", HAUS[0])]


def test_ki_pruefung_ergaenzt_nur_die_strasse(kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = make_paper(kommune, name="Lärm", paper_type="Anregungen und Beschwerden")
    punkt = verortung_pruefung._geokodieren("Wolbecker Straße 12a", paper, StreetGazetteer(kommune))
    assert punkt is not None
    assert (punkt["name"], punkt["lat"], punkt["lon"]) == ("Wolbecker Straße", *STRASSE)
    normal = make_paper(kommune, name="Feuerwache", paper_type="Beschlussvorlage")
    punkt = verortung_pruefung._geokodieren("Wolbecker Straße 12a", normal, StreetGazetteer(kommune))
    assert punkt is not None and (punkt["lat"], punkt["lon"]) == HAUS

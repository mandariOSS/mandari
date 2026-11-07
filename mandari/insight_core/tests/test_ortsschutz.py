# SPDX-License-Identifier: AGPL-3.0-or-later
"""Datenschutz bei Orten (Issue #1269): Vorgangsarten mit Privatanschrift erkennen, Hausnummern auslassen."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from insight_core.models import OParlBody, OParlOrganization, OParlPaper, Street
from insight_core.services import ortsschutz
from insight_core.services.ortsschutz import (
    BAUORDNUNG,
    EINWOHNERSCHAFT,
    OHNE_NR,
    grund_nach_art,
    hat_hausnummer,
    hausnummern_auslassen,
    oeffentlicher_titel,
    ort_ohne_hausnummer,
)


@pytest.mark.parametrize(
    ("titel", "erwartet"),
    [
        ("Parkverbot vor Musterstraße 12a", f"Parkverbot vor Musterstraße {OHNE_NR}"),
        ("Gefahrenstelle Endenicher Allee 12-14", f"Gefahrenstelle Endenicher Allee {OHNE_NR}"),
        ("Musterstr. 5, 53111 Bonn", f"Musterstr. {OHNE_NR}, 53111 Bonn"),
        ("Musterweg Nr. 7 u. 9", f"Musterweg {OHNE_NR}"),
        ("Am Wehr 4 und Zur Linde 2", f"Am Wehr {OHNE_NR} und Zur Linde {OHNE_NR}"),
        ("Baum vor Hausnummer 82", f"Baum vor {OHNE_NR}"),
        ("(neben Haus-Nr.) 68", f"(neben {OHNE_NR})"),
        ("Wolbecker Straße 1 bis 3", f"Wolbecker Straße {OHNE_NR}"),
        ("Markt 2", f"Markt {OHNE_NR}"),
    ],
)
def test_hausnummer_hinter_strasse_wird_ausgelassen(titel: str, erwartet: str) -> None:
    assert hausnummern_auslassen(titel) == erwartet
    assert hat_hausnummer(titel)


@pytest.mark.parametrize(
    "titel",
    [
        "Sanierung Musterstraße 2027",  # Jahr, keine Hausnummer
        "Im Jahr 2026 zum dritten Mal",
        "Haushaltsplan 2027, Produkt 12",
        "Faktor 2 beim Beschluss 12",
        "Bürgerantrag zur Musterstraße",
        "",
    ],
)
def test_ohne_hausnummer_bleibt_der_titel(titel: str) -> None:
    assert hausnummern_auslassen(titel) == titel
    assert not hat_hausnummer(titel)


def test_ort_ohne_hausnummer() -> None:
    assert ort_ohne_hausnummer("Musterstraße 12, 53111 Bonn") == "Musterstraße, 53111 Bonn"
    assert ort_ohne_hausnummer("Wolbecker Straße 12a") == "Wolbecker Straße"
    # Ohne Grundwort (-straße, -weg …) zählt die Zahl am Ende
    assert ort_ohne_hausnummer("Hohe Pforte 3") == "Hohe Pforte"
    assert ort_ohne_hausnummer("Rathaus") == "Rathaus"


@pytest.mark.parametrize(
    ("art", "titel", "grund"),
    [
        ("Bürgerantrag § 24 GO - Öffentlich", "Gefahrenstelle Radweg", EINWOHNERSCHAFT),
        ("Einwohnerfrage", "Ampel", EINWOHNERSCHAFT),
        ("Anregungen und Beschwerden", "Lärm", EINWOHNERSCHAFT),
        ("Vorlage", "Anregungen und Beschwerden: Parkplatz Musterweg 3", EINWOHNERSCHAFT),
        ("Mitteilung", "Stellungnahme zum Bürgerantrag Spielplatz", EINWOHNERSCHAFT),
        ("Bauantrag", "Neubau Garage", BAUORDNUNG),
        ("Vorlage", "Bauvoranfrage Musterweg 3", BAUORDNUNG),
        ("Beschlussvorlage", "Einvernehmen der Gemeinde nach § 36 BauGB, Musterweg 3", BAUORDNUNG),
        ("Mitteilung des Oberbürgermeisters", "Haushalt 2027", ""),
        ("Bürgerbegehren", "Radentscheid", ""),
        ("Antrag", "Radweg Musterstraße", ""),
        (None, None, ""),
    ],
)
def test_grund_nach_art(art: str | None, titel: str | None, grund: str) -> None:
    assert grund_nach_art(art, titel) == grund


def test_oeffentlicher_titel_nur_bei_privatanschrift() -> None:
    assert oeffentlicher_titel("Ampel Musterstraße 12", "Einwohnerfrage") == f"Ampel Musterstraße {OHNE_NR}"
    # Andere Vorgänge behalten ihren Titel, auch mit Hausnummer (Rathaus, Schule …)
    assert oeffentlicher_titel("Sanierung Schule Musterstraße 12", "Beschlussvorlage") == (
        "Sanierung Schule Musterstraße 12"
    )
    # Ein bekanntes Ergebnis der gründlichen Prüfung hat Vorrang
    assert oeffentlicher_titel("Stellungnahme Musterstraße 12", "Mitteilung", BAUORDNUNG).endswith(OHNE_NR)
    assert oeffentlicher_titel(None) == ""


def test_titel_oeffentlich_am_modell(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = make_paper(geo_body, name="Parkverbot Musterstraße 12", paper_type="Bürgerantrag")
    assert paper.titel_oeffentlich == f"Parkverbot Musterstraße {OHNE_NR}"
    # Intern bleibt der volle Titel
    assert OParlPaper.objects.get(pk=paper.pk).name == "Parkverbot Musterstraße 12"


@pytest.mark.django_db
class TestGrund:
    def test_federfuehrendes_amt_der_bauordnung(
        self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        amt = OParlOrganization.objects.create(
            external_id="https://ris.beispielstadt.example/oparl/organization/63",
            body=geo_body,
            name="63 Bauordnungsamt",
        )
        paper = make_paper(
            geo_body,
            name="Neubau Musterweg 3",
            paper_type="Mitteilung",
            raw_json={"underDirectionOf": [amt.external_id]},
        )
        assert grund_nach_art(paper.paper_type, paper.name) == ""
        assert ortsschutz.grund(paper) == BAUORDNUNG

    def test_verbundener_vorgang_aus_der_einwohnerschaft(
        self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        antrag = make_paper(geo_body, name="Tempo 30 Musterweg 3", paper_type="Bürgerantrag")
        stellungnahme = make_paper(
            geo_body,
            name="Tempo 30 Musterweg 3",
            paper_type="Stellungnahme der Verwaltung",
            raw_json={"superordinatedPaper": [{"id": antrag.external_id}]},
        )
        assert ortsschutz.grund(stellungnahme) == EINWOHNERSCHAFT

    def test_sonst_kein_grund(self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
        paper = make_paper(geo_body, name="Radweg Musterweg", paper_type="Antrag", raw_json={"underDirectionOf": "x"})
        assert ortsschutz.grund(paper) == ""


@pytest.mark.django_db
def test_strassenpunkt(geo_body: OParlBody, make_street: Callable[..., Street]) -> None:
    make_street(geo_body, "Musterstraße", 51.95, 7.62)
    make_street(geo_body, "Musterstraße", 51.97, 7.64)  # zweiter Abschnitt: Mittel
    punkt: dict[str, Any] | None = ortsschutz.strassenpunkt(geo_body, "Musterstraße 12, 48143 Münster")
    assert punkt == {"lat": 51.96, "lon": 7.63, "name": "Musterstraße"}
    assert ortsschutz.strassenpunkt(geo_body, "Unbekannter Weg 3") is None
    assert ortsschutz.strassenpunkt(None, "Musterstraße 12") is None

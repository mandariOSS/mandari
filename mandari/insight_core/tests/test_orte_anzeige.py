# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Orte auf der Vorgangsseite (Issue #1269): Sicherheit und Fundstelle je Ort, „stadtweit“ statt langer Straßenlisten,
begrenzte Randspalte, keine Hausnummern bei Privatanschriften – auf der Seite, im Seitentitel und auf der Karte.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    PaperLocation,
)
from insight_core.services.orte_anzeige import (
    ERSCHLOSSEN,
    GESICHERT,
    ORTE_SICHTBAR,
    UNSICHER,
    bezirke,
    ortsangaben,
    sicherheit,
)
from insight_core.services.ortsschutz import OHNE_NR
from insight_core.services.paper_locations import sync_paper_locations
from insight_core.tests.conftest import CENTER_LAT, CENTER_LON

pytestmark = pytest.mark.django_db


def _ort(name: str, lat: float = CENTER_LAT, lon: float = CENTER_LON, **mehr: Any) -> dict[str, Any]:
    return {"lat": lat, "lon": lon, "name": name, "source": "street_match", **mehr}


def _randspalte(html: str) -> str:
    treffer = re.search(r'<section aria-labelledby="orte-titel" data-testid="vorgang-orte">(.*?)</section>', html, re.S)
    assert treffer, "Orte fehlen"
    return treffer.group(1)


@pytest.mark.parametrize(
    ("eintrag", "status", "urteil", "stufe", "fundstelle"),
    [
        ({"source": "oparl"}, "", "", GESICHERT, "Angabe der Kommune"),
        ({"source": "plan_boundary"}, "", "", GESICHERT, "amtlicher Umring des Bebauungsplans"),
        ({"source": "manual"}, "", "", GESICHERT, "von Hand eingetragen"),
        ({"source": "street_match"}, PaperLocation.STATUS_CONFIRMED, "", GESICHERT, "von Hand geprüft"),
        ({"source": "street_match", "fundstelle": "titel"}, "", "", ERSCHLOSSEN, "aus dem Titel"),
        ({"source": "address_match", "fundstelle": "dokument"}, "", "", ERSCHLOSSEN, "aus dem Dokument"),
        ({"source": "street_match"}, "", "", ERSCHLOSSEN, "aus den Dokumenten"),
        (
            {"source": "street_match"},
            "",
            PaperLocation.KI_UNSICHER,
            UNSICHER,
            "im Dokument genannt, Zuordnung unsicher",
        ),
        ({"source": "ai"}, "", "", UNSICHER, "von einer KI erkannt"),
        ({"source": "ai_check"}, "", "", UNSICHER, "von einer KI ergänzt"),
    ],
)
def test_sicherheit_je_herkunft(eintrag: dict[str, Any], status: str, urteil: str, stufe: str, fundstelle: str) -> None:
    assert sicherheit(eintrag, status, urteil) == (stufe, fundstelle)


def test_bezirke_aus_dem_beratungsverlauf() -> None:
    verlauf: list[dict[str, Any]] = [
        {"meeting": None, "organization_name": "Bezirksvertretung Beuel"},
        {"meeting": None, "organization_name": "Bezirksvertretung Beuel"},
        {"meeting": None, "organization_name": "Ortsrat Mitte"},
        {"meeting": None, "organization_name": "Ausschuss für Mobilität"},
        {"meeting": None, "organization_name": None},
    ]
    assert bezirke(verlauf) == ["Beuel", "Mitte"]


class TestOrtsangaben:
    def test_randspalte_begrenzt_sicherste_zuerst(
        self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        orte = [_ort(f"Straße {i}", CENTER_LAT + i * 0.0001) for i in range(8)]
        orte.append(_ort("Rathaus", CENTER_LAT - 0.001, source="oparl"))
        paper = make_paper(geo_body, locations=orte)
        angaben = ortsangaben(paper, orte, [])
        assert angaben.anzahl == 9 and len(angaben.orte) == ORTE_SICHTBAR and len(angaben.weitere) == 4
        assert angaben.orte[0].name == "Rathaus" and angaben.orte[0].sicherheit == GESICHERT
        assert not angaben.stadtweit

    def test_stadtweit_ab_drei_bezirken(self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
        orte = [_ort("Hafenweg")]
        paper = make_paper(geo_body, locations=orte)
        verlauf = [{"meeting": None, "organization_name": f"Bezirksvertretung {n}"} for n in ("Nord", "Ost", "Süd")]
        angaben = ortsangaben(paper, orte, verlauf)
        assert angaben.stadtweit == "Beraten in 3 Stadtbezirken: Nord, Ost und Süd."
        assert angaben.orte == [] and [o.name for o in angaben.weitere] == ["Hafenweg"]
        assert not ortsangaben(paper, orte, verlauf[:2]).stadtweit

    def test_stadtweit_wenn_ueber_das_stadtgebiet_verteilt(
        self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        # Kommune: 0,2° Nord-Süd; die Orte überspannen 0,12° (mehr als die Hälfte)
        verteilt = [_ort(f"Weg {i}", CENTER_LAT - 0.06 + i * 0.024) for i in range(6)]
        paper = make_paper(geo_body, locations=verteilt)
        assert "über das ganze Stadtgebiet verteilt" in ortsangaben(paper, verteilt, []).stadtweit
        nah = [_ort(f"Weg {i}", CENTER_LAT + i * 0.001) for i in range(6)]
        assert not ortsangaben(paper, nah, []).stadtweit

    def test_privatanschrift_ohne_hausnummer(self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
        orte = [_ort("Wolbecker Straße 12a", source="oparl"), _ort("Wolbecker Straße", CENTER_LAT + 0.01)]
        paper = make_paper(geo_body, locations=orte)
        angaben = ortsangaben(paper, orte, [], "einwohnerschaft")
        # Beide heißen ohne Hausnummer gleich: ein Eintrag, der gesicherte vorn
        assert [(o.name, o.sicherheit) for o in angaben.orte] == [("Wolbecker Straße", GESICHERT)]
        assert angaben.ohne_hausnummer


def _beratung(body: OParlBody, paper: OParlPaper, gremium: str, nummer: int) -> None:
    organisation = OParlOrganization.objects.create(
        external_id=f"https://ris.beispielstadt.example/oparl/organization/{uuid.uuid4()}", body=body, name=gremium
    )
    sitzung = OParlMeeting.objects.create(
        external_id=f"https://ris.beispielstadt.example/oparl/meeting/{uuid.uuid4()}",
        body=body,
        name=gremium,
        start=timezone.now() - timedelta(days=nummer),
    )
    sitzung.organizations.add(organisation)
    punkt = OParlAgendaItem.objects.create(
        external_id=f"{sitzung.external_id}/top", meeting=sitzung, number=str(nummer), result="Kenntnisnahme"
    )
    OParlConsultation.objects.create(
        external_id=f"{sitzung.external_id}/consultation",
        body=body,
        paper=paper,
        meeting_external_id=sitzung.external_id,
        agenda_item_external_id=punkt.external_id,
    )


class TestVorgangsseite:
    def test_buergerantrag_ohne_hausnummer_mit_sicherheit(
        self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        paper = make_paper(
            geo_body,
            name="Parkverbot vor Wolbecker Straße 12a",
            paper_type="Bürgerantrag",
            reference="BA/0001/2026",
            locations=[_ort("Wolbecker Straße 12a", source="oparl")],
        )
        sync_paper_locations(paper)
        html = Client().get(f"/insight/vorgaenge/{paper.pk}/").content.decode()
        assert "Straße 12a" not in html
        assert f"Parkverbot vor Wolbecker Straße {OHNE_NR}" in html
        orte = _randspalte(html)
        assert 'data-sicherheit="gesichert"' in orte and "Angabe der Kommune" in orte
        assert "Bei Eingaben aus der Einwohnerschaft und Bauanträgen nennen wir keine Hausnummern." in orte
        daten = re.search(r'id="paper-map-data"[^>]*>(.*?)</script>', html, re.S)
        assert daten is not None
        karte = json.loads(daten.group(1))
        assert [ort["name"] for ort in karte["places"]] == ["Wolbecker Straße"]

    def test_lange_ortsliste_wird_stadtweit(self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
        orte = [_ort(f"Straße {i}", CENTER_LAT + i * 0.0001, fundstelle="dokument") for i in range(120)]
        paper = make_paper(geo_body, name="Stellungnahme zu Straßenschäden", paper_type="Stellungnahme", locations=orte)
        for nummer, bezirk in enumerate(("Nord", "Ost", "Süd", "West"), start=1):
            _beratung(geo_body, paper, f"Bezirksvertretung {bezirk}", nummer)
        html = Client().get(f"/insight/vorgaenge/{paper.pk}/").content.decode()
        orte_html = _randspalte(html)
        assert 'data-testid="orte-stadtweit"' in orte_html
        assert "Beraten in 4 Stadtbezirken:" in orte_html
        assert "Genannte Orte (120)" in orte_html
        # Keine Zeile je Straße mehr
        assert orte_html.count("data-sicherheit=") == 0

    def test_wenige_orte_als_liste_mit_fundstelle(
        self, geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
    ) -> None:
        orte = [_ort(f"Straße {i}", CENTER_LAT + i * 0.0001, fundstelle="titel") for i in range(7)]
        paper = make_paper(geo_body, name="Sanierung", paper_type="Beschlussvorlage", locations=orte)
        orte_html = _randspalte(Client().get(f"/insight/vorgaenge/{paper.pk}/").content.decode())
        assert orte_html.count('data-sicherheit="erschlossen"') == ORTE_SICHTBAR
        assert "aus dem Titel" in orte_html and "2 weitere Orte" in orte_html
        assert "ein Straßenpunkt steht für die ganze Straße." in orte_html

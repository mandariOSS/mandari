# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Entfernte OParl-Orte (Issue #553): Nennt die Quelle einen Ort einer Vorlage nicht mehr, entfernt der
Ingestor die Verknüpfung (``oparl_papers_locations``). Die daraus übernommenen Koordinaten
(``OParlPaper.locations`` mit Herkunft ``oparl``) räumt der Georef-Lauf auf; andere Verortungen bleiben.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from insight_core.models import OParlBody, OParlLocation, OParlPaper, PaperLocation
from insight_core.services import georef_runner
from insight_core.services.oparl_locations import apply_oparl_locations
from insight_core.tests.conftest import CENTER_LAT, CENTER_LON

pytestmark = pytest.mark.django_db

STRASSE = {"lat": CENTER_LAT + 0.01, "lon": CENTER_LON, "name": "Hafenweg", "source": "street_match"}


def _ort(body: OParlBody, nummer: int, lat: float) -> OParlLocation:
    return OParlLocation.objects.create(
        external_id=f"https://ris.beispielstadt.example/oparl/locations/{nummer}",
        body=body,
        street_address=f"Prinzipalmarkt {nummer}",
        geojson={"type": "Point", "coordinates": [CENTER_LON, lat]},
    )


@pytest.fixture
def verortet(geo_body: OParlBody, make_paper: Callable[..., OParlPaper]) -> OParlPaper:
    """Vorlage mit zwei OParl-Orten und einer Verortung aus dem Straßenverzeichnis."""
    paper = make_paper(geo_body, locations=[dict(STRASSE, confidence=0.8)])
    paper.oparl_locations.add(_ort(geo_body, 1, CENTER_LAT), _ort(geo_body, 2, CENTER_LAT - 0.01))
    assert apply_oparl_locations(paper)
    paper.refresh_from_db()
    assert sorted(loc["source"] for loc in paper.locations or []) == ["oparl", "oparl", "street_match"]
    return paper


def _herkunft(paper: OParlPaper) -> list[tuple[str, str]]:
    paper.refresh_from_db()
    return sorted((loc["source"], loc["name"]) for loc in paper.locations or [])


def test_entfernter_ort_verschwindet_aus_der_verortung(verortet: OParlPaper) -> None:
    verortet.oparl_locations.remove(OParlLocation.objects.get(street_address="Prinzipalmarkt 2"))
    assert apply_oparl_locations(OParlPaper.objects.get(pk=verortet.pk))
    assert _herkunft(verortet) == [("oparl", "Prinzipalmarkt 1"), ("street_match", "Hafenweg")]
    assert PaperLocation.objects.filter(paper=verortet, source="oparl").count() == 1


def test_ohne_ort_verschwinden_alle_uebernommenen_koordinaten(verortet: OParlPaper) -> None:
    verortet.oparl_locations.clear()
    assert apply_oparl_locations(OParlPaper.objects.get(pk=verortet.pk))
    assert _herkunft(verortet) == [("street_match", "Hafenweg")]
    assert not PaperLocation.objects.filter(paper=verortet, source="oparl").exists()
    # Idempotent: nichts mehr zu tun
    assert not apply_oparl_locations(OParlPaper.objects.get(pk=verortet.pk))


def test_bestaetigter_ort_bleibt(verortet: OParlPaper) -> None:
    """Eine im Admin bestätigte Verortung überlebt auch das Entfernen in der Quelle."""
    PaperLocation.objects.filter(paper=verortet, name="Prinzipalmarkt 1").update(status=PaperLocation.STATUS_CONFIRMED)
    verortet.oparl_locations.clear()
    apply_oparl_locations(OParlPaper.objects.get(pk=verortet.pk))
    assert _herkunft(verortet) == [("oparl", "Prinzipalmarkt 1"), ("street_match", "Hafenweg")]


def test_vorlage_ohne_oparl_eintraege_bleibt_unberuehrt(
    geo_body: OParlBody, make_paper: Callable[..., OParlPaper]
) -> None:
    paper = make_paper(geo_body, locations=[dict(STRASSE, confidence=0.8)])
    assert not apply_oparl_locations(paper)
    assert _herkunft(paper) == [("street_match", "Hafenweg")]


def test_georef_lauf_findet_vorlagen_ohne_verknuepften_ort(
    verortet: OParlPaper, geo_body: OParlBody, make_paper: Callable[..., OParlPaper], monkeypatch: Any
) -> None:
    """Der Lauf sah bisher nur Vorlagen mit Verknüpfung; ohne jede blieben die Koordinaten stehen."""
    unberuehrt = make_paper(geo_body)
    noch_verknuepft = make_paper(geo_body)
    noch_verknuepft.oparl_locations.add(OParlLocation.objects.get(street_address="Prinzipalmarkt 1"))
    verortet.oparl_locations.clear()

    gefunden = georef_runner._papers_with_stale_oparl_locations(50)
    assert [paper.pk for paper in gefunden] == [verortet.pk]
    assert unberuehrt.pk not in {paper.pk for paper in gefunden}

    from insight_core.models import Street

    # Ohne Straßenverzeichnis endet der Lauf nach der Übernahme der OParl-Orte
    assert not Street.objects.exists()
    stats = georef_runner._run_pass(50)
    assert stats["oparl_backfilled"] >= 1
    assert _herkunft(verortet) == [("street_match", "Hafenweg")]

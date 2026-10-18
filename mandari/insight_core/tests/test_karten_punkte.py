# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Punkte der Vorgangskarten (Issue #853): Insight und Work fragen dieselbe Abfrage
(``insight_core.services.karten_punkte`` auf ``hub.ris.selectors.paper_places``) und zeichnen mit demselben
Kartenmodul (``frontend/js/vorgangskarte.ts``).

Die Karte in Insight las vorher das JSON am Vorgang: Im Admin entfernte Verortungen blieben sichtbar, das
Inline-Skript setzte Titel und Ortsnamen als HTML ins Popup. Jetzt kommen die Punkte aus der Tabelle der
Verortungen, ohne gelöschte Vorgänge und entfernte Verortungen, und die Seite hat kein Inline-Skript mehr.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.work.ris import services as work_services
from insight_core.models import OParlBody, OParlPaper, OParlSource, PaperLocation
from insight_core.services import karten_punkte

pytestmark = pytest.mark.django_db

BASIS = "https://ris.punktstadt.example/oparl"


def _kennung(art: str) -> str:
    return f"{BASIS}/{art}/{uuid.uuid4()}"


def _vorgang(body: OParlBody, name: str, tage: int | None, **felder: Any) -> OParlPaper:
    datum = timezone.localdate() - timedelta(days=tage) if tage is not None else None
    return OParlPaper.objects.create(
        external_id=_kennung("papers"), body=body, name=name, reference=f"V/{name[:3]}", date=datum, **felder
    )


def _ort(vorgang: OParlPaper, lat: float, lon: float, name: str = "", **felder: Any) -> PaperLocation:
    return PaperLocation.objects.create(
        paper=vorgang, body=vorgang.body, latitude=lat, longitude=lon, name=name, **felder
    )


@pytest.fixture
def body() -> OParlBody:
    cache.clear()
    source = OParlSource.objects.create(name="Punkt-RIS", url=f"{BASIS}/system")
    return OParlBody.objects.create(external_id=_kennung("bodies"), source=source, name="Punktstadt", slug="punktstadt")


def _insight(body: OParlBody) -> Client:
    client = Client()
    client.get(f"/insight/kommune/{body.id}/")
    return client


def test_ausschnitt_nur_mit_gueltigen_grenzen() -> None:
    assert karten_punkte.ausschnitt("7.5,51.9,7.7,52.0") == (7.5, 51.9, 7.7, 52.0)
    for kaputt in ("", None, "1,2,3", "7.7,51.9,7.5,52.0", "7.5,52.0,7.7,51.9", "7.5,51.9,7.7,95", "x,1,2,3"):
        assert karten_punkte.ausschnitt(kaputt) is None


def test_geojson_mit_adresse_und_grenze(body: OParlBody) -> None:
    for nummer in range(3):
        _ort(_vorgang(body, f"Vorgang {nummer}", nummer), 51.9, 7.6, f"Ort {nummer}")

    daten = karten_punkte.geojson([body], url=lambda pk: f"/vorgang/{pk}/", hoechstens=2)

    assert daten["type"] == "FeatureCollection" and daten["truncated"] is True
    erster = daten["features"][0]
    assert erster["geometry"]["coordinates"] == [7.6, 51.9]
    assert erster["properties"]["title"] == "Vorgang 0" and erster["properties"]["location_name"] == "Ort 0"
    assert erster["properties"]["url"] == f"/vorgang/{erster['properties']['id']}/"
    assert "url" not in karten_punkte.geojson([body])["features"][0]["properties"]


def test_insight_karte_liest_die_verortungen_ohne_geloeschte_und_entfernte(body: OParlBody) -> None:
    markt = _vorgang(body, "Marktplatz", 3)
    _ort(markt, 51.96, 7.62, "Marktplatz")
    _ort(markt, 51.97, 7.63, "Falsch verortet", status=PaperLocation.STATUS_REMOVED)
    _ort(_vorgang(body, "Gelöscht", 2, deleted=True), 51.95, 7.61)
    alt = _vorgang(body, "Alt", 400)
    _ort(alt, 52.5, 13.4, "Weit weg")

    client = _insight(body)
    kurz = client.get("/insight/karte/partials/markers/?weeks=4").json()

    assert [f["properties"]["location_name"] for f in kurz["features"]] == ["Marktplatz"]
    assert kurz["features"][0]["properties"]["url"] == f"/insight/vorgaenge/{markt.pk}/"
    assert kurz["truncated"] is False
    alle = client.get("/insight/karte/partials/markers/?all=1").json()
    assert {f["properties"]["title"] for f in alle["features"]} == {"Marktplatz", "Alt"}
    ausschnitt = client.get("/insight/karte/partials/markers/?all=1&bbox=13,52,14,53").json()
    assert [f["properties"]["title"] for f in ausschnitt["features"]] == ["Alt"]


def test_insight_und_work_zeigen_dieselben_punkte(body: OParlBody) -> None:
    for nummer, (lat, lon) in enumerate(((51.96, 7.62), (51.95, 7.61), (52.5, 13.4))):
        _ort(_vorgang(body, f"Vorgang {nummer}", 10 + nummer), lat, lon, f"Ort {nummer}")

    insight = _insight(body).get("/insight/karte/partials/markers/?all=1&bbox=7.5,51.9,7.7,52.0").json()
    work = work_services.karte_daten(
        OParlBody.objects.filter(pk=body.pk), {"zeitraum": "alle", "bbox": "7.5,51.9,7.7,52.0"}
    )

    ohne_adresse = [{k: v for k, v in f["properties"].items() if k != "url"} for f in insight["features"]]
    assert ohne_adresse == [f["properties"] for f in work["features"]]
    assert len(ohne_adresse) == 2


def test_insight_karte_ohne_inline_skript_mit_kachel_proxy(body: OParlBody) -> None:
    html = _insight(body).get("/insight/karte/").content.decode()

    assert 'x-data="insightKarte"' in html and "mapApp" not in html
    assert 'data-kacheln="/insight/tiles/0/0/0"' in html
    assert 'data-daten="/insight/karte/partials/markers/"' in html
    assert 'id="insight-karte-start"' in html
    inhalt = html[html.index('x-data="insightKarte"') :]
    assert "<script nonce" not in inhalt and "tile.openstreetmap" not in html

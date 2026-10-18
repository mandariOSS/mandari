# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Lese-Fassade: Verortungen von Vorgängen für Karten (``paper_places``, Issue #853).

Die Karte in Work zeigte früher die 500 neuesten Vorgänge mit Ort aus dem JSON am Vorgang, auch gelöschte. Die
Fassade liest jetzt die indizierte Tabelle der Verortungen: nur die Kommunen der Organisation, ohne gelöschte bzw.
zurückgenommene Vorgänge und ohne entfernte Verortungen, wahlweise je Kartenausschnitt und ab einem Datum,
neueste Vorgänge zuerst.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from hub.ris import selectors as ris
from insight_core.models import OParlBody, OParlPaper, OParlSource, PaperLocation

pytestmark = pytest.mark.django_db

BASIS = "https://ris.kartenstadt.example/oparl"


def _kennung(art: str) -> str:
    return f"{BASIS}/{art}/{uuid.uuid4()}"


def _vorgang(body: OParlBody, name: str, datum: date | None, **felder: object) -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=_kennung("papers"), body=body, name=name, reference=f"V/{name[:3]}", date=datum, **felder
    )


def _ort(vorgang: OParlPaper, lat: float, lon: float, name: str = "", **felder: object) -> PaperLocation:
    return PaperLocation.objects.create(
        paper=vorgang, body=vorgang.body, latitude=lat, longitude=lon, name=name, **felder
    )


@pytest.fixture
def kommunen() -> tuple[OParlBody, OParlBody]:
    source = OParlSource.objects.create(name="Karten-RIS", url=f"{BASIS}/system")
    eigene = OParlBody.objects.create(external_id=_kennung("bodies"), source=source, name="Kartenstadt")
    fremde = OParlBody.objects.create(external_id=_kennung("bodies"), source=source, name="Nachbarstadt")
    return eigene, fremde


def test_nur_eigene_kommunen_ohne_geloeschte_und_entfernte(kommunen: tuple[OParlBody, OParlBody]) -> None:
    eigene, fremde = kommunen
    markt = _vorgang(eigene, "Marktplatz", date(2026, 9, 1))
    _ort(markt, 51.96, 7.62, "Marktplatz")
    _ort(markt, 51.97, 7.63, "Falsch verortet", status=PaperLocation.STATUS_REMOVED)
    _ort(_vorgang(eigene, "Gelöscht", date(2026, 9, 2), deleted=True), 51.95, 7.61)
    _ort(_vorgang(fremde, "Nachbar", date(2026, 9, 3)), 51.94, 7.60)

    orte = ris.paper_places([eigene])

    assert [(o.title, o.place) for o in orte] == [("Marktplatz", "Marktplatz")]
    assert orte[0].paper_id == markt.pk and orte[0].reference == "V/Mar"
    assert (orte[0].latitude, orte[0].longitude) == (51.96, 7.62) and orte[0].paper_date == date(2026, 9, 1)


def test_ausschnitt_zeitraum_reihenfolge_und_grenze(kommunen: tuple[OParlBody, OParlBody]) -> None:
    eigene, _ = kommunen
    alt = _vorgang(eigene, "Alt", date(2020, 1, 1))
    neu = _vorgang(eigene, "Neu", date(2026, 9, 1))
    mitte = _vorgang(eigene, "Mitte", date(2025, 6, 1))
    ohne_datum = _vorgang(eigene, "Ohne Datum", None)
    _ort(alt, 51.96, 7.62)
    _ort(neu, 51.96, 7.62)
    _ort(mitte, 51.96, 7.62)
    _ort(ohne_datum, 51.96, 7.62)
    _ort(neu, 52.50, 13.40, "Außerhalb")

    # Ohne Grenzen: alle, neueste zuerst, ohne Datum am Ende
    assert [o.title for o in ris.paper_places([eigene])] == ["Neu", "Neu", "Mitte", "Alt", "Ohne Datum"]
    # Ausschnitt (West, Süd, Ost, Nord) lässt den Punkt außerhalb weg
    im_ausschnitt = ris.paper_places([eigene], area=(7.5, 51.9, 7.7, 52.0))
    assert [o.title for o in im_ausschnitt] == ["Neu", "Mitte", "Alt", "Ohne Datum"]
    # Zeitraum: ab einem Datum, ohne Datum fällt heraus
    assert [o.title for o in ris.paper_places([eigene], area=(7.5, 51.9, 7.7, 52.0), since=date(2025, 1, 1))] == [
        "Neu",
        "Mitte",
    ]
    # Grenze: die neuesten zuerst
    assert [o.title for o in ris.paper_places([eigene], limit=2)] == ["Neu", "Neu"]
    assert ris.paper_places([eigene], limit=0) == []


def test_eine_abfrage(kommunen: tuple[OParlBody, OParlBody], django_assert_num_queries: object) -> None:
    eigene, _ = kommunen
    for nummer in range(5):
        _ort(_vorgang(eigene, f"Vorgang {nummer}", date(2026, 1, nummer + 1)), 51.9 + nummer / 100, 7.6)

    with django_assert_num_queries(1):  # type: ignore[operator]
        assert len(ris.paper_places(OParlBody.objects.filter(pk=eigene.pk))) == 5

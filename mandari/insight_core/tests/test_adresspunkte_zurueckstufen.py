# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestand: Adresspunkte von Privatanschriften auf die Straße zurückstufen, umkehrbar und ohne zu löschen (Issue #1269).
"""

from __future__ import annotations

from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command

from hub.ris import selectors as ris
from insight_core.models import OParlBody, OParlPaper, PaperLocation, Street
from insight_core.services import ortsschutz_bestand
from insight_core.services.paper_locations import location_counts, nearby_papers, sync_paper_locations

pytestmark = pytest.mark.django_db

STRASSE = (51.9500, 7.6400)
HAUS = (51.9530, 7.6450)


@pytest.fixture
def kommune(geo_body: OParlBody, make_street: Callable[..., Street]) -> OParlBody:
    make_street(geo_body, "Wolbecker Straße", *STRASSE)
    return geo_body


def _vorgang(
    body: OParlBody, make_paper: Callable[..., OParlPaper], art: str, orte: list[dict[str, Any]]
) -> OParlPaper:
    paper = make_paper(body, name="Parkverbot", paper_type=art, locations=orte)
    sync_paper_locations(paper)
    return paper


def _ort(name: str, punkt: tuple[float, float], quelle: str = "address_match", **mehr: Any) -> dict[str, Any]:
    return {"lat": punkt[0], "lon": punkt[1], "name": name, "source": quelle, "confidence": 0.9, **mehr}


def _orte(paper: OParlPaper) -> list[dict[str, Any]]:
    paper.refresh_from_db()
    return list(paper.locations or [])


def _zeilen(paper: OParlPaper) -> list[tuple[str, float, float, str, str]]:
    return list(
        PaperLocation.objects.filter(paper=paper)
        .order_by("name")
        .values_list("name", "latitude", "longitude", "source", "status")
    )


def test_probelauf_aendert_nichts(kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = _vorgang(kommune, make_paper, "Bürgerantrag", [_ort("Wolbecker Straße 12a", HAUS)])
    vorher = _zeilen(paper)
    bilanz = ortsschutz_bestand.zurueckstufen(paper)
    assert (bilanz.vorgaenge, bilanz.verschoben) == (1, 1)
    assert _zeilen(paper) == vorher
    assert _orte(paper)[0]["name"] == "Wolbecker Straße 12a"


def test_adresspunkt_wird_strasse_und_kommt_zurueck(kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = _vorgang(kommune, make_paper, "Bürgerantrag", [_ort("Wolbecker Straße 12a", HAUS, fundstelle="titel")])
    zeile = PaperLocation.objects.get(paper=paper)
    zeile.ki_urteil = PaperLocation.KI_PASST
    zeile.save(update_fields=["ki_urteil"])

    bilanz = ortsschutz_bestand.zurueckstufen(paper, ausfuehren=True)
    assert bilanz.verschoben == 1 and bilanz.ausgeblendet == 0
    paper.refresh_from_db()
    zeile.refresh_from_db()
    assert _zeilen(paper) == [("Wolbecker Straße", *STRASSE, "street_match", PaperLocation.STATUS_AUTO)]
    assert zeile.pk == PaperLocation.objects.get(paper=paper).pk  # dieselbe Zeile, nichts gelöscht
    assert zeile.ki_urteil == PaperLocation.KI_PASST
    vorher = zeile.vorher or {}
    assert vorher["name"] == "Wolbecker Straße 12a" and vorher["aktion"] == "verschoben"
    assert paper.locations == [
        {
            "lat": STRASSE[0],
            "lon": STRASSE[1],
            "name": "Wolbecker Straße",
            "source": "street_match",
            "confidence": 0.8,
            "fundstelle": "titel",
        }
    ]
    # Ein zweiter Lauf findet nichts mehr; der Abgleich JSON → Tabelle bleibt ruhig
    assert ortsschutz_bestand.zurueckstufen(paper, ausfuehren=True).vorgaenge == 0
    assert not sync_paper_locations(paper).changed

    bilanz = ortsschutz_bestand.rueckgaengig(paper, ausfuehren=True)
    assert bilanz.wiederhergestellt == 1
    paper.refresh_from_db()
    assert _zeilen(paper) == [("Wolbecker Straße 12a", *HAUS, "address_match", PaperLocation.STATUS_AUTO)]
    assert PaperLocation.objects.get(paper=paper).vorher is None
    orte = _orte(paper)
    assert orte[0]["name"] == "Wolbecker Straße 12a" and orte[0]["fundstelle"] == "titel"


def test_spaeterer_lauf_ohne_strasse_blendet_aus_statt_zu_loeschen(
    kommune: OParlBody, make_paper: Callable[..., OParlPaper]
) -> None:
    """Findet ein neuer Verortungslauf die Straße nicht mehr, bleibt die zurückgestufte Zeile umkehrbar."""
    paper = _vorgang(kommune, make_paper, "Bürgerantrag", [_ort("Wolbecker Straße 12a", HAUS)])
    ortsschutz_bestand.zurueckstufen(paper, ausfuehren=True)
    zeile = PaperLocation.objects.get(paper=paper)
    # Neuer Lauf, etwa nur noch aus dem Titel: kein Ort mehr
    paper.refresh_from_db()
    paper.locations = None
    paper.save(update_fields=["locations"])
    ergebnis = sync_paper_locations(paper)
    assert (ergebnis.deleted, ergebnis.hidden) == (0, 1)
    zeile.refresh_from_db()
    assert zeile.status == PaperLocation.STATUS_HIDDEN and (zeile.vorher or {}).get("name") == "Wolbecker Straße 12a"
    assert ris.paper_places([kommune]) == [] and location_counts([str(paper.pk)]) == {}
    # Ein weiterer Abgleich bleibt ruhig, eine Zeile ohne ``vorher`` wird wie bisher gelöscht
    assert not sync_paper_locations(paper).changed
    anderer = _vorgang(kommune, make_paper, "Beschlussvorlage", [_ort("Wolbecker Straße", STRASSE, "street_match")])
    anderer.locations = None
    anderer.save(update_fields=["locations"])
    assert sync_paper_locations(anderer).deleted == 1

    assert ortsschutz_bestand.rueckgaengig(paper, ausfuehren=True).wiederhergestellt == 1
    assert _zeilen(paper) == [("Wolbecker Straße 12a", *HAUS, "address_match", PaperLocation.STATUS_AUTO)]
    assert [o["name"] for o in _orte(paper)] == ["Wolbecker Straße 12a"]


def test_unbekannte_strasse_wird_ausgeblendet(kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = _vorgang(kommune, make_paper, "Einwohnerfrage", [_ort("Gartenpfad 3", HAUS, quelle="oparl")])
    bilanz = ortsschutz_bestand.zurueckstufen(paper, ausfuehren=True)
    assert bilanz.ausgeblendet == 1
    paper.refresh_from_db()
    zeile = PaperLocation.objects.get(paper=paper)
    # Nichts gelöscht: Zeile mit allen Angaben, nur ausgeblendet
    assert (zeile.name, zeile.latitude, zeile.status) == ("Gartenpfad 3", HAUS[0], PaperLocation.STATUS_HIDDEN)
    assert not paper.locations
    # Öffentlich nirgends: Karte, Umkreissuche, Zahl der Orte
    assert ris.paper_places([kommune]) == []
    assert nearby_papers(kommune, *HAUS, 500) == []
    assert location_counts([str(paper.pk)]) == {}
    # Der Abgleich legt den Punkt nicht wieder an, auch wenn er im JSON auftaucht
    paper.locations = [_ort("Gartenpfad 3", HAUS, quelle="oparl")]
    paper.save(update_fields=["locations"])
    assert sync_paper_locations(paper).suppressed == 1
    assert PaperLocation.objects.filter(paper=paper).count() == 1

    assert ortsschutz_bestand.rueckgaengig(paper, ausfuehren=True).wiederhergestellt == 1
    paper.refresh_from_db()
    zeile.refresh_from_db()
    assert zeile.status == PaperLocation.STATUS_AUTO
    assert [(o["name"], o["source"]) for o in _orte(paper)] == [("Gartenpfad 3", "oparl")]


def test_strasse_schon_verortet_wird_ausgeblendet(kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = _vorgang(
        kommune,
        make_paper,
        "Bürgerantrag",
        [_ort("Wolbecker Straße 12a", HAUS, quelle="oparl"), _ort("Wolbecker Straße", STRASSE, "street_match")],
    )
    bilanz = ortsschutz_bestand.zurueckstufen(paper, ausfuehren=True)
    assert (bilanz.verschoben, bilanz.ausgeblendet) == (0, 1)
    assert PaperLocation.objects.filter(paper=paper).count() == 2
    assert [o["name"] for o in _orte(paper)] == ["Wolbecker Straße"]


def test_bestaetigte_zeile_bleibt_bestaetigt(kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = _vorgang(kommune, make_paper, "Bauantrag", [_ort("Wolbecker Straße 12a", HAUS)])
    PaperLocation.objects.filter(paper=paper).update(status=PaperLocation.STATUS_CONFIRMED)
    ortsschutz_bestand.zurueckstufen(paper, ausfuehren=True)
    assert _zeilen(paper) == [("Wolbecker Straße", *STRASSE, "street_match", PaperLocation.STATUS_CONFIRMED)]
    ortsschutz_bestand.rueckgaengig(paper, ausfuehren=True)
    assert _zeilen(paper) == [("Wolbecker Straße 12a", *HAUS, "address_match", PaperLocation.STATUS_CONFIRMED)]


def test_andere_vorgaenge_und_entfernte_zeilen_bleiben(
    kommune: OParlBody, make_paper: Callable[..., OParlPaper]
) -> None:
    normal = _vorgang(kommune, make_paper, "Beschlussvorlage", [_ort("Wolbecker Straße 12a", HAUS)])
    entfernt = _vorgang(kommune, make_paper, "Bürgerantrag", [_ort("Wolbecker Straße 12a", HAUS)])
    PaperLocation.objects.filter(paper=entfernt).update(status=PaperLocation.STATUS_REMOVED)
    assert ortsschutz_bestand.zurueckstufen(normal, ausfuehren=True).vorgaenge == 0
    assert ortsschutz_bestand.zurueckstufen(entfernt, ausfuehren=True).vorgaenge == 0
    assert _zeilen(normal) == [("Wolbecker Straße 12a", *HAUS, "address_match", PaperLocation.STATUS_AUTO)]


def test_befehl_probelauf_ausfuehren_rueckgaengig(kommune: OParlBody, make_paper: Callable[..., OParlPaper]) -> None:
    paper = _vorgang(kommune, make_paper, "Bürgerantrag", [_ort("Wolbecker Straße 12a", HAUS)])
    _vorgang(kommune, make_paper, "Beschlussvorlage", [_ort("Wolbecker Straße 12a", HAUS)])

    ausgabe = StringIO()
    call_command("adresspunkte_zurueckstufen", "--body", "beispielstadt", stdout=ausgabe)
    assert "1 von 2 geprüften Vorgängen: 1 Adresspunkte auf die Straße zurückgestuft" in ausgabe.getvalue()
    assert "Probelauf" in ausgabe.getvalue()
    assert _zeilen(paper)[0][0] == "Wolbecker Straße 12a"

    call_command("adresspunkte_zurueckstufen", "--ausfuehren", stdout=StringIO())
    assert _zeilen(paper)[0][0] == "Wolbecker Straße"

    ausgabe = StringIO()
    call_command("adresspunkte_zurueckstufen", "--rueckgaengig", "--ausfuehren", stdout=ausgabe)
    assert "1 Orte wiederhergestellt" in ausgabe.getvalue()
    assert _zeilen(paper)[0][0] == "Wolbecker Straße 12a"

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verständliche Suchtreffer (Konzept Insight-Suche, P0.5–P0.7): Gruppen, Kontext, Stand-Satz, saubere Ausschnitte."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from django.template import engines
from django.test import Client
from django_cotton.compiler_regex import CottonCompiler

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
)
from insight_core.services import search_presentation as darstellung
from insight_core.services.search_service import HIGHLIGHT_POST, HIGHLIGHT_PRE, ElasticsearchService, _safe_highlight

BERLIN = ZoneInfo("Europe/Berlin")

# --- Ausschnitte säubern (P0.7) ---------------------------------------------------------------------


def test_symbolschrift_und_steuerzeichen_verschwinden() -> None:
    roh = "Ausbau der\uf0a7 nördlichen\x01 Straße\ufffd \uf0b7 Punkt \uf0e8 Ziel\uf0ff"

    assert darstellung.clean_snippet(roh) == "Ausbau der• nördlichen Straße • Punkt → Ziel"


@pytest.fixture(scope="module")
def alle_zeichen() -> str:
    """Jeder Unicode-Codepunkt außer den Surrogaten, als eine Zeichenkette."""
    return "".join(chr(codepunkt) for codepunkt in range(0x110000) if not 0xD800 <= codepunkt <= 0xDFFF)


def test_steuerzeichenmuster_trifft_genau_die_gemeinten_zeichen(alle_zeichen: str) -> None:
    """C0-Steuerzeichen ohne Tab, Zeilenumbruch und Wagenrücklauf, dazu DEL und U+FFFD – sonst nichts."""
    gemeint = {chr(c) for c in (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F, 0xFFFD)}

    assert set(darstellung._CONTROL.findall(alle_zeichen)) == gemeint


def test_symbolschriftmuster_trifft_genau_den_privatbereich(alle_zeichen: str) -> None:
    gemeint = {chr(c) for c in range(0xE000, 0xF900)}

    assert set(darstellung._PRIVATE_USE.findall(alle_zeichen)) == gemeint


@pytest.mark.parametrize("zeichen", ["\x01", "\x08", "\x0b", "\x0c", "\x0e", "\x1f", "\x7f", "�"])
def test_steuerzeichen_an_den_bereichsgrenzen_werden_leerraum(zeichen: str) -> None:
    assert darstellung.clean_snippet(f"Haushalt{zeichen}2027") == "Haushalt 2027"


def test_privatbereich_an_den_grenzen_verschwindet() -> None:
    assert darstellung.clean_snippet("Ziel und Weg") == "Ziel und Weg"


def test_gewoehnlicher_text_bleibt_unveraendert() -> None:
    ascii_druckbar = "".join(chr(c) for c in range(0x21, 0x7F))
    latin1_druckbar = "".join(chr(c) for c in range(0xA1, 0x100))
    text = f"{ascii_druckbar} {latin1_druckbar} „Straße“ – 5 € · Ölmühle → Übung 豈 \U0001f5f3"

    assert darstellung.clean_snippet(text) == text


@pytest.mark.parametrize(
    ("roh", "sauber"),
    [
        ("im fol- genden Abschnitt", "im folgenden Abschnitt"),
        ("ein Geh- und Radweg", "ein Geh- und Radweg"),
        ("Ein- oder Ausfahrt", "Ein- oder Ausfahrt"),
        ("Bus- Linie", "Bus- Linie"),  # Großbuchstabe danach: kein Trennstrich am Zeilenende
    ],
)
def test_offene_silbentrennung(roh: str, sauber: str) -> None:
    assert darstellung.clean_snippet(roh) == sauber


def test_rohtext_kann_keine_markierung_vortaeuschen() -> None:
    """``_safe_highlight`` nutzt ``\\x00`` als Platzhalter; Säubern vorher entfernt ihn aus dem Rohtext."""
    roh = f"\x00MARK_START\x00<script>{HIGHLIGHT_PRE}Witzleben{HIGHLIGHT_POST}\uf0a7"

    html = str(_safe_highlight(darstellung.clean_snippet(roh)))

    assert html.count("<mark") == 1
    assert "&lt;script&gt;" in html
    assert "\uf0a7" not in html and "\x00" not in html


# --- Art und Dokumentname (P0.6) --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("roh", "art"),
    [
        ("Vorlagen", "Vorlage"),
        ("Beschlussvorlage", "Vorlage"),
        ("Antrag an die BV Mitte", "Antrag"),
        ("Anregung des JR an die BV West", "Anregung"),
        ("Einwohnerfragen", "Einwohnerfrage"),
        ("Mitteilungsvorlage", "Mitteilung"),
        ("Sonderfall X", "Sonderfall X"),
        ("", ""),
    ],
)
def test_art_normalisiert(roh: str, art: str) -> None:
    assert darstellung.normalize_paper_type(roh) == art


@pytest.mark.parametrize(
    ("name", "datei", "anzeige"),
    [
        (None, "V-0169-2016-Anlage-2-Begruendung.pdf", "Anlage 2 – Begründung"),
        ("V-0624-2016-Anlage-1-Uebersicht", None, "Anlage 1 – Übersicht"),
        ("Begründung zum Bebauungsplan", "x.pdf", "Begründung zum Bebauungsplan"),
        (None, "Feuerwehrbedarfsplan.pdf", "Feuerwehrbedarfsplan"),
        (None, None, "Dokument"),
    ],
)
def test_dokumentname_statt_dateiname(name: str | None, datei: str | None, anzeige: str) -> None:
    assert darstellung.document_label(name, datei) == anzeige


def test_ehrliche_zahl() -> None:
    assert darstellung.count_sentence({"vorgaenge": 79, "unterlagen": 15}) == "79 Vorgänge und 15 Sitzungsunterlagen"
    assert darstellung.count_sentence({"vorgaenge": 1, "meetings": 2}) == "1 Vorgang und 2 Sitzungen"
    assert darstellung.count_sentence({"vorgaenge": 5200, "approx": True}) == "rund 5.200 Vorgänge"


# --- Gruppierung im Suchdienst (P0.5) ---------------------------------------------------------------


class _Indizes:
    def exists(self, index: str) -> bool:
        return True


class _FakeElasticsearch:
    """Treffer je Index mit Quelle; ``ids``-Abfragen liefern Dokumente, Aggregation zählt Gruppen."""

    def __init__(self, daten: dict[str, list[dict[str, Any]]]) -> None:
        self.daten = daten
        self.indices = _Indizes()
        self.aufrufe: list[tuple[str, dict[str, Any]]] = []

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.aufrufe.append((index, body))
        if "aggs" in body:
            vorgaenge = {
                d.get("paper_id") or d["id"]
                for i in ("papers", "files")
                for d in self.daten.get(i, [])
                if i == "papers" or d.get("paper_id")
            }
            unterlagen = {d.get("meeting_id") for d in self.daten.get("files", []) if not d.get("paper_id")}
            return {
                "aggregations": {
                    "vorgaenge": {"n": {"value": len(vorgaenge)}},
                    "unterlagen": {"n": {"value": len(unterlagen)}},
                }
            }
        query = body["query"]
        ids = query.get("ids", {}).get("values") or next(
            (f["ids"]["values"] for f in query.get("bool", {}).get("filter", []) if "ids" in f), None
        )
        docs = self.daten.get(index, [])
        if ids is not None:
            alle = {d["id"]: d for i in self.daten for d in self.daten[i] if i == index}
            alle.update(
                {pid: {"id": pid, "name": f"Vorgang {pid}"} for pid in ids if index == "papers" and pid not in alle}
            )
            treffer = [alle[i] for i in ids if i in alle]
        else:
            treffer = [d for d in docs if d.get("score")]
        treffer = sorted(treffer, key=lambda d: -d.get("score", 0))[: body.get("size", 10)]
        hits = []
        for d in treffer:
            quelle = {k: v for k, v in d.items() if k != "score"}
            if body.get("_source") is False:
                quelle = {}
            elif isinstance(body.get("_source"), list):
                quelle = {k: v for k, v in quelle.items() if k in body["_source"]}
            hit: dict[str, Any] = {"_id": d["id"], "_score": d.get("score", 1.0), "_source": quelle}
            if "highlight" in body and d.get("text_content"):
                hit["highlight"] = {"text_content": [d["text_content"]]}
            hits.append(hit)
        alle_treffer = [d for d in docs if d.get("score")]
        bester = max((d["score"] for d in alle_treffer), default=None)
        return {"hits": {"hits": hits, "total": {"value": len(alle_treffer)}, "max_score": bester}}

    def count(self, index: str, query: dict[str, Any], **_: Any) -> dict[str, int]:
        return {"count": 0}


def _dienst(daten: dict[str, list[dict[str, Any]]]) -> tuple[Any, _FakeElasticsearch]:
    dienst = cast(Any, ElasticsearchService).__new__(ElasticsearchService)
    dienst.client = _FakeElasticsearch(daten)
    return dienst, dienst.client


def _daten() -> dict[str, list[dict[str, Any]]]:
    # Vorgang V1 trifft selbst und mit zwei Anlagen; V2 nur über eine Anlage; eine Niederschrift ohne Vorgang
    return {
        "papers": [{"id": "V1", "score": 9.0, "name": "VBP Nr. 571"}],
        "files": [
            {"id": "f1", "score": 8.0, "paper_id": "V1", "name": "Anlage 2", "text_content": "Von-Witzleben-Straße"},
            {"id": "f2", "score": 7.0, "paper_id": "V1", "name": "Anlage 3"},
            {"id": "f3", "score": 6.0, "paper_id": "V2", "name": "Maßnahmenliste"},
            {"id": "f4", "score": 5.0, "meeting_id": "S1", "name": "Niederschrift"},
        ],
        "meetings": [],
        "persons": [{"id": "P1", "score": 3.0, "name": "Frau von Witzleben"}],
        "organizations": [],
    }


def test_dokumente_stehen_unter_ihrem_vorgang_und_nichts_doppelt() -> None:
    dienst, _client = _dienst(_daten())

    ergebnis = dienst.search_grouped("witzleben", ranking="v2")

    gruppen = ergebnis["groups"]
    assert [(g["kind"], g["key"]) for g in gruppen] == [("paper", "V1"), ("paper", "V2"), ("meeting", "S1")]
    erste = gruppen[0]
    assert erste["paper"]["id"] == "V1"
    assert erste["file"]["id"] == "f1"
    assert [d["id"] for d in erste["others"]] == ["f2"]
    # V2 trifft nur über die Anlage, der Vorgang wird trotzdem geladen
    assert gruppen[1]["paper"]["id"] == "V2"
    # Personen nicht in „Alle“, aber gezählt
    assert ergebnis["totals_by_index"]["persons"] == 1
    assert ergebnis["counts"]["vorgaenge"] == 2 and ergebnis["counts"]["unterlagen"] == 1
    assert ergebnis["has_more"] is False


def test_rangfusion_vorgang_mit_treffern_in_zwei_indexen_steht_oben() -> None:
    daten = _daten()
    daten["papers"] = [{"id": "V9", "score": 50.0, "name": "nur der Titel"}, {"id": "V2", "score": 5.0}]
    dienst, _client = _dienst(daten)

    gruppen = dienst.search_grouped("witzleben", ranking="v2")["groups"]

    # V2: Platz 2 bei Vorgängen und Platz 3 bei Dateien schlägt V9 (nur Platz 1 bei Vorgängen) und V1 (nur Dateien)
    assert [g["key"] for g in gruppen][:3] == ["V2", "V9", "V1"]


def test_eigener_typ_zeigt_personen() -> None:
    dienst, _client = _dienst(_daten())

    gruppen = dienst.search_grouped("witzleben", index_names=["persons"], ranking="v2")["groups"]

    assert [(g["kind"], g["key"]) for g in gruppen] == [("person", "P1")]


# --- Stand-Satz gebündelt und Darstellung -----------------------------------------------------------


@pytest.fixture
def vorgang(db: Any) -> OParlPaper:
    quelle = OParlSource.objects.create(name="Beispiel", url="https://ris.beispiel.example/oparl/system")
    basis = "https://ris.beispiel.example/oparl/"
    body = OParlBody.objects.create(external_id=basis + "body/1", source=quelle, name="Beispielstadt", slug="beispiel")
    paper = OParlPaper.objects.create(
        external_id=basis + "paper/1", body=body, name="VBP Nr. 571", reference="V/0624/2016", paper_type="Vorlagen"
    )
    gremium = OParlOrganization.objects.create(external_id=basis + "org/1", body=body, name="Rat")
    for nummer, (datum, ergebnis) in enumerate(
        [
            (datetime(2016, 9, 6, 17, tzinfo=BERLIN), "vertagt"),
            (datetime(2016, 9, 28, 17, tzinfo=BERLIN), "beschlossen"),
        ],
        start=1,
    ):
        sitzung = OParlMeeting.objects.create(external_id=f"{basis}meeting/{nummer}", body=body, start=datum)
        sitzung.organizations.add(gremium)
        top = OParlAgendaItem.objects.create(external_id=f"{basis}top/{nummer}", meeting=sitzung, result=ergebnis)
        OParlConsultation.objects.create(
            external_id=f"{basis}consultation/{nummer}",
            body=body,
            paper=paper,
            meeting_external_id=sitzung.external_id,
            agenda_item_external_id=top.external_id,
        )
    return paper


@pytest.mark.django_db
def test_stand_satz_fuer_alle_vorgaenge_in_einer_abfrage(vorgang: OParlPaper, django_assert_num_queries: Any) -> None:
    with django_assert_num_queries(1):
        stand = darstellung.statuses_for_papers([str(vorgang.id), "kein-uuid"])

    assert stand[str(vorgang.id)].text == "Am 28.09.2016 im Rat beschlossen."


@pytest.mark.django_db
def test_treffer_mit_kontextzeile_stand_und_sauberem_ausschnitt(vorgang: OParlPaper) -> None:
    gruppe = {
        "kind": "paper",
        "key": str(vorgang.id),
        "paper": {
            "id": str(vorgang.id),
            "name": "VBP Nr. 571",
            "reference": "V/0624/2016",
            "paper_type": "Vorlagen",
            "organization_names": ["Bezirksvertretung Münster-Mitte"],
            "date": "2016-03-01",
        },
        "file": {
            "id": "6d3f1f0a-0000-4000-8000-000000000001",
            "file_name": "V-0169-2016-Anlage-2-Begruendung.pdf",
            "_formatted": {
                "text_content": f"\uf0a7 nördliche {HIGHLIGHT_PRE}Von-Witzleben{HIGHLIGHT_POST}-Straße fol- gender"
            },
        },
        "others": [{"id": "6d3f1f0a-0000-4000-8000-000000000002", "name": "Anlage 3"}],
    }

    treffer = darstellung.present_groups([gruppe], "beispiel")[0]

    assert treffer["context"] == ["Vorlage", "V/0624/2016", "Bezirksvertretung Münster-Mitte", "28.09.2016"]
    assert treffer["status"] == "Am 28.09.2016 im Rat beschlossen."
    assert str(treffer["snippet"]).startswith("• nördliche <mark")
    assert "folgender" in str(treffer["snippet"])
    assert treffer["fundstelle"]["label"] == "Anlage 2 – Begründung"
    assert treffer["others"][0]["label"] == "Anlage 3"

    html = (
        engines["django"]
        .from_string(CottonCompiler().process('<ol><c-suche.treffer-vorgang :treffer="t" /></ol>'))
        .render({"t": treffer})
    )
    assert "Am 28.09.2016 im Rat beschlossen." in html
    assert "1 weiteres Dokument mit Treffer" in html
    assert "<details" in html and "\uf0a7" not in html


@pytest.mark.django_db
def test_suchseite_zeigt_gruppen_und_ehrliche_zahl(vorgang: OParlPaper, monkeypatch: pytest.MonkeyPatch) -> None:
    from insight_core.services import search_service

    daten = _daten()
    daten["papers"] = [{"id": str(vorgang.id), "score": 9.0, "name": "VBP Nr. 571", "paper_type": "Vorlagen"}]
    for datei in daten["files"][:2]:
        datei["paper_id"] = str(vorgang.id)
    dienst, _client = _dienst(daten)
    monkeypatch.setattr(search_service, "get_search_service", lambda: dienst)

    antwort = Client().get("/insight/suche/partials/results/", {"q": "witzleben"})

    html = antwort.content.decode()
    assert antwort.status_code == 200
    assert "2 Vorgänge und 1 Sitzungsunterlage" in html
    assert "Außerdem" in html and "1 Person" in html
    assert html.count(f"/insight/vorgaenge/{vorgang.id}/") == 1
    assert "Am 28.09.2016 im Rat beschlossen." in html

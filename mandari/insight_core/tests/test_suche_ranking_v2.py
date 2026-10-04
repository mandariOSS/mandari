# SPDX-License-Identifier: AGPL-3.0-or-later
"""Abfrage v2 der Volltextsuche (Konzept Insight-Suche, P0.2–P0.4) und Rückschalten auf v1."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from insight_core.services import search_ranking
from insight_core.services.search_service import HIGHLIGHT, ElasticsearchService


def _clauses(query: Any, key: str) -> list[dict[str, Any]]:
    """Alle Teilabfragen eines Typs (z. B. ``multi_match``) irgendwo in der Abfrage."""
    found: list[dict[str, Any]] = []
    if isinstance(query, dict):
        for k, v in query.items():
            if k == key:
                found.append(v)
            found += _clauses(v, key)
    elif isinstance(query, list):
        for v in query:
            found += _clauses(v, key)
    return found


# --- Zerlegen der Anfrage ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("eingabe", "kern", "grundwort"),
    [
        ("Von-Witzleben-Straße", "von-witzleben", ["straße"]),
        ("Witzleben Str.", "witzleben", ["straße"]),
        ("Von-Witzleben-Str.", "von-witzleben", ["straße"]),
        ("von-witzleben", "von-witzleben", []),
        ("Straße", "straße", []),
    ],
)
def test_strassen_grundwort_ist_optional(eingabe: str, kern: str, grundwort: list[str]) -> None:
    zerlegt = search_ranking.parse(eingabe)

    assert zerlegt.alternatives[0].text == kern
    assert zerlegt.optional_suffixes == grundwort


def test_kompositum_bekommt_schreibweisen_der_strasse() -> None:
    zerlegt = search_ranking.parse("Witzlebenstraße")

    lesarten = {(a.text, a.phrase) for a in zerlegt.alternatives}
    assert lesarten == {
        ("witzlebenstraße", ""),
        ("witzlebenstr", ""),
        ("", "witzleben straße"),
        ("", "witzleben str"),
    }
    assert zerlegt.name_part == "witzleben"
    assert zerlegt.name_part_alternative == search_ranking.Alternative("witzleben")
    assert search_ranking.normalize("Hafenstr.") == "hafenstraße"


def test_kurze_woerter_sind_kein_kompositum_und_gleichbedeutungen_zaehlen_wenig() -> None:
    assert search_ranking.parse("Radweg").name_part == ""
    kita = search_ranking.parse("Kita Gievenbeck")
    synonyme = [a for a in kita.alternatives if a.boost < 1]
    assert {a.text for a in synonyme} == {"kindertagesstätte gievenbeck", "kindertageseinrichtung gievenbeck"}
    assert all(a.boost == search_ranking.SYNONYM_BOOST for a in synonyme)
    # Keine Oberbegriffe mehr: „Schule“ ist keine Lesart von „Kita“
    assert not any("schule" in a.text for a in kita.alternatives)


@pytest.mark.parametrize(
    ("eingabe", "erwartet"),
    [("V/0624/2016", True), ("A-R/0055/2026", True), ("AJR-W/0001/2026", True), ("Haushalt/Finanzen", False)],
)
def test_aktenzeichen_erkennen(eingabe: str, erwartet: bool) -> None:
    assert search_ranking.is_reference(eingabe) is erwartet


# --- Abfrage je Index -------------------------------------------------------------------------------


def test_hauptklausel_und_getrennt_nach_analyzer_ohne_unschaerfe() -> None:
    abfrage = search_ranking.text_query("von-witzleben", "files")

    hauptklauseln = [m for m in _clauses(abfrage, "multi_match") if m.get("type") == "cross_fields"]
    assert hauptklauseln and all(m["operator"] == "and" for m in hauptklauseln)
    deutsch = [m for m in hauptklauseln if m.get("analyzer") == "german_custom"]
    standard = [m for m in hauptklauseln if "analyzer" not in m]
    assert {f.split("^")[0] for m in deutsch for f in m["fields"]} == {
        "name",
        "text_content",
        "paper_name",
        "organization_names",
    }
    assert {f.split("^")[0] for m in standard for f in m["fields"]} == {"file_name", "paper_reference"}
    assert "fuzziness" not in json.dumps(abfrage)
    phrasen = [m for m in _clauses(abfrage, "multi_match") if m.get("type") == "phrase"]
    assert phrasen and phrasen[0]["boost"] == search_ranking.PHRASE_BOOST


def test_aktenzeichen_exakt_auf_keyword_und_art_nicht_im_volltext() -> None:
    abfrage = search_ranking.text_query("v/0624/2016", "papers")

    terme = _clauses(abfrage, "term")
    assert terme == [{"reference.keyword": {"value": "v/0624/2016", "boost": 10.0, "case_insensitive": True}}]
    felder = {f for m in _clauses(abfrage, "multi_match") for f in m.get("fields", [])}
    assert not any(f.startswith("paper_type") for f in felder)


def test_unscharf_nur_im_rueckfall_mit_grenzen() -> None:
    rueckfall = search_ranking.fuzzy_query("Witzlebn", "papers")

    for klausel in _clauses(rueckfall, "multi_match"):
        assert klausel["fuzziness"] == "AUTO:6,10"
        assert klausel["prefix_length"] == 2
        assert klausel["operator"] == "and"


def test_aktualitaet_nur_mit_datum_und_begrenzt() -> None:
    basis = {"match_all": {}}
    mit_bonus = search_ranking.with_recency(basis, "files", 1.0)["function_score"]

    assert mit_bonus["boost_mode"] == "multiply" and mit_bonus["score_mode"] == "sum"
    grund, bonus = mit_bonus["functions"]
    assert grund == {"weight": 1}
    assert bonus["filter"] == {"exists": {"field": "meeting_date"}}
    assert bonus["weight"] == 1.0
    assert search_ranking.with_recency(basis, "persons", 1.0) == basis
    assert search_ranking.with_recency(basis, "papers", 0) == basis


def test_rueckschalten_auf_v1_liefert_die_bisherige_abfrage(settings: Any) -> None:
    settings.SEARCH_RANKING = "v1"
    dienst = cast(Any, ElasticsearchService).__new__(ElasticsearchService)

    abfrage = dienst._build_query("von-witzleben", "b1", "papers")

    assert abfrage == {
        "bool": {
            "must": [
                {
                    "multi_match": {
                        "query": "von-witzleben",
                        "fields": [
                            "name^3",
                            "reference^2",
                            "paper_type",
                            "organization_names",
                            "file_contents_preview",
                            "file_names",
                        ],
                        "type": "best_fields",
                        "fuzziness": "AUTO",
                    }
                }
            ],
            "filter": [{"term": {"body_id": "b1"}}],
        }
    }
    settings.SEARCH_RANKING = "v2"
    assert "function_score" in dienst._build_query("von-witzleben", "b1", "papers")


# --- Suchablauf mit nachgebautem Elasticsearch -------------------------------------------------------


class _Indizes:
    def __init__(self, namen: set[str]) -> None:
        self.namen = namen

    def exists(self, index: str) -> bool:
        return index in self.namen


class _FakeElasticsearch:
    """Dokumente je Index mit Relevanz, Datum, „nur unscharf“ und „Hervorhebung scheitert“."""

    def __init__(self, daten: dict[str, list[dict[str, Any]]], zaehler: tuple[int, int] = (0, 0)) -> None:
        self.daten = daten
        self.indices = _Indizes(set(daten))
        self.aufrufe: list[tuple[str, dict[str, Any]]] = []
        self.zaehler = zaehler  # (bloßer Namensteil, Schreibweisen der Straße)

    def _treffer(self, index: str, query: dict[str, Any], min_score: float = 0.0) -> list[dict[str, Any]]:
        unscharf = "fuzziness" in json.dumps(query)
        ids = [f["values"] for f in _clauses(query, "ids")]
        treffer = [d for d in self.daten[index] if unscharf or not d.get("unscharf")]
        if ids:
            treffer = [d for d in treffer if d["id"] in ids[0]]
        return [d for d in treffer if d["score"] >= min_score]

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.aufrufe.append((index, body))
        treffer = self._treffer(index, body["query"], body.get("min_score", 0.0))
        if "highlight" in body and any(d.get("gross") for d in treffer):
            raise RuntimeError("highlight: field too long")
        if "sort" in body:
            treffer.sort(key=lambda d: d.get("datum") or "", reverse=True)
        else:
            treffer.sort(key=lambda d: -d["score"])
        auswahl = treffer[body.get("from", 0) : body.get("from", 0) + body.get("size", 10)]
        hits = []
        for d in auswahl:
            hit: dict[str, Any] = {"_id": d["id"], "_score": d["score"]}
            if body.get("_source", True) is not False:
                hit["_source"] = {"id": d["id"], "name": d["id"]}
            if "sort" in body:
                datum = d.get("datum")
                hit["sort"] = [datetime.fromisoformat(datum).replace(tzinfo=UTC).timestamp() * 1000 if datum else None]
            if "highlight" in body:
                hit["highlight"] = {"name": [f"<mark>{d['id']}</mark>"]}
            hits.append(hit)
        bester = max((d["score"] for d in treffer), default=None)
        return {"hits": {"hits": hits, "total": {"value": len(treffer), "relation": "eq"}, "max_score": bester}}

    def count(self, index: str, query: dict[str, Any], min_score: float = 0.0, **_: Any) -> dict[str, int]:
        self.aufrufe.append((index, {"count": query}))
        if "," in index:  # Seltenheit des Namensteils über papers,files
            return {"count": self.zaehler[1] if _clauses(query, "should") else self.zaehler[0]}
        return {"count": len(self._treffer(index, query, min_score))}


def _dienst(daten: dict[str, list[dict[str, Any]]], **kwargs: Any) -> tuple[Any, _FakeElasticsearch]:
    dienst = cast(Any, ElasticsearchService).__new__(ElasticsearchService)
    dienst.client = _FakeElasticsearch(daten, **kwargs)
    return dienst, dienst.client


def test_unscharfer_rueckfall_nur_bei_weniger_als_drei_genauen_treffern() -> None:
    dokumente = [{"id": "genau", "score": 5.0}] + [{"id": f"u{n}", "score": 1.0, "unscharf": True} for n in range(4)]
    dienst, _client = _dienst({"papers": dokumente})

    ergebnis = dienst.search_all("Witzlebn", index_names=["papers"], ranking="v2")
    assert ergebnis["similar_spelling"] is True
    assert ergebnis["total"] == 5

    genug = [{"id": f"g{n}", "score": 5.0} for n in range(3)] + dokumente[1:]
    dienst, _client = _dienst({"papers": genug})
    ergebnis = dienst.search_all("Witzleben", index_names=["papers"], ranking="v2")
    assert ergebnis["similar_spelling"] is False
    assert ergebnis["total"] == 3

    # v1 sucht immer unscharf und kennt keinen Rückfall
    dienst, client = _dienst({"papers": dokumente})
    assert dienst.search_all("Witzlebn", index_names=["papers"], ranking="v1")["similar_spelling"] is False


def test_mindestrelevanz_laesst_randtreffer_weg(settings: Any) -> None:
    settings.SEARCH_MIN_RELEVANCE = 0.1
    dokumente = [{"id": f"p{n}", "score": s} for n, s in enumerate([10.0, 5.0, 1.0, 0.9, 0.1])]
    dienst, _client = _dienst({"papers": dokumente})

    ergebnis = dienst.search_all("Haushalt", index_names=["papers"], ranking="v2")

    assert [d["id"] for d in ergebnis["results"]] == ["p0", "p1", "p2"]
    assert ergebnis["total"] == 3
    assert dienst.search_all("Haushalt", index_names=["papers"], ranking="v1")["total"] == 5


def test_neueste_zuerst_ueber_die_treffer_ab_mindestrelevanz(settings: Any) -> None:
    settings.SEARCH_MIN_RELEVANCE = 0.1
    papers = [
        {"id": "alt-stark", "score": 10.0, "datum": "2016-09-28"},
        {"id": "neu", "score": 4.0, "datum": "2026-08-13"},
        {"id": "neu-rand", "score": 0.5, "datum": "2026-09-30"},
    ]
    files = [{"id": "datei-mitte", "score": 8.0, "datum": "2024-09-24"}, {"id": "ohne-datum", "score": 9.0}]
    dienst, client = _dienst({"papers": papers, "files": files})

    ergebnis = dienst.search_all("Witzleben", index_names=["papers", "files"], sort="newest", ranking="v2")

    assert [d["id"] for d in ergebnis["results"]] == ["neu", "datei-mitte", "alt-stark", "ohne-datum"]
    sortiert = [body for _index, body in client.aufrufe if "sort" in body]
    assert sortiert and all(
        body["min_score"] == pytest.approx(1.0) or body["min_score"] == pytest.approx(0.9) for body in sortiert
    )


def test_seite_bleibt_gefuellt_wenn_die_hervorhebung_scheitert() -> None:
    """Darmstadt „Haushalt“: Eine Datei über 1 Mio. Zeichen leerte früher die ganze Seite (Z8)."""
    dateien = [{"id": f"f{n}", "score": 10.0 - n, "gross": n == 3} for n in range(5)]
    dienst, _client = _dienst({"files": dateien})

    ergebnis = dienst.search_all("Haushalt", index_names=["files"], ranking="v2")

    assert [d["id"] for d in ergebnis["results"]] == ["f0", "f1", "f2", "f3", "f4"]
    assert "_formatted" not in ergebnis["results"][0]
    assert 0 < HIGHLIGHT["max_analyzed_offset"] < 1_000_000


@pytest.mark.parametrize(
    ("zaehler", "mit_namensteil"),
    [((146, 148), True), ((139, 9), False), ((1587, 1500), False), ((0, 0), False)],
)
def test_namensteil_nur_wenn_selten_und_meist_als_strasse(zaehler: tuple[int, int], mit_namensteil: bool) -> None:
    dienst, client = _dienst({"papers": [{"id": "p", "score": 1.0}] * 3}, zaehler=zaehler)

    dienst.search_all("Witzlebenstraße", body_id="b1", index_names=["papers"], ranking="v2")

    abfrage = next(body["query"] for index, body in client.aufrufe if index == "papers" and "query" in body)
    lesarten = [m["query"] for m in _clauses(abfrage, "multi_match") if m.get("type") == "cross_fields"]
    assert ("witzleben" in lesarten) is mit_namensteil

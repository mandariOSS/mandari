# SPDX-License-Identifier: AGPL-3.0-or-later
"""Volltextsuche: Blättern über alle Seiten, korrekt gemischt über mehrere Indexe."""

from __future__ import annotations

from typing import Any, cast

import pytest

from insight_core.services.search_service import MAX_RESULT_DEPTH, ElasticsearchService


class _Indizes:
    def __init__(self, namen: set[str]) -> None:
        self.namen = namen

    def exists(self, index: str) -> bool:
        return index in self.namen


class _FakeElasticsearch:
    """Nachbau der genutzten Client-Aufrufe: ``from``/``size``, ``_source``, ``ids``-Filter, Hervorhebung."""

    def __init__(self, daten: dict[str, list[tuple[str, float]]]) -> None:
        # je Index: (Dokument-ID, Relevanz)
        self.daten = daten
        self.indices = _Indizes(set(daten))
        self.aufrufe: list[tuple[str, dict[str, Any]]] = []

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.aufrufe.append((index, body))
        treffer = sorted(self.daten[index], key=lambda t: -t[1])
        filter_ids = [
            f["ids"]["values"]
            for f in body["query"].get("bool", {}).get("filter", [])
            if isinstance(f, dict) and "ids" in f
        ]
        if filter_ids:
            treffer = [t for t in treffer if t[0] in filter_ids[0]]
        anfang = body.get("from", 0)
        auswahl = treffer[anfang : anfang + body.get("size", 10)]
        hits = []
        for doc_id, score in auswahl:
            hit: dict[str, Any] = {"_id": doc_id, "_score": score}
            if body.get("_source", True) is not False:
                hit["_source"] = {"id": doc_id, "name": f"Dokument {doc_id}"}
            if "highlight" in body:
                hit["highlight"] = {"name": [f"<mark>Dokument</mark> {doc_id}"]}
            hits.append(hit)
        bester = max((score for _doc_id, score in self.daten[index]), default=None)
        gesamt = len(self.daten[index])
        return {"hits": {"hits": hits, "total": {"value": gesamt, "relation": "eq"}, "max_score": bester}}

    def count(self, index: str, query: dict[str, Any], min_score: float = 0.0, **_: Any) -> dict[str, int]:
        self.aufrufe.append((index, {"count": query, "min_score": min_score}))
        return {"count": sum(1 for _doc_id, score in self.daten[index] if score >= min_score)}


def _dienst(daten: dict[str, list[tuple[str, float]]]) -> tuple[ElasticsearchService, _FakeElasticsearch]:
    dienst = cast(Any, ElasticsearchService).__new__(ElasticsearchService)
    client = _FakeElasticsearch(daten)
    dienst.client = client
    return dienst, client


def _vorgaenge(anzahl: int) -> list[tuple[str, float]]:
    return [(f"p{n:03d}", 1000.0 - n) for n in range(anzahl)]


@pytest.mark.parametrize("seite", [1, 2, 3, 5])
def test_jede_seite_eines_index_ist_gefuellt(seite: int) -> None:
    dienst, _client = _dienst({"papers": _vorgaenge(100)})

    ergebnis = dienst.search_all("Radweg", page=seite, page_size=20, index_names=["papers"])

    erwartet = [f"p{n:03d}" for n in range((seite - 1) * 20, seite * 20)]
    assert [doc["id"] for doc in ergebnis["results"]] == erwartet
    assert ergebnis["total"] == 100
    assert ergebnis["pages"] == 5


def test_seite_ist_ueber_alle_indexe_korrekt_gemischt() -> None:
    papers = [(f"p{n}", float(100 - 2 * n)) for n in range(30)]  # 100, 98, 96, …
    meetings = [(f"m{n}", float(99 - 2 * n)) for n in range(30)]  # 99, 97, 95, …
    dienst, _client = _dienst({"papers": papers, "meetings": meetings})

    ergebnis = dienst.search_all("Rat", page=3, page_size=10, index_names=["meetings", "papers"])

    alle = sorted(papers + meetings, key=lambda t: -t[1])
    assert [doc["id"] for doc in ergebnis["results"]] == [doc_id for doc_id, _ in alle[20:30]]
    assert [doc["_index"] for doc in ergebnis["results"]][:2] == ["papers", "meetings"]
    assert all(doc["_formatted"]["name"].startswith("<mark>") for doc in ergebnis["results"])


def test_inhalte_nur_fuer_die_angezeigte_seite() -> None:
    """Schritt 1 lädt keine Dokumentinhalte; nur die Treffer der Seite kommen mit Hervorhebung."""
    dienst, client = _dienst({"papers": _vorgaenge(100), "files": _vorgaenge(100)})

    dienst.search_all("Radweg", page=2, page_size=20, index_names=["papers", "files"])

    rangfolge = [body for _index, body in client.aufrufe if "highlight" not in body and "count" not in body]
    inhalte = [body for _index, body in client.aufrufe if "highlight" in body]
    assert all(body["_source"] is False and body["size"] == 40 for body in rangfolge)
    assert sum(body["size"] for body in inhalte) == 20


def test_suchtiefe_ist_begrenzt(settings: Any) -> None:
    settings.SEARCH_MIN_RELEVANCE = 0  # die künstlichen Werte fallen bis ins Negative; hier zählt nur die Tiefe
    dienst, client = _dienst({"papers": _vorgaenge(3000)})
    letzte_seite = MAX_RESULT_DEPTH // 20

    ergebnis = dienst.search_all("Radweg", page=letzte_seite, page_size=20, index_names=["papers"])
    assert ergebnis["pages"] == letzte_seite
    assert len(ergebnis["results"]) == 20

    client.aufrufe.clear()
    dahinter = dienst.search_all("Radweg", page=letzte_seite + 1, page_size=20, index_names=["papers"])
    assert dahinter["results"] == []
    assert dahinter["total"] == 3000
    assert all(body.get("size", 0) == 0 for _index, body in client.aufrufe)

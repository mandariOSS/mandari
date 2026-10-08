# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame Suchparameter für Work und Bürgerportal und die Gewichtung nach Bezug (Issue #853).

- ``SearchQuery`` versteht neue und alte Schreibweisen (Work: ``von``, ``bis``, ``gremium``, ``art``, ``typ``, ``seite``,
  ``kommune``); ein eigener Zeitraum gilt nur mit ``period=frei``.
- ``RelationBoost`` ändert die Abfrage nur um Faktoren; ohne Bezug bleibt sie gleich (gleiche Reihenfolge wie im
  Bürgerportal).
- ``present_groups`` bildet die Adressen über ``links`` ab und nennt Kennung und Gremien je Treffer.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from django.http import QueryDict

from insight_core.services import search_ranking
from insight_core.services.search_filters import SearchQuery
from insight_core.services.search_presentation import present_groups
from insight_core.services.search_ranking import RelationBoost
from insight_core.services.search_service import ElasticsearchService


def test_alte_work_links_und_neue_parameter() -> None:
    alt = SearchQuery.from_get(
        QueryDict(
            "q=Radweg&von=2025-01-01&bis=2025-12-31&gremium=Rat&art=Antrag%20an%20den%20Rat&typ=papers&seite=3&kommune=k1"
        )
    )

    assert (alt.period, alt.date_from, alt.date_to, alt.committee, alt.paper_type) == (
        "frei",
        "2025-01-01",
        "2025-12-31",
        "Rat",
        "Antrag an den Rat",
    )
    assert (alt.result_type, alt.page, alt.body_filter) == ("papers", 3, "k1")
    assert alt.date_range(date(2026, 10, 6)) == ("2025-01-01", "2025-12-31")
    assert alt.url() == (
        "?q=Radweg&result_type=papers&period=frei&von=2025-01-01&bis=2025-12-31"
        "&art=Antrag+an+den+Rat&gremium=Rat&kommune=k1"
    )
    # Eine Voreinstellung ersetzt den eigenen Zeitraum, auch in der Adresse
    assert (
        alt.url(period="12m") == "?q=Radweg&result_type=papers&period=12m&art=Antrag+an+den+Rat&gremium=Rat&kommune=k1"
    )

    neu = SearchQuery.from_get(
        QueryDict("q=Kita&result_type=meetings&period=2y&von=2020-01-01&paper_type=Antrag&sort=newest&page=2")
    )
    assert (neu.period, neu.date_from, neu.paper_types, neu.sort, neu.page) == ("2y", "", ["Antrag"], "newest", 2)
    assert neu.date_range(date(2026, 10, 6)) == ("2024-10-06", None)

    unsinn = SearchQuery.from_get(QueryDict("q=x&von=gestern&typ=geheim&period=99y&sort=zufall&seite=abc"))
    assert (unsinn.date_from, unsinn.result_type, unsinn.period, unsinn.sort, unsinn.page) == (
        "",
        "",
        "",
        "relevance",
        1,
    )
    # „Eigener Zeitraum“ ohne Datum ist kein Filter
    assert not SearchQuery.from_get(QueryDict("q=&period=frei")).has_filters
    assert SearchQuery.from_get(QueryDict("q=&gremium=Rat")).has_filters


def _query(index: str, boost: RelationBoost | None) -> dict[str, Any]:
    dienst = ElasticsearchService.__new__(ElasticsearchService)
    return dienst._build_query("Radweg", None, index, body_ids=["b1"], ranking="v2", boost=boost)


def test_ohne_bezug_dieselbe_abfrage_wie_im_buergerportal() -> None:
    for index in ("papers", "files", "meetings", "persons", "organizations"):
        assert _query(index, None) == _query(index, RelationBoost())
        assert _query(index, None) == _query(index, RelationBoost(papers=((frozenset(), 1.5),), committees=()))


def test_bezug_als_faktoren_ohne_filter() -> None:
    boost = RelationBoost(
        papers=((frozenset({"p1"}), 1.5), (frozenset({"p2", "p3"}), 1.3)),
        meetings=((frozenset({"m1"}), 1.2),),
        committees=("Bauausschuss",),
        committee_factor=1.15,
    )

    papers = _query("papers", boost)["function_score"]
    assert papers["query"] == _query("papers", None) and papers["score_mode"] == "multiply"
    assert [f["filter"] for f in papers["functions"]] == [
        {"ids": {"values": ["p1"]}},
        {"ids": {"values": ["p2", "p3"]}},
        {"bool": {"should": [{"match_phrase": {"organization_names": "Bauausschuss"}}], "minimum_should_match": 1}},
    ]
    assert [f["weight"] for f in papers["functions"]] == [1.5, 1.3, 1.15]
    files = _query("files", boost)["function_score"]["functions"]
    assert files[0]["filter"] == {"terms": {"paper_id": ["p1"]}} and files[2]["filter"] == {
        "terms": {"meeting_id": ["m1"]}
    }
    assert _query("meetings", boost)["function_score"]["functions"][0]["filter"] == {"ids": {"values": ["m1"]}}
    # Personen und Gremien haben keinen Bezug: Abfrage unverändert
    assert _query("persons", boost) == _query("persons", None)
    assert search_ranking.with_relation({"match_all": {}}, "papers", None) == {"match_all": {}}


@pytest.mark.django_db
def test_treffer_mit_eigenen_adressen_kennung_und_gremien() -> None:
    gruppen: list[dict[str, Any]] = [
        {
            "kind": "paper",
            "key": "6d3f1f0a-0000-4000-8000-000000000001",
            "paper": {
                "id": "6d3f1f0a-0000-4000-8000-000000000001",
                "name": "Radweg",
                "reference": "V/1",
                "paper_type": "Beschlussvorlage",
                "organization_names": ["Bauausschuss"],
            },
            "file": {"id": "6d3f1f0a-0000-4000-8000-0000000000f1", "name": "Anlage", "organization_names": ["Rat"]},
            "others": [],
        },
        {"kind": "meeting", "key": "m1", "meeting_id": "m1", "meeting": {"id": "m1", "name": "Rat"}, "file": None},
    ]

    insight = present_groups(gruppen)
    assert insight[0]["url"] == "/insight/vorgaenge/6d3f1f0a-0000-4000-8000-000000000001/"

    work = present_groups(gruppen, links=lambda art, pk: f"/work/x/{art}/{pk}/")
    assert [t["url"] for t in work] == ["/work/x/vorgang/6d3f1f0a-0000-4000-8000-000000000001/", "/work/x/sitzung/m1/"]
    assert (work[0]["pk"], work[0]["gremien"]) == ("6d3f1f0a-0000-4000-8000-000000000001", ["Bauausschuss", "Rat"])
    # Dokumente öffnen weiter über die öffentliche Vorschau
    assert work[0]["fundstelle"]["url"] == "/insight/dokumente/6d3f1f0a-0000-4000-8000-0000000000f1/preview/"


# --- Ein Kontextaufbau für Bürgerportal und Work ------------------------------------------------------------


def test_buergerportal_liest_nur_seine_parameter() -> None:
    """Dieselbe Klasse wie in Work; die Filter aus Work bleiben im Bürgerportal leer, wie bisher."""
    portal = SearchQuery.from_get(
        QueryDict("q=Kita&gremium=Rat&art=Antrag&kommune=k1&von=2025-01-01&bis=2025-02-01&typ=papers&seite=3&ort=aus"),
        portal=True,
    )

    assert (portal.committee, portal.paper_type, portal.body_filter, portal.date_from, portal.date_to) == (
        "",
        "",
        "",
        "",
        "",
    )
    assert (portal.period, portal.result_type, portal.page, portal.ort) == ("", "", 1, False)
    assert portal.url() == "?q=Kita&ort=aus" and not portal.has_filters
    assert SearchQuery.from_get(QueryDict("q=x&period=frei"), portal=True).period == ""
    assert SearchQuery.from_get(QueryDict("q=x&type=paper&page=2"), portal=True).url(page=2) == (
        "?q=x&result_type=papers&page=2"
    )


class _Aufzeichnung:
    """Suchdienst, der nur die Aufrufe festhält (eine Gruppe ohne Vorgang, damit kein Datenbankzugriff nötig ist)."""

    def __init__(self, treffer: bool = True) -> None:
        self.treffer = treffer
        self.facetten: list[dict[str, Any]] = []
        self.suchen: list[dict[str, Any]] = []

    def facet_counts(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.facetten.append(kwargs)
        return {"paper_types": {"Antrag": 2}, "periods": {"12m": 1}}

    def search_grouped(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.suchen.append(kwargs)
        gruppen = [{"kind": "person", "key": "p1", "person": {"id": "p1", "name": "Person"}, "others": []}]
        return {
            "groups": gruppen if self.treffer and kwargs.get("page_size", 20) > 1 else [],
            "counts": {"vorgaenge": 0, "unterlagen": 0, "meetings": 0},
            "totals_by_index": {"persons": 1},
            "similar_spelling": False,
            "has_more": False,
        }


def test_ohne_work_angaben_dieselben_aufrufe_wie_im_buergerportal() -> None:
    from insight_core.services.search_page import build_context

    dienst = _Aufzeichnung()
    params = SearchQuery.from_get(QueryDict("q=Kita&period=2y"), portal=True)

    kontext = build_context(dienst, params, None, ["b1"], date(2026, 10, 8))

    assert set(dienst.facetten[0]) == {"date_from", "date_to", "body_id", "body_ids"}
    assert set(dienst.suchen[0]) == {
        "page",
        "page_size",
        "date_from",
        "date_to",
        "paper_type",
        "sort",
        "weights",
        "kinds",
        "file_paper_filter",
        "body_id",
        "body_ids",
    }
    assert [o["value"] for o in kontext["period_options"]] == ["", "12m", "2y", "5y", "older"]
    assert kontext["groups"][0]["url"] == "/insight/personen/p1/"
    assert kontext["active_filters"] == [{"label": "Zeitraum: Letzte 2 Jahre", "url": "?q=Kita"}]


def test_work_angaben_wirken_im_selben_kontextaufbau() -> None:
    from insight_core.services.search_page import build_context

    dienst = _Aufzeichnung(treffer=False)
    boost = RelationBoost(papers=((frozenset({"p9"}), 1.5),))
    params = SearchQuery.from_get(QueryDict("q=Kita&von=2025-01-01&gremium=Rat&art=Antrag&kommune=k1"))

    kontext = build_context(
        dienst,
        params,
        None,
        ["k1"],
        date(2026, 10, 8),
        links=lambda art, pk: f"/work/x/{art}/{pk}/",
        boost=boost,
        custom_period=True,
    )

    assert dienst.facetten[0]["boost"] is boost and dienst.facetten[0]["organization_name"] == "Rat"
    suche, ohne = dienst.suchen
    assert (suche["boost"], suche["organization_name"], suche["index_names"]) == (
        boost,
        "Rat",
        ["papers", "meetings", "files"],
    )
    assert suche["paper_type"] == ["Antrag"] and suche["kinds"] == {"paper"}
    # „Ohne Filter“ sucht im selben Bereich (Kommune, Bezug), aber ohne Filter des Inhalts
    assert "organization_name" not in ohne and ohne["boost"] is boost and ohne["body_ids"] == ["k1"]
    assert kontext["without_filters_url"] == "?q=Kita&kommune=k1"
    assert kontext["period_options"][-1]["label"] == "Eigener Zeitraum" and kontext["period_options"][-1]["checked"]
    assert [f["label"] for f in kontext["active_filters"]] == [
        "Zeitraum: 01.01.2025 bis heute",
        "Art: Antrag",
        "Gremium: Rat",
    ]

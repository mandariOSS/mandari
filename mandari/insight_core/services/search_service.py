# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Elasticsearch Service für Django.

Bietet Volltextsuche über Elasticsearch für alle OParl-Entitäten.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError

from . import search_ranking
from .search_presentation import clean_snippet
from .search_ranking import DATE_FIELD_BY_INDEX, RelationBoost

HIGHLIGHT_PRE = '<mark class="bg-yellow-200 dark:bg-yellow-800">'
HIGHLIGHT_POST = "</mark>"

logger = logging.getLogger(__name__)

# Index names (müssen mit apps/api/src/search/service.py übereinstimmen)
INDEX_MEETINGS = "meetings"
INDEX_PAPERS = "papers"
INDEX_PERSONS = "persons"
INDEX_ORGANIZATIONS = "organizations"
INDEX_FILES = "files"

ALL_INDEXES = [INDEX_MEETINGS, INDEX_PAPERS, INDEX_PERSONS, INDEX_ORGANIZATIONS, INDEX_FILES]

# Suchtiefe: So viele Treffer lassen sich über alle Seiten hinweg abrufen (bei 20 je Seite
# 50 Seiten). Für eine korrekt gemischte Seite p braucht jeder Index seine besten
# p·page_size Treffer; tiefer blättern wir nicht, weil der Aufwand mit der Seite wächst
# (Elasticsearch selbst erlaubt höchstens 10.000, index.max_result_window).
MAX_RESULT_DEPTH = 1000

#: Unter so vielen genauen Treffern läuft in v2 die unscharfe Rückfallsuche (ähnliche Schreibweisen)
FUZZY_FALLBACK_BELOW = 3

#: Elasticsearch hebt höchstens 1.000.000 Zeichen je Feld hervor (index.highlight.max_analyzed_offset); mit
#: diesem Wert hört die Hervorhebung davor auf, statt die ganze Abfrage scheitern zu lassen
HIGHLIGHT_MAX_ANALYZED_OFFSET = 999_999

HIGHLIGHT = {
    "pre_tags": [HIGHLIGHT_PRE],
    "post_tags": [HIGHLIGHT_POST],
    "max_analyzed_offset": HIGHLIGHT_MAX_ANALYZED_OFFSET,
    "fields": {
        "name": {"number_of_fragments": 0},
        "text_content": {"fragment_size": 200, "number_of_fragments": 1},
        "reference": {"number_of_fragments": 0},
        # Ausschnitt für Vorgänge (Text ihrer Dokumente, gekürzt im Index)
        "file_contents_preview": {"fragment_size": 200, "number_of_fragments": 1},
    },
}

#: Rangfusion (Reciprocal Rank Fusion): Konstante und Gewicht je Index; Personen und Gremien nur im eigenen Typ
RRF_K = 60
RRF_WEIGHTS = {"papers": 1.0, "files": 0.9, "meetings": 0.6, "persons": 0.0, "organizations": 0.0}
#: Dateien je Seite tiefer abrufen, damit genug Vorgänge zusammenkommen (höchstens MAX_RESULT_DEPTH)
FILES_DEPTH_FACTOR = 5
#: Dokumente je Gruppe, deren Namen die Liste nennt
MAX_DOCS_PER_GROUP = 10
FILE_LABEL_FIELDS = ["id", "name", "file_name"]
#: Bis zu dieser Zahl ist ``cardinality`` praktisch genau; darüber zeigt die Seite „rund“
GROUP_COUNT_PRECISION = 3000
#: Gruppe eines Treffers: Vorgang, sonst Sitzung, sonst das Dokument selbst (``papers`` hat kein ``paper_id``)
GROUP_KEY_SCRIPT = (
    "if (doc.containsKey('paper_id') && doc['paper_id'].size() > 0) { emit(doc['paper_id'].value); }"
    " else if (doc.containsKey('meeting_id') && doc['meeting_id'].size() > 0)"
    " { emit('meeting:' + doc['meeting_id'].value); }"
    " else { emit(doc['id'].value); }"
)

SORT_RELEVANCE = "relevance"
SORT_NEWEST = "newest"
RANKING_VERSIONS = ("v1", "v2")


def ranking_version(override: str | None = None) -> str:
    """Wirksame Abfrageversion: ``override`` (Messbefehl), sonst ``SEARCH_RANKING`` (Standard v2)."""
    version = (override or getattr(settings, "SEARCH_RANKING", "v2") or "v2").strip().lower()
    return version if version in RANKING_VERSIONS else "v2"


@dataclass
class RankedHits:
    """Ergebnis von Schritt 1: Rangfolge über die Indexe ohne Dokumentinhalte."""

    #: (Relevanz, Reihenfolge des Index, Index, Dokument-ID) in der Reihenfolge der Trefferliste
    entries: list[tuple[float, int, str, str]] = field(default_factory=list)
    queries: dict[str, dict[str, Any]] = field(default_factory=dict)
    total: int = 0
    #: Treffer stammen aus der unscharfen Rückfallsuche (Hinweis „ähnliche Schreibweisen“)
    similar_spelling: bool = False
    errors: int = 0
    #: Treffer je Index (``_id``, ``_score``, ggf. ``_source`` und ``sort``) in der Reihenfolge des Index
    hits_by_index: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    totals_by_index: dict[str, int] = field(default_factory=dict)
    #: Schwelle der Mindestrelevanz je Index (0 = keine)
    thresholds: dict[str, float] = field(default_factory=dict)
    #: Indexe, hinter deren Suchtiefe weitere Treffer liegen
    truncated: set[str] = field(default_factory=set)


class ElasticsearchService:
    """Service für Elasticsearch-Integration in Django."""

    def __init__(self):
        """Initialisiert den Elasticsearch-Client."""
        self.client = Elasticsearch(settings.ELASTICSEARCH_URL)

    def is_healthy(self) -> bool:
        """Prüft ob Elasticsearch verfügbar ist."""
        try:
            return self.client.ping()
        except Exception as e:
            logger.warning(f"Elasticsearch health check fehlgeschlagen: {e}")
            return False

    def search_all(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
        index_names: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        organization_name: str | None = None,
        paper_type: str | None = None,
        body_ids: list[str] | None = None,
        sort: str = SORT_RELEVANCE,
        ranking: str | None = None,
    ) -> dict[str, Any]:
        """
        Multi-Index-Suche über alle Entitäten.

        Args:
            query: Suchbegriff
            body_id: Filter nach Kommune (UUID als String)
            body_ids: Filter nach mehreren Kommunen (terms-Query, z.B. alle
                       Kommunen einer Work-Organisation); hat Vorrang vor body_id
            page: Seitennummer (1-indiziert)
            page_size: Ergebnisse pro Seite
            index_names: Zu durchsuchende Indexe (Standard: alle)
            date_from: Zeitraum-Filter ab (ISO-Datum, je nach Index auf
                       date/start/meeting_date angewendet)
            date_to: Zeitraum-Filter bis (ISO-Datum)
            organization_name: Gremium-Filter (exakter Name, wirkt auf
                       papers/meetings/files über organization_names)
            paper_type: Vorlagen-Art (nur papers-Index)
            sort: ``relevance`` (Standard; in v2 mit Aktualitätsbonus) oder ``newest`` (Datum absteigend,
                       in v2 nur über Treffer ab der Mindestrelevanz)
            ranking: Abfrageversion ``v1``/``v2``; Standard ``SEARCH_RANKING``

        Returns:
            Dict mit results, total, page, page_size, pages, similar_spelling

        Ablauf in zwei Schritten: Zuerst liefert jeder Index Kennung und Relevanz seiner besten
        ``page * page_size`` Treffer (ohne Dokumentinhalt); gemischt und nach Relevanz sortiert
        ergibt das die richtige Seite über alle Indexe. Danach werden nur die Dokumente dieser
        Seite samt Hervorhebung geladen. Vorher holte jeder Index fest ``2 * page_size``
        Treffer ab Position 0 – ab Seite 3 blieben Seiten leer, obwohl mehr Treffer gemeldet wurden.
        """
        page = max(1, int(page))
        start = (page - 1) * page_size
        depth = min(page * page_size, MAX_RESULT_DEPTH)

        ranked = self.rank_hits(
            query,
            body_id=body_id,
            index_names=index_names,
            depth=depth if start < depth else 0,
            date_from=date_from,
            date_to=date_to,
            organization_name=organization_name,
            paper_type=paper_type,
            body_ids=body_ids,
            sort=sort,
            ranking=ranking,
        )
        page_hits = ranked.entries[start : start + page_size]
        total_hits = ranked.total

        return {
            "results": self._load_page_documents(page_hits, ranked.queries),
            "total": total_hits,
            "page": page,
            "page_size": page_size,
            "pages": (min(total_hits, MAX_RESULT_DEPTH) + page_size - 1) // page_size if total_hits > 0 else 0,
            "similar_spelling": ranked.similar_spelling,
        }

    def rank_hits(
        self,
        query: str,
        *,
        body_id: str | None = None,
        index_names: list[str] | None = None,
        depth: int | Mapping[str, int] = MAX_RESULT_DEPTH,
        date_from: str | None = None,
        date_to: str | None = None,
        organization_name: str | None = None,
        paper_type: str | list[str] | None = None,
        body_ids: list[str] | None = None,
        sort: str = SORT_RELEVANCE,
        ranking: str | None = None,
        sources: Mapping[str, list[str]] | None = None,
        boost: RelationBoost | None = None,
    ) -> RankedHits:
        """Schritt 1: Rangfolge der besten ``depth`` Treffer je Index, gemischt; ohne Dokumentinhalte.

        ``depth`` gilt für alle Indexe oder je Index; ``sources`` nennt je Index Felder, die schon dieser
        Schritt liefert (etwa ``paper_id`` der Dateien für die Gruppierung). ``boost`` gewichtet nach dem Bezug
        einer Organisation (Work, ``search_ranking.RelationBoost``), ohne die Treffermenge zu ändern. In v2 folgt bei weniger als ``FUZZY_FALLBACK_BELOW`` genauen Treffern die unscharfe Rückfallsuche.
        """
        if index_names is None:
            index_names = ALL_INDEXES
        version = ranking_version(ranking)
        name_part_is_rare = version == "v2" and bool(query) and self._name_part_is_rare(query, body_id, body_ids)

        def build(index_name: str, fuzzy: bool) -> dict[str, Any]:
            return self._build_query(
                query,
                body_id,
                index_name,
                date_from=date_from,
                date_to=date_to,
                organization_name=organization_name,
                paper_type=paper_type,
                body_ids=body_ids,
                ranking=version,
                fuzzy=fuzzy,
                name_part_is_rare=name_part_is_rare,
                boost=boost,
            )

        depths = depth if isinstance(depth, Mapping) else dict.fromkeys(index_names, depth)
        felder = sources or {}
        ranked = self._rank(index_names, build, False, depths, felder, sort, version, bool(query))
        if version == "v2" and query and ranked.total < FUZZY_FALLBACK_BELOW and ranked.errors < len(index_names):
            unscharf = self._rank(index_names, build, True, depths, felder, sort, version, True)
            if unscharf.total > ranked.total:
                unscharf.similar_spelling = True
                ranked = unscharf

        # Elasticsearch komplett nicht erreichbar → Fehler signalisieren, damit
        # Aufrufer (views/search.py) auf die Django-Datenbanksuche zurückfallen
        # können statt still leere Ergebnisse zu zeigen.
        if index_names and ranked.errors == len(index_names):
            raise RuntimeError("Elasticsearch nicht erreichbar (alle Indexe fehlgeschlagen)")
        return ranked

    def _rank(
        self,
        index_names: list[str],
        build: Callable[[str, bool], dict[str, Any]],
        fuzzy: bool,
        depths: Mapping[str, int],
        sources: Mapping[str, list[str]],
        sort: str,
        version: str,
        has_query: bool,
    ) -> RankedHits:
        ranked = RankedHits()
        min_relevance = float(getattr(settings, "SEARCH_MIN_RELEVANCE", 0.05)) if version == "v2" and has_query else 0.0
        # (Datum fehlt?, -Datum, -Relevanz, Reihenfolge) für „Neueste“
        newest_keys: dict[tuple[str, str], tuple[int, float]] = {}

        for position, index_name in enumerate(index_names):
            try:
                # Prüfen ob Index existiert
                if not self.client.indices.exists(index=index_name):
                    continue
                es_query = build(index_name, fuzzy)
                ranked.queries[index_name] = es_query
                depth = depths.get(index_name, MAX_RESULT_DEPTH)
                hits, total, threshold = self._index_hits(
                    index_name, es_query, depth, sort, min_relevance, sources.get(index_name)
                )
                ranked.hits_by_index[index_name] = hits
                ranked.totals_by_index[index_name] = total
                ranked.thresholds[index_name] = threshold
                if total > len(hits) and len(hits) >= depth:
                    ranked.truncated.add(index_name)
                for hit in hits:
                    score = float(hit.get("_score") or 0)
                    ranked.entries.append((score, position, index_name, hit["_id"]))
                    if sort == SORT_NEWEST:
                        datum = (hit.get("sort") or [None])[0]
                        newest_keys[(index_name, hit["_id"])] = (
                            (0, -float(datum)) if isinstance(datum, int | float) else (1, 0.0)
                        )
                ranked.total += total
            except NotFoundError:
                pass
            except Exception as e:
                ranked.errors += 1
                logger.error(f"Unerwarteter Fehler bei Index '{index_name}': {e}")

        if sort == SORT_NEWEST:
            ranked.entries.sort(key=lambda e: (*newest_keys.get((e[2], e[3]), (1, 0.0)), -e[0], e[1]))
        else:
            # Nach Relevanz mischen (bei Gleichstand in der Reihenfolge der Indexe)
            ranked.entries.sort(key=lambda eintrag: (-eintrag[0], eintrag[1]))
        return ranked

    def _index_hits(
        self,
        index_name: str,
        es_query: dict[str, Any],
        depth: int,
        sort: str,
        min_relevance: float,
        source: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int, float]:
        """Treffer (``_id``, ``_score``, bei „Neueste“ ``sort``) und Gesamtzahl eines Index.

        Mit Mindestrelevanz (v2) entfallen Treffer unter ``min_relevance`` × bestem Wert des Index; die
        Gesamtzahl zählt dann nur die übrigen.
        """
        date_field = DATE_FIELD_BY_INDEX.get(index_name)
        newest = sort == SORT_NEWEST and date_field is not None
        hits: list[dict[str, Any]] = []
        total = 0
        threshold = 0.0
        if not newest or min_relevance > 0:
            # Bei „Neueste“ genügt der beste Treffer, um die Schwelle der Mindestrelevanz zu bestimmen
            result = self.client.search(
                index=index_name,
                body={"query": es_query, "size": 1 if newest else depth, "from": 0, "_source": source or False},
            )
            hits = result["hits"]["hits"]
            total = int(result["hits"]["total"]["value"])
            if min_relevance > 0 and hits:
                threshold = float(result["hits"].get("max_score") or hits[0].get("_score") or 0) * min_relevance
        if newest:
            body: dict[str, Any] = {
                "query": es_query,
                "size": depth,
                "from": 0,
                "_source": source or False,
                "sort": [{date_field: {"order": "desc", "missing": "_last", "unmapped_type": "date"}}, "_score"],
                "track_scores": True,
            }
            if threshold > 0:
                body["min_score"] = threshold
            result = self.client.search(index=index_name, body=body)
            return result["hits"]["hits"], int(result["hits"]["total"]["value"]), threshold
        if threshold <= 0:
            return hits, total, threshold
        kept = [hit for hit in hits if float(hit.get("_score") or 0) >= threshold]
        if total > len(hits) and len(kept) == len(hits):
            # Hinter der Suchtiefe liegen weitere Treffer über der Schwelle: genau zählen
            total = int(self.client.count(index=index_name, query=es_query, min_score=threshold)["count"])
        else:
            total = len(kept)
        return kept, total, threshold

    def search_grouped(
        self,
        query: str,
        *,
        body_id: str | None = None,
        body_ids: list[str] | None = None,
        page: int = 1,
        page_size: int = 20,
        index_names: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        organization_name: str | None = None,
        paper_type: str | list[str] | None = None,
        sort: str = SORT_RELEVANCE,
        ranking: str | None = None,
        file_paper_filter: Callable[[set[str]], set[str]] | None = None,
        weights: Mapping[str, float] | None = None,
        kinds: set[str] | None = None,
        boost: RelationBoost | None = None,
    ) -> dict[str, Any]:
        """Treffer nach Vorgang gruppiert (Konzept Insight-Suche, P0.5).

        ``file_paper_filter`` bekommt die Vorgänge der gefundenen Dateien und gibt die zulässigen zurück (Filter
        „Art“: Dateien tragen im Index keine Art, P0 filtert sie über ihren Vorgang nach). Dateien ohne
        zulässigen Vorgang entfallen dann; die Zahl zählt die gebildeten Gruppen. ``weights`` überschreibt die
        Gewichte der Rangfusion (Index mit 0 wird nur gezählt), ``kinds`` behält nur Gruppen dieser Arten
        (``paper``, ``meeting``, ``file``, ``person``, ``organization``) – so zählt ein Aufruf für alle Reiter.
        ``boost`` gewichtet nach dem Bezug einer Organisation (Work); die Treffermenge bleibt dieselbe.

        Dateien mit Vorgang stehen unter ihm, auch wenn der Vorgang selbst nicht trifft; Unterlagen ohne
        Vorgang unter ihrer Sitzung. Die Reihenfolge entsteht per Rangfusion über die Indexe (Reciprocal Rank
        Fusion, ``RRF_K``): Je Index zählt der beste Platz der Gruppe, gewichtet nach Typ. Personen und
        Gremien erscheinen in „Alle“ nicht (nur gezählt in ``totals_by_index``), im eigenen Typ schon.
        „Neueste“ ordnet die Gruppen nach ihrem neuesten Treffer.

        Returns:
            Dict mit ``groups`` (Rohdaten je Gruppe für ``search_presentation.present_groups``), ``counts``
            (Vorgänge, Sitzungsunterlagen, Sitzungen; ``approx`` über der Genauigkeitsgrenze), ``page``,
            ``pages``, ``has_more``, ``similar_spelling`` und ``totals_by_index``.
        """
        indexes = list(index_names or ALL_INDEXES)
        page = max(1, int(page))
        want = page * page_size
        if weights is None:
            weights = dict.fromkeys(indexes, 1.0) if len(indexes) == 1 else RRF_WEIGHTS
        depths = {
            index: min(want * (FILES_DEPTH_FACTOR if index == INDEX_FILES else 1), MAX_RESULT_DEPTH)
            if weights.get(index, 0) > 0
            else 0
            for index in indexes
        }
        ranked = self.rank_hits(
            query,
            body_id=body_id,
            body_ids=body_ids,
            index_names=indexes,
            depth=depths,
            date_from=date_from,
            date_to=date_to,
            organization_name=organization_name,
            paper_type=paper_type,
            sort=sort,
            ranking=ranking,
            sources={INDEX_FILES: ["paper_id", "meeting_id"]},
            boost=boost,
        )
        if file_paper_filter is not None and INDEX_FILES in ranked.hits_by_index:
            files = ranked.hits_by_index[INDEX_FILES]
            erlaubt = file_paper_filter({str((h.get("_source") or {}).get("paper_id")) for h in files} - {"None"})
            ranked.hits_by_index[INDEX_FILES] = [
                h for h in files if str((h.get("_source") or {}).get("paper_id")) in erlaubt
            ]
        groups = self._groups(ranked, indexes, weights, sort)
        if kinds is not None:
            groups = [group for group in groups if group["kind"] in kinds]
        page_groups = groups[(page - 1) * page_size : want]
        has_more = len(groups) > want or any(
            index in ranked.truncated and weights.get(index, 0) > 0 for index in indexes
        )
        if file_paper_filter is not None:
            counts = {
                "vorgaenge": sum(1 for g in groups if g["kind"] == "paper"),
                "unterlagen": 0,
                "meetings": ranked.totals_by_index.get(INDEX_MEETINGS, 0),
                "persons": ranked.totals_by_index.get(INDEX_PERSONS, 0),
                "organizations": ranked.totals_by_index.get(INDEX_ORGANIZATIONS, 0),
                "approx": has_more,
            }
        else:
            counts = self._group_counts(ranked)
        return {
            "groups": self._load_groups(page_groups, ranked.queries),
            "counts": counts,
            "page": page,
            "page_size": page_size,
            "pages": page + 1 if has_more else page,
            "has_more": has_more,
            "similar_spelling": ranked.similar_spelling,
            "totals_by_index": dict(ranked.totals_by_index),
        }

    def facet_counts(
        self,
        query: str,
        *,
        body_id: str | None = None,
        body_ids: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        ranking: str | None = None,
        today: str = "now/d",
        boost: RelationBoost | None = None,
        organization_name: str | None = None,
    ) -> dict[str, Any]:
        """Zähler für die Filter „Art“ (Originalwerte von ``paper_type``) und „Zeitraum“ über die Vorgänge.

        Ohne den Art-Filter selbst, damit die anderen Arten wählbar bleiben; mit derselben Mindestrelevanz wie
        die Liste und mit dem Gremium-Filter (``organization_name``, Work), wenn gesetzt. Zeitraum: letzte 12 Monate, 2 Jahre, 5 Jahre, älter (``filters``-Aggregation auf ``date``).
        """
        version = ranking_version(ranking)
        es_query = self._build_query(
            query,
            body_id,
            INDEX_PAPERS,
            date_from=date_from,
            date_to=date_to,
            body_ids=body_ids,
            ranking=version,
            boost=boost,
            organization_name=organization_name,
        )
        leer: dict[str, Any] = {"paper_types": {}, "periods": {}}
        try:
            if not self.client.indices.exists(index=INDEX_PAPERS):
                return leer
            probe = self.client.search(index=INDEX_PAPERS, body={"query": es_query, "size": 1, "_source": False})
            bester = float(probe["hits"].get("max_score") or 0)
            relevanz = float(getattr(settings, "SEARCH_MIN_RELEVANCE", 0.05)) if version == "v2" and query else 0.0
            body: dict[str, Any] = {
                "query": es_query,
                "size": 0,
                "aggs": {
                    "art": {"terms": {"field": "paper_type", "size": 200}},
                    "zeitraum": {
                        "filters": {
                            "filters": {
                                "12m": {"range": {"date": {"gte": f"{today}-12M"}}},
                                "2y": {"range": {"date": {"gte": f"{today}-2y"}}},
                                "5y": {"range": {"date": {"gte": f"{today}-5y"}}},
                                "older": {"range": {"date": {"lt": f"{today}-5y"}}},
                            }
                        }
                    },
                },
            }
            if relevanz > 0 and bester > 0:
                body["min_score"] = bester * relevanz
            aggs = self.client.search(index=INDEX_PAPERS, body=body)["aggregations"]
        except Exception as e:  # ohne Zähler bleiben die Filter nutzbar
            logger.warning(f"Filterzähler nicht ermittelbar: {e}")
            return leer
        return {
            "paper_types": {b["key"]: int(b["doc_count"]) for b in aggs["art"]["buckets"]},
            "periods": {k: int(v["doc_count"]) for k, v in aggs["zeitraum"]["buckets"].items()},
        }

    @staticmethod
    def _group_key(index_name: str, hit: Mapping[str, Any]) -> tuple[str, str]:
        source = hit.get("_source") or {}
        if index_name == INDEX_FILES:
            if source.get("paper_id"):
                return "paper", str(source["paper_id"])
            if source.get("meeting_id"):
                return "meeting", str(source["meeting_id"])
            return "file", str(hit["_id"])
        kind = {
            INDEX_PAPERS: "paper",
            INDEX_MEETINGS: "meeting",
            INDEX_PERSONS: "person",
            INDEX_ORGANIZATIONS: "organization",
        }
        return kind.get(index_name, index_name), str(hit["_id"])

    def _groups(
        self, ranked: RankedHits, indexes: list[str], weights: Mapping[str, float], sort: str
    ) -> list[dict[str, Any]]:
        """Gruppen in Listenreihenfolge: Rangfusion bzw. neuester Treffer; Mitglieder je Index nach Rang."""
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for index_name in indexes:
            weight = float(weights.get(index_name, 0.0))
            if weight <= 0:
                continue
            for rank, hit in enumerate(ranked.hits_by_index.get(index_name, []), start=1):
                kind, key = self._group_key(index_name, hit)
                group = groups.setdefault(
                    (kind, key), {"kind": kind, "key": key, "score": 0.0, "newest": None, "members": {}}
                )
                members = group["members"].setdefault(index_name, [])
                if not members:  # bester Platz dieses Index
                    group["score"] += weight / (RRF_K + rank)
                members.append(str(hit["_id"]))
                datum = (hit.get("sort") or [None])[0]
                if isinstance(datum, int | float) and (group["newest"] is None or datum > group["newest"]):
                    group["newest"] = float(datum)
        ordered = list(groups.values())
        if sort == SORT_NEWEST:
            ordered.sort(key=lambda g: (g["newest"] is None, -(g["newest"] or 0.0), -g["score"]))
        else:
            ordered.sort(key=lambda g: -g["score"])
        return ordered

    def _load_groups(
        self, page_groups: list[dict[str, Any]], queries: Mapping[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Inhalte der Gruppen einer Seite: Vorgang bzw. Sitzung, bestes Dokument mit Ausschnitt, weitere Namen.

        Je Index höchstens zwei Abfragen; Vorgänge und Sitzungen werden auch geladen, wenn nur ihre Dokumente
        treffen (Filter auf die Kennungen, die Abfrage nur für die Hervorhebung).
        """
        wanted: dict[str, list[str]] = {index: [] for index in ALL_INDEXES}
        main_files: list[str] = []
        other_files: list[str] = []
        for group in page_groups:
            kind, key = group["kind"], group["key"]
            if kind == "paper":
                wanted[INDEX_PAPERS].append(key)
            elif kind == "meeting":
                wanted[INDEX_MEETINGS].append(key)
            elif kind == "person":
                wanted[INDEX_PERSONS].append(key)
            elif kind == "organization":
                wanted[INDEX_ORGANIZATIONS].append(key)
            files = group["members"].get(INDEX_FILES, [])
            if files:
                main_files.append(files[0])
                other_files += files[1:MAX_DOCS_PER_GROUP]

        docs: dict[tuple[str, str], dict[str, Any]] = {}
        excludes = {INDEX_PAPERS: ["file_contents_preview", "file_names"], INDEX_FILES: ["text_content"]}
        for index_name, ids in [*wanted.items(), (INDEX_FILES, main_files)]:
            if ids:
                docs.update(self._fetch(index_name, ids, queries.get(index_name), excludes.get(index_name, [])))
        if other_files:
            body = {"query": {"ids": {"values": other_files}}, "size": len(other_files), "_source": FILE_LABEL_FIELDS}
            try:
                for hit in self.client.search(index=INDEX_FILES, body=body)["hits"]["hits"]:
                    docs[(INDEX_FILES, hit["_id"])] = self._to_result(hit, INDEX_FILES)
            except Exception as e:  # Namen weiterer Dokumente sind Beiwerk: ohne sie bleibt die Gruppe
                logger.warning(f"Weitere Dokumente nicht ladbar: {e}")

        result: list[dict[str, Any]] = []
        for group in page_groups:
            kind, key = group["kind"], group["key"]
            files = group["members"].get(INDEX_FILES, [])
            entry: dict[str, Any] = {"kind": kind, "key": key}
            index_by_kind = {
                "paper": INDEX_PAPERS,
                "meeting": INDEX_MEETINGS,
                "person": INDEX_PERSONS,
                "organization": INDEX_ORGANIZATIONS,
            }
            if kind in index_by_kind:
                entry[kind] = docs.get((index_by_kind[kind], key))
            if kind == "meeting":
                entry["meeting_id"] = key
            entry["file"] = docs.get((INDEX_FILES, files[0])) if files else None
            entry["others"] = [
                docs[(INDEX_FILES, doc_id)] for doc_id in files[1:MAX_DOCS_PER_GROUP] if (INDEX_FILES, doc_id) in docs
            ]
            entry["more"] = max(0, len(files) - 1)
            if kind != "file" and entry.get(kind) is None and entry["file"] is None:
                continue  # zwischen den Schritten gelöscht
            result.append(entry)
        return result

    def _fetch(
        self, index_name: str, ids: list[str], query: dict[str, Any] | None, excludes: list[str]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Dokumente per Kennung, mit Hervorhebung aus ``query``; scheitert sie, ohne Hervorhebung."""
        es_query: dict[str, Any] = {"bool": {"filter": [{"ids": {"values": ids}}]}}
        if query is not None:
            es_query["bool"]["should"] = [query]
        body: dict[str, Any] = {"query": es_query, "size": len(ids), "_source": {"excludes": excludes}}
        if query is not None:
            body["highlight"] = HIGHLIGHT
        try:
            hits = self.client.search(index=index_name, body=body)["hits"]["hits"]
        except Exception as e:
            logger.warning(f"Hervorhebung im Index '{index_name}' fehlgeschlagen, lade ohne: {e}")
            body.pop("highlight", None)
            try:
                hits = self.client.search(index=index_name, body=body)["hits"]["hits"]
            except Exception as e2:
                logger.error(f"Unerwarteter Fehler beim Laden der Treffer aus Index '{index_name}': {e2}")
                return {}
        return {(index_name, hit["_id"]): self._to_result(hit, index_name) for hit in hits}

    def _group_counts(self, ranked: RankedHits) -> dict[str, Any]:
        """Ehrliche Zahl: Vorgänge (Vorgang oder Datei mit Vorgang) und Sitzungsunterlagen ohne Vorgang.

        Eine Abfrage über ``papers`` und ``files`` mit der Mindestrelevanz je Index; gezählt wird ein
        Laufzeitfeld (Vorgang, sonst Sitzung, sonst die Datei) per ``cardinality``. Bis
        ``GROUP_COUNT_PRECISION`` ist die Zahl praktisch genau, darüber steht „rund“.
        """
        counts: dict[str, Any] = {
            "vorgaenge": 0,
            "unterlagen": 0,
            "meetings": ranked.totals_by_index.get(INDEX_MEETINGS, 0),
            "persons": ranked.totals_by_index.get(INDEX_PERSONS, 0),
            "organizations": ranked.totals_by_index.get(INDEX_ORGANIZATIONS, 0),
            "approx": False,
        }
        parts: list[dict[str, Any]] = []
        for index_name in (INDEX_PAPERS, INDEX_FILES):
            query = ranked.queries.get(index_name)
            if query is None:
                continue
            threshold = ranked.thresholds.get(index_name, 0.0)
            inner = {"function_score": {"query": query, "min_score": threshold}} if threshold > 0 else query
            parts.append({"bool": {"filter": [{"term": {"_index": index_name}}], "must": [inner]}})
        if not parts:
            return counts
        cardinality = {"cardinality": {"field": "gruppe", "precision_threshold": GROUP_COUNT_PRECISION}}
        body = {
            "size": 0,
            "query": {"bool": {"should": parts, "minimum_should_match": 1}},
            "runtime_mappings": {"gruppe": {"type": "keyword", "script": {"source": GROUP_KEY_SCRIPT}}},
            "aggs": {
                "vorgaenge": {
                    "filter": {
                        "bool": {
                            "should": [{"term": {"_index": INDEX_PAPERS}}, {"exists": {"field": "paper_id"}}],
                            "minimum_should_match": 1,
                        }
                    },
                    "aggs": {"n": cardinality},
                },
                "unterlagen": {
                    "filter": {
                        "bool": {
                            "filter": [{"term": {"_index": INDEX_FILES}}],
                            "must_not": [{"exists": {"field": "paper_id"}}],
                        }
                    },
                    "aggs": {"n": cardinality},
                },
            },
        }
        indexes = ",".join(index for index in (INDEX_PAPERS, INDEX_FILES) if index in ranked.queries)
        try:
            aggs = self.client.search(index=indexes, body=body)["aggregations"]
        except Exception as e:  # ohne Zahl bleibt die Liste nutzbar; sie zeigt dann die Trefferzahl je Typ
            logger.warning(f"Gruppenzahl nicht ermittelbar: {e}")
            counts["vorgaenge"] = ranked.totals_by_index.get(INDEX_PAPERS, 0)
            return counts
        counts["vorgaenge"] = int(aggs["vorgaenge"]["n"]["value"])
        counts["unterlagen"] = int(aggs["unterlagen"]["n"]["value"])
        counts["approx"] = max(counts["vorgaenge"], counts["unterlagen"]) > GROUP_COUNT_PRECISION
        return counts

    def _name_part_is_rare(self, query: str, body_id: str | None, body_ids: list[str] | None) -> bool:
        """Ist der Namensteil eines Straßenkompositums („witzleben“ in „Witzlebenstraße“) selten genug?

        Er gilt als eigene Lesart, wenn höchstens ``SEARCH_NAME_PART_MAX_DOCS`` Vorgänge und Dateien der
        Kommune ihn enthalten und mindestens die Hälfte davon eine Schreibweise der Straße.
        """
        queries = search_ranking.name_part_count_queries(search_ranking.parse(query))
        if queries is None:
            return False
        bare, forms = queries
        filters = self._filter_clauses(body_id, "papers", body_ids=body_ids)

        def count(clause: dict[str, Any]) -> int:
            return int(
                self.client.count(
                    index="papers,files",
                    ignore_unavailable=True,
                    query={"bool": {"must": [clause], "filter": filters}},
                )["count"]
            )

        try:
            bare_count = count(bare)
            if bare_count == 0 or bare_count > int(getattr(settings, "SEARCH_NAME_PART_MAX_DOCS", 400)):
                return False
            return count(forms) * 2 >= bare_count
        except Exception as e:  # Zählen ist nur eine Verfeinerung: ohne Antwort ohne Namensteil suchen
            logger.warning(f"Seltenheit des Namensteils nicht prüfbar: {e}")
            return False

    def _load_page_documents(
        self, page_hits: list[tuple[float, int, str, str]], queries: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Schritt 2: Dokumente einer Seite mit Hervorhebung laden, in der Reihenfolge der Seite.

        Scheitert die Hervorhebung (etwa an einem Feld über der Analysegrenze), lädt der Index die Treffer
        ohne Hervorhebung, statt alle Dokumente der Seite zu verwerfen.
        """
        docs: dict[tuple[str, str], dict[str, Any]] = {}
        for index_name in dict.fromkeys(eintrag[2] for eintrag in page_hits):
            ids = [eintrag[3] for eintrag in page_hits if eintrag[2] == index_name]
            body: dict[str, Any] = {
                # Dieselbe Abfrage (für die Hervorhebung), eingeschränkt auf die Treffer der Seite
                "query": {"bool": {"must": [queries[index_name]], "filter": [{"ids": {"values": ids}}]}},
                "size": len(ids),
                "highlight": HIGHLIGHT,
            }
            try:
                result = self.client.search(index=index_name, body=body)
            except Exception as e:
                logger.warning(f"Hervorhebung im Index '{index_name}' fehlgeschlagen, lade ohne: {e}")
                body.pop("highlight")
                try:
                    result = self.client.search(index=index_name, body=body)
                except Exception as e2:
                    logger.error(f"Unerwarteter Fehler beim Laden der Treffer aus Index '{index_name}': {e2}")
                    continue
            for hit in result["hits"]["hits"]:
                docs[(index_name, hit["_id"])] = self._to_result(hit, index_name)

        results = []
        for score, _position, index_name, doc_id in page_hits:
            doc = docs.get((index_name, doc_id))
            if doc is not None:  # zwischen beiden Schritten gelöscht → auslassen
                doc["_rankingScore"] = score
                results.append(doc)
        return results

    @staticmethod
    def _to_result(hit: dict[str, Any], index_name: str) -> dict[str, Any]:
        """Treffer als Ergebnis-Dict (Quelle, Index, Typ, Hervorhebung in ``_formatted``)."""
        doc: dict[str, Any] = dict(hit.get("_source") or {})
        doc["_rankingScore"] = hit.get("_score", 0)
        doc["_index"] = index_name
        if "type" not in doc:
            doc["type"] = index_name.rstrip("s")

        # Highlighting in _formatted übersetzen (Kompatibilität)
        if "highlight" in hit:
            formatted = dict(doc)
            for field_name, fragments in hit["highlight"].items():
                formatted[field_name] = fragments[0] if fragments else doc.get(field_name, "")
            doc["_formatted"] = formatted
        return doc

    # Datumsfeld je Index für Zeitraum-Filter
    DATE_FIELD_BY_INDEX = DATE_FIELD_BY_INDEX

    def _build_query(
        self,
        query: str,
        body_id: str | None,
        index_name: str,
        date_from: str | None = None,
        date_to: str | None = None,
        organization_name: str | None = None,
        paper_type: str | list[str] | None = None,
        body_ids: list[str] | None = None,
        *,
        ranking: str | None = None,
        fuzzy: bool = False,
        name_part_is_rare: bool = False,
        boost: RelationBoost | None = None,
    ) -> dict[str, Any]:
        """Baut die Elasticsearch-Query für einen Index (v1 wie bis 10/2026, v2 nach ``search_ranking``).

        ``boost`` (Bezug einer Organisation, Work) kommt als äußerer Faktor dazu, in beiden Versionen.
        """
        filter_clauses = self._filter_clauses(
            body_id,
            index_name,
            date_from=date_from,
            date_to=date_to,
            organization_name=organization_name,
            paper_type=paper_type,
            body_ids=body_ids,
        )
        if ranking_version(ranking) == "v1":
            must: list[dict[str, Any]] = []
            if query:
                must.append(
                    {
                        "multi_match": {
                            "query": query,
                            "fields": self._get_search_fields(index_name),
                            "type": "best_fields",
                            "fuzziness": "AUTO",
                        }
                    }
                )
            else:
                must.append({"match_all": {}})
            return search_ranking.with_relation({"bool": {"must": must, "filter": filter_clauses}}, index_name, boost)

        if not query:
            text: dict[str, Any] = {"match_all": {}}
        elif fuzzy:
            text = search_ranking.fuzzy_query(query, index_name)
        else:
            text = search_ranking.text_query(query, index_name, name_part_is_rare=name_part_is_rare)
        weight = float(getattr(settings, "SEARCH_RECENCY_WEIGHT", 1.0))
        recent = search_ranking.with_recency({"bool": {"must": [text], "filter": filter_clauses}}, index_name, weight)
        return search_ranking.with_relation(recent, index_name, boost)

    def _filter_clauses(
        self,
        body_id: str | None,
        index_name: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        organization_name: str | None = None,
        paper_type: str | list[str] | None = None,
        body_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Filter einer Suche: Kommune(n), Zeitraum, Gremium, Art."""
        filter_clauses: list[dict[str, Any]] = []
        # Kommune(n)-Filter: mehrere body_ids (terms) haben Vorrang vor
        # dem einzelnen body_id (term)
        if body_ids:
            filter_clauses.append({"terms": {"body_id": [str(b) for b in body_ids]}})
        elif body_id:
            filter_clauses.append({"term": {"body_id": body_id}})

        # Zeitraum-Filter (nur bei Indexen mit Datumsfeld)
        date_field = self.DATE_FIELD_BY_INDEX.get(index_name)
        if date_field and (date_from or date_to):
            range_clause: dict[str, str] = {}
            if date_from:
                range_clause["gte"] = date_from
            if date_to:
                range_clause["lte"] = date_to
            filter_clauses.append({"range": {date_field: range_clause}})

        # Gremium-Filter: organization_names ist ein analysiertes Textfeld,
        # daher match_phrase statt term (exakter Namens-Treffer)
        if organization_name and index_name in ("papers", "meetings", "files"):
            filter_clauses.append({"match_phrase": {"organization_names": organization_name}})

        if paper_type and index_name == "papers":
            if isinstance(paper_type, list):
                filter_clauses.append({"terms": {"paper_type": paper_type}})
            else:
                filter_clauses.append({"term": {"paper_type": paper_type}})
        return filter_clauses

    @staticmethod
    def _get_search_fields(index_name: str) -> list[str]:
        """Gibt die durchsuchbaren Felder für einen Index zurück."""
        fields_map = {
            "papers": [
                "name^3",
                "reference^2",
                "paper_type",
                "organization_names",
                "file_contents_preview",
                "file_names",
            ],
            "meetings": ["name^3", "organization_names^2", "location_name"],
            "persons": ["name^3", "given_name^2", "family_name^2", "title"],
            "organizations": ["name^3", "short_name^2", "organization_type", "classification"],
            "files": ["name^2", "file_name", "text_content", "paper_name", "paper_reference", "organization_names"],
        }
        return fields_map.get(index_name, ["name"])

    def search_papers(
        self,
        query: str,
        body_id: str | None = None,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int = 20,
        include_files: bool = True,
    ) -> dict[str, Any]:
        """Sucht in Papers und optional deren Dateien."""
        indexes = [INDEX_PAPERS]
        if include_files:
            indexes.append(INDEX_FILES)
        return self.search_all(query=query, body_id=body_id, page=page, page_size=page_size, index_names=indexes)

    def get_stats(self) -> dict[str, Any]:
        """Gibt Statistiken für alle Indexe zurück."""
        stats = {}
        for index_name in ALL_INDEXES:
            try:
                if not self.client.indices.exists(index=index_name):
                    stats[index_name] = {"numberOfDocuments": 0, "isIndexing": False}
                    continue
                count = self.client.count(index=index_name)
                stats[index_name] = {
                    "numberOfDocuments": count["count"],
                    "isIndexing": False,
                }
            except Exception as e:
                stats[index_name] = {"error": str(e)}
        return stats


# Singleton-Instanz
_search_service: ElasticsearchService | None = None


def get_search_service() -> ElasticsearchService:
    """Gibt die Singleton-Instanz des Search-Service zurück."""
    global _search_service
    if _search_service is None:
        _search_service = ElasticsearchService()
    return _search_service


def _safe_highlight(text: str | None) -> SafeString:
    """Sanitize highlighted text: escape HTML, restore only <mark> tags."""
    if not text:
        return SafeString("")
    # Replace highlight tags with placeholders
    text = str(text).replace(HIGHLIGHT_PRE, "\x00MARK_START\x00")
    text = text.replace(HIGHLIGHT_POST, "\x00MARK_END\x00")
    # Escape all remaining HTML
    text = escape(text)
    # Restore highlight tags
    text = text.replace("\x00MARK_START\x00", HIGHLIGHT_PRE)
    return mark_safe(text.replace("\x00MARK_END\x00", HIGHLIGHT_POST))  # nur <mark> bleibt HTML


def _text(value: Any, fallback: str = "") -> SafeString:
    """Rückfallwert (Name, Aktenzeichen …) aus der Quelle: immer maskiert."""
    return escape(str(value) if value not in (None, "") else fallback)


def format_search_result(hit: dict[str, Any]) -> dict[str, Any]:
    """
    Formatiert einen Elasticsearch-Treffer für die Template-Anzeige.

    Returns:
        Dict mit type, title, subtitle, url, highlight
    """
    result_type = hit.get("type", "unknown")

    # Highlighted Felder extrahieren (falls vorhanden)
    formatted = hit.get("_formatted", {})
    highlighted_name = _safe_highlight(formatted.get("name", hit.get("name")))
    # Ausschnitte vor der Maskierung säubern (Symbolschrift, Steuerzeichen, Silbentrennung)
    highlighted_text = _safe_highlight(clean_snippet(formatted.get("text_content", "")))

    # Titel und Vorschautexte sind immer SafeString: Hervorhebungen mit <mark>, alle Rückfallwerte
    # (Aktenzeichen, Namensteile, IDs) maskiert. Das Template gibt sie ohne |safe aus.
    if result_type == "paper":
        return {
            "type": "paper",
            "title": highlighted_name or _text(hit.get("name") or hit.get("reference"), "Vorgang"),
            "subtitle": hit.get("paper_type"),
            "url": f"/insight/vorgaenge/{hit.get('id')}/",
            "reference": hit.get("reference"),
            "highlight": highlighted_text if highlighted_text else None,
        }

    if result_type == "person":
        title = highlighted_name or (_text(hit.get("name")) if hit.get("name") else None)
        if not title:
            parts = []
            if hit.get("given_name"):
                parts.append(str(hit["given_name"]))
            if hit.get("family_name"):
                parts.append(str(hit["family_name"]))
            title = _text(" ".join(parts), "Person")
        return {
            "type": "person",
            "title": title,
            "subtitle": "Person",
            "url": f"/insight/personen/{hit.get('id')}/",
        }

    if result_type == "organization":
        return {
            "type": "organization",
            "title": highlighted_name or _text(hit.get("name"), "Gremium"),
            "subtitle": hit.get("organization_type"),
            "url": f"/insight/gremien/{hit.get('id')}/",
        }

    if result_type == "meeting":
        subtitle = None
        if hit.get("start"):
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(hit["start"].replace("Z", "+00:00"))
                subtitle = dt.strftime("%d.%m.%Y")
            except (ValueError, AttributeError):
                pass
        return {
            "type": "meeting",
            "title": highlighted_name or _text(hit.get("name"), "Sitzung"),
            "subtitle": subtitle,
            "url": f"/insight/termine/{hit.get('id')}/",
        }

    if result_type == "file":
        # Build enriched subtitle: V/2025/1234 · Jugendhilfeausschuss · 12.03.2026
        subtitle_parts = []
        if hit.get("paper_reference"):
            subtitle_parts.append(hit["paper_reference"])
        elif hit.get("paper_name"):
            subtitle_parts.append(hit["paper_name"])
        org_names = hit.get("organization_names")
        if org_names and isinstance(org_names, list) and org_names[0]:
            subtitle_parts.append(org_names[0])
        if hit.get("meeting_date"):
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(str(hit["meeting_date"]).replace("Z", "+00:00"))
                subtitle_parts.append(dt.strftime("%d.%m.%Y"))
            except (ValueError, AttributeError):
                pass
        return {
            "type": "file",
            "title": highlighted_name or _text(hit.get("name") or hit.get("file_name"), "Datei"),
            "subtitle": " \u00b7 ".join(subtitle_parts) if subtitle_parts else None,
            "url": f"/insight/vorgaenge/{hit.get('paper_id')}/",
            # Vorschau immer \u00fcber den eigenen Datei-Proxy, nie die Adresse aus der Quelle
            "access_url": _preview_url(hit.get("id")),
            "text_preview": highlighted_text or _text(clean_snippet(hit.get("text_preview"))),
            "paper_id": hit.get("paper_id"),
            "highlight": highlighted_text if highlighted_text else None,
        }

    return {
        "type": result_type,
        "title": _text(hit.get("id"), "Unbekannt"),
        "subtitle": None,
        "url": "#",
    }


def _preview_url(file_id: Any) -> str:
    """Vorschau-URL des Datei-Proxys für eine Datei-ID aus dem Index (leer bei ungültiger ID)."""
    import uuid

    try:
        return f"/insight/dokumente/{uuid.UUID(str(file_id))}/preview/"
    except ValueError:
        return ""

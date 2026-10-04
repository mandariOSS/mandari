# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Elasticsearch im Speicher für die Tests des Abonnements ``suchindex`` und des Schattenbetriebs.

Bildet nur, was der Code nutzt: Indizes anlegen, prüfen, löschen; Bulk mit externer Version
(neuere Version gewinnt, sonst 409); Zählen, ``mget``, Suche nach ``body_id`` mit Scroll und
Aggregation; Abbildung, Größe und Heap.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Gespeichert:
    source: dict[str, Any]
    version: int


@dataclass
class Index:
    settings: dict[str, Any] = field(default_factory=dict)
    mappings: dict[str, Any] = field(default_factory=dict)
    docs: dict[str, Gespeichert] = field(default_factory=dict)


class _Indices:
    def __init__(self, es: FakeElasticsearch) -> None:
        self.es = es

    def exists(self, *, index: str) -> bool:
        return all(name in self.es.indizes for name in index.split(","))

    def create(self, *, index: str, settings: dict[str, Any], mappings: dict[str, Any]) -> dict[str, Any]:
        if index in self.es.indizes:
            raise AssertionError(f"Index {index} existiert schon")
        self.es.indizes[index] = Index(settings=settings, mappings=mappings)
        return {"acknowledged": True}

    def delete(self, *, index: str) -> dict[str, Any]:
        for name in index.split(","):
            self.es.indizes.pop(name)
        return {"acknowledged": True}

    def get_mapping(self, *, index: str) -> dict[str, Any]:
        return {index: {"mappings": self.es.indizes[index].mappings}}

    def stats(self, *, index: str, metric: str) -> dict[str, Any]:
        groesse = sum(len(json.dumps(doc.source)) for doc in self.es.indizes[index].docs.values())
        return {"_all": {"primaries": {"store": {"size_in_bytes": groesse}}}}


class _Nodes:
    def stats(self, *, metric: str) -> dict[str, Any]:
        return {
            "nodes": {"n1": {"jvm": {"mem": {"heap_used_in_bytes": 512 * 1024 * 1024, "heap_max_in_bytes": 2**30}}}}
        }


class FakeElasticsearch:
    def __init__(self) -> None:
        self.indizes: dict[str, Index] = {}
        self.indices = _Indices(self)
        self.nodes = _Nodes()
        self.bulk_aufrufe = 0
        #: Kennung → Statuscode, den Bulk für dieses Dokument liefert (z. B. 400)
        self.ablehnen: dict[str, int] = {}
        #: Ausnahme, die der nächste Bulk-Aufruf wirft
        self.fehler: Exception | None = None
        self._scrolls: dict[str, list[list[dict[str, Any]]]] = {}

    # --- Hilfen für Tests -------------------------------------------------------------------

    def anlegen(self, name: str, mappings: dict[str, Any] | None = None) -> Index:
        return self.indizes.setdefault(name, Index(mappings=mappings or {}))

    def ablegen(self, name: str, source: dict[str, Any], version: int = 1) -> None:
        self.anlegen(name).docs[str(source["id"])] = Gespeichert(source=source, version=version)

    def doc(self, name: str, kennung: Any) -> Gespeichert | None:
        index = self.indizes.get(name)
        return index.docs.get(str(kennung)) if index else None

    # --- API ------------------------------------------------------------------------------------

    def bulk(self, *, operations: list[Any]) -> dict[str, Any]:
        self.bulk_aufrufe += 1
        if self.fehler is not None:
            fehler, self.fehler = self.fehler, None
            raise fehler
        items: list[dict[str, Any]] = []
        position = 0
        while position < len(operations):
            kopf = operations[position]
            position += 1
            aktion, meta = next(iter(kopf.items()))
            quelle = None
            if aktion == "index":
                quelle = operations[position]
                position += 1
                quelle = json.loads(quelle) if isinstance(quelle, str) else quelle
            items.append({aktion: self._eine(aktion, meta, quelle)})
        return {"errors": any(next(iter(i.values()))["status"] >= 300 for i in items), "items": items}

    def _eine(self, aktion: str, meta: dict[str, Any], quelle: dict[str, Any] | None) -> dict[str, Any]:
        name, kennung = meta["_index"], meta["_id"]
        antwort: dict[str, Any] = {"_index": name, "_id": kennung}
        if kennung in self.ablehnen:
            antwort.update(status=self.ablehnen[kennung], error={"type": "mapper_parsing_exception", "reason": "x"})
            return antwort
        assert meta.get("version_type") == "external"
        version = int(meta["version"])
        index = self.indizes.setdefault(name, Index())  # wie Elasticsearch: legt fehlende Indizes selbst an
        vorhanden = index.docs.get(kennung)
        if vorhanden is not None and vorhanden.version >= version:
            antwort.update(status=409, error={"type": "version_conflict_engine_exception"})
            return antwort
        if aktion == "delete":
            if vorhanden is None:
                antwort.update(status=404, result="not_found")
            else:
                del index.docs[kennung]
                antwort.update(status=200, result="deleted")
            return antwort
        assert quelle is not None
        index.docs[kennung] = Gespeichert(source=quelle, version=version)
        antwort.update(status=201 if vorhanden is None else 200)
        return antwort

    def count(
        self, *, index: str, allow_no_indices: bool = False, query: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        namen = [name for name in self.indizes if fnmatch.fnmatch(name, index)]
        if not namen and not allow_no_indices and "*" not in index:
            raise KeyError(index)
        kommunen = None if query is None else set(query["terms"]["body_id"])
        return {
            "count": sum(
                1
                for name in namen
                for doc in self.indizes[name].docs.values()
                if kommunen is None or doc.source.get("body_id") in kommunen
            )
        }

    def mget(self, *, index: str, ids: list[str], source: bool = True) -> dict[str, Any]:
        docs = []
        for kennung in ids:
            gespeichert = self.doc(index, kennung)
            eintrag: dict[str, Any] = {"_id": kennung, "found": gespeichert is not None}
            if gespeichert is not None and source:
                eintrag["_source"] = gespeichert.source
            docs.append(eintrag)
        return {"docs": docs}

    def search(self, *, index: str, size: int, **kwargs: Any) -> dict[str, Any]:
        docs = list(self.indizes[index].docs.values())
        if "aggs" in kwargs:
            anzahl: dict[str, int] = {}
            for doc in docs:
                anzahl[str(doc.source.get("body_id"))] = anzahl.get(str(doc.source.get("body_id")), 0) + 1
            buckets = [{"key": key, "doc_count": n} for key, n in sorted(anzahl.items())]
            return {"hits": {"hits": []}, "aggregations": {"kommunen": {"buckets": buckets}}}
        kommune = kwargs["query"]["term"]["body_id"]
        treffer = [{"_id": str(doc.source["id"])} for doc in docs if doc.source.get("body_id") == kommune]
        seiten = [treffer[start : start + size] for start in range(0, len(treffer), size)] or [[]]
        scroll_id = f"s{len(self._scrolls)}"
        self._scrolls[scroll_id] = seiten[1:]
        return {"_scroll_id": scroll_id, "hits": {"hits": seiten[0]}}

    def scroll(self, *, scroll_id: str, scroll: str) -> dict[str, Any]:
        seiten = self._scrolls[scroll_id]
        return {"_scroll_id": scroll_id, "hits": {"hits": seiten.pop(0) if seiten else []}}

    def clear_scroll(self, *, scroll_id: str) -> dict[str, Any]:
        self._scrolls.pop(scroll_id, None)
        return {"succeeded": True}

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Elasticsearch Service für Django.

Bietet Volltextsuche über Elasticsearch für alle OParl-Entitäten.
"""

import logging
from typing import Any

from django.conf import settings
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import NotFoundError

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

HIGHLIGHT = {
    "pre_tags": [HIGHLIGHT_PRE],
    "post_tags": [HIGHLIGHT_POST],
    "fields": {
        "name": {"number_of_fragments": 0},
        "text_content": {"fragment_size": 200, "number_of_fragments": 1},
        "reference": {"number_of_fragments": 0},
    },
}


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

        Returns:
            Dict mit results, total, page, page_size, pages

        Ablauf in zwei Schritten: Zuerst liefert jeder Index Kennung und Relevanz seiner besten
        ``page * page_size`` Treffer (ohne Dokumentinhalt); gemischt und nach Relevanz sortiert
        ergibt das die richtige Seite über alle Indexe. Danach werden nur die Dokumente dieser
        Seite samt Hervorhebung geladen. Vorher holte jeder Index fest ``2 * page_size``
        Treffer ab Position 0 – ab Seite 3 blieben Seiten leer, obwohl mehr Treffer gemeldet wurden.
        """
        if index_names is None:
            index_names = ALL_INDEXES

        page = max(1, int(page))
        start = (page - 1) * page_size
        depth = min(page * page_size, MAX_RESULT_DEPTH)

        # (Relevanz, Reihenfolge des Index, Index, Dokument-ID)
        ranking: list[tuple[float, int, str, str]] = []
        queries: dict[str, dict[str, Any]] = {}
        total_hits = 0
        error_count = 0

        for position, index_name in enumerate(index_names):
            try:
                # Prüfen ob Index existiert
                if not self.client.indices.exists(index=index_name):
                    continue

                es_query = self._build_query(
                    query,
                    body_id,
                    index_name,
                    date_from=date_from,
                    date_to=date_to,
                    organization_name=organization_name,
                    paper_type=paper_type,
                    body_ids=body_ids,
                )
                queries[index_name] = es_query

                # Schritt 1: nur Kennungen und Relevanz; liegt die Seite hinter der
                # Suchtiefe, reicht die Anzahl
                result = self.client.search(
                    index=index_name,
                    body={"query": es_query, "size": depth if start < depth else 0, "from": 0, "_source": False},
                )
                for hit in result["hits"]["hits"]:
                    ranking.append((float(hit.get("_score") or 0), position, index_name, hit["_id"]))
                total_hits += result["hits"]["total"]["value"]

            except NotFoundError:
                pass
            except Exception as e:
                error_count += 1
                logger.error(f"Unerwarteter Fehler bei Index '{index_name}': {e}")

        # Elasticsearch komplett nicht erreichbar → Fehler signalisieren, damit
        # Aufrufer (views/search.py) auf die Django-Datenbanksuche zurückfallen
        # können statt still leere Ergebnisse zu zeigen.
        if index_names and error_count == len(index_names):
            raise RuntimeError("Elasticsearch nicht erreichbar (alle Indexe fehlgeschlagen)")

        # Nach Relevanz mischen (bei Gleichstand in der Reihenfolge der Indexe), dann paginieren
        ranking.sort(key=lambda eintrag: (-eintrag[0], eintrag[1]))
        page_hits = ranking[start : start + page_size]

        return {
            "results": self._load_page_documents(page_hits, queries),
            "total": total_hits,
            "page": page,
            "page_size": page_size,
            "pages": (min(total_hits, MAX_RESULT_DEPTH) + page_size - 1) // page_size if total_hits > 0 else 0,
        }

    def _load_page_documents(
        self, page_hits: list[tuple[float, int, str, str]], queries: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Schritt 2: Dokumente einer Seite mit Hervorhebung laden, in der Reihenfolge der Seite."""
        docs: dict[tuple[str, str], dict[str, Any]] = {}
        for index_name in dict.fromkeys(eintrag[2] for eintrag in page_hits):
            ids = [eintrag[3] for eintrag in page_hits if eintrag[2] == index_name]
            try:
                result = self.client.search(
                    index=index_name,
                    body={
                        # Dieselbe Abfrage (für die Hervorhebung), eingeschränkt auf die Treffer der Seite
                        "query": {"bool": {"must": [queries[index_name]], "filter": [{"ids": {"values": ids}}]}},
                        "size": len(ids),
                        "highlight": HIGHLIGHT,
                    },
                )
            except Exception as e:
                logger.error(f"Unerwarteter Fehler beim Laden der Treffer aus Index '{index_name}': {e}")
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
            for field, fragments in hit["highlight"].items():
                formatted[field] = fragments[0] if fragments else doc.get(field, "")
            doc["_formatted"] = formatted
        return doc

    # Datumsfeld je Index für Zeitraum-Filter
    DATE_FIELD_BY_INDEX = {
        "papers": "date",
        "meetings": "start",
        "files": "meeting_date",
    }

    def _build_query(
        self,
        query: str,
        body_id: str | None,
        index_name: str,
        date_from: str | None = None,
        date_to: str | None = None,
        organization_name: str | None = None,
        paper_type: str | None = None,
        body_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Baut die Elasticsearch-Query für einen Index."""
        must = []
        filter_clauses = []

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
            filter_clauses.append({"term": {"paper_type": paper_type}})

        return {
            "bool": {
                "must": must,
                "filter": filter_clauses,
            }
        }

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
    highlighted_text = _safe_highlight(formatted.get("text_content", ""))

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
            "text_preview": highlighted_text or _text(hit.get("text_preview")),
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

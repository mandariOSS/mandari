# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Elasticsearch Service für Django.

Bietet Volltextsuche über Elasticsearch für alle OParl-Entitäten.
"""

import logging
from typing import Any

from django.conf import settings
from django.utils.html import escape
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
        """
        if index_names is None:
            index_names = ALL_INDEXES

        all_results: list[dict[str, Any]] = []
        total_hits = 0

        for index_name in index_names:
            try:
                # Prüfen ob Index existiert
                if not self.client.indices.exists(index=index_name):
                    continue

                # Query aufbauen
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

                result = self.client.search(
                    index=index_name,
                    body={
                        "query": es_query,
                        "size": page_size * 2,  # Mehr laden für Merge
                        "from": 0,
                        "highlight": {
                            "pre_tags": [HIGHLIGHT_PRE],
                            "post_tags": [HIGHLIGHT_POST],
                            "fields": {
                                "name": {"number_of_fragments": 0},
                                "text_content": {"fragment_size": 200, "number_of_fragments": 1},
                                "reference": {"number_of_fragments": 0},
                            },
                        },
                    },
                )

                for hit in result["hits"]["hits"]:
                    doc = hit["_source"]
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

                    all_results.append(doc)

                total_hits += result["hits"]["total"]["value"]

            except NotFoundError:
                pass
            except Exception as e:
                logger.error(f"Unerwarteter Fehler bei Index '{index_name}': {e}")

        # Nach Relevanz sortieren
        all_results.sort(key=lambda x: x.get("_rankingScore", 0), reverse=True)

        # Paginieren
        start = (page - 1) * page_size
        end = start + page_size
        paginated = all_results[start:end]

        return {
            "results": paginated,
            "total": total_hits,
            "page": page,
            "page_size": page_size,
            "pages": (total_hits + page_size - 1) // page_size if total_hits > 0 else 0,
        }

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

    def search_meetings(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Sucht nur in Sitzungen."""
        return self.search_all(
            query=query, body_id=body_id, page=page, page_size=page_size, index_names=[INDEX_MEETINGS]
        )

    def search_persons(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Sucht nur in Personen."""
        return self.search_all(
            query=query, body_id=body_id, page=page, page_size=page_size, index_names=[INDEX_PERSONS]
        )

    def search_organizations(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Sucht nur in Gremien."""
        return self.search_all(
            query=query, body_id=body_id, page=page, page_size=page_size, index_names=[INDEX_ORGANIZATIONS]
        )

    def search_files(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Sucht nur in Dateiinhalten."""
        return self.search_all(query=query, body_id=body_id, page=page, page_size=page_size, index_names=[INDEX_FILES])

    def search_single_index(
        self,
        query: str,
        index_name: str,
        body_id: str | None = None,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Sucht in einem einzelnen Index mit erweiterten Optionen.

        Args:
            query: Suchbegriff
            index_name: Name des Index
            body_id: Filter nach Kommune
            filters: Zusätzliche Filter als Dict
            page: Seitennummer
            page_size: Ergebnisse pro Seite
            sort: Sortierausdrücke (z.B. ["name:asc", "date:desc"])
        """
        try:
            es_query = self._build_query(query, body_id, index_name)

            # Zusätzliche Filter
            if filters:
                for key, value in filters.items():
                    if isinstance(value, list):
                        es_query["bool"]["filter"].append({"terms": {key: value}})
                    elif value is not None:
                        es_query["bool"]["filter"].append({"term": {key: value}})

            body: dict[str, Any] = {
                "query": es_query,
                "size": page_size,
                "from": (page - 1) * page_size,
            }

            # Sortierung
            if sort:
                es_sort = []
                for s in sort:
                    field, _, order = s.partition(":")
                    es_sort.append({field: {"order": order or "asc"}})
                body["sort"] = es_sort

            result = self.client.search(index=index_name, body=body)

            hits = []
            for hit in result["hits"]["hits"]:
                doc = hit["_source"]
                doc["_rankingScore"] = hit.get("_score", 0)
                hits.append(doc)

            total = result["hits"]["total"]["value"]
            return {
                "results": hits,
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": (total + page_size - 1) // page_size,
                "processing_time_ms": result.get("took", 0),
            }

        except Exception as e:
            logger.error(f"Suche in Index '{index_name}' fehlgeschlagen: {e}")
            return {
                "results": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "pages": 0,
                "error": str(e),
            }

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


def _safe_highlight(text: str | None) -> str:
    """Sanitize highlighted text: escape HTML, restore only <mark> tags."""
    if not text:
        return text or ""
    # Replace highlight tags with placeholders
    text = text.replace(HIGHLIGHT_PRE, "\x00MARK_START\x00")
    text = text.replace(HIGHLIGHT_POST, "\x00MARK_END\x00")
    # Escape all remaining HTML
    text = escape(text)
    # Restore highlight tags
    text = text.replace("\x00MARK_START\x00", HIGHLIGHT_PRE)
    text = text.replace("\x00MARK_END\x00", HIGHLIGHT_POST)
    return text


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

    if result_type == "paper":
        return {
            "type": "paper",
            "title": highlighted_name or hit.get("name") or hit.get("reference", "Vorgang"),
            "subtitle": hit.get("paper_type"),
            "url": f"/insight/vorgaenge/{hit.get('id')}/",
            "reference": hit.get("reference"),
            "highlight": highlighted_text if highlighted_text else None,
        }

    elif result_type == "person":
        title = highlighted_name or hit.get("name")
        if not title:
            parts = []
            if hit.get("given_name"):
                parts.append(hit["given_name"])
            if hit.get("family_name"):
                parts.append(hit["family_name"])
            title = " ".join(parts) if parts else "Person"
        return {
            "type": "person",
            "title": title,
            "subtitle": "Person",
            "url": f"/insight/personen/{hit.get('id')}/",
        }

    elif result_type == "organization":
        return {
            "type": "organization",
            "title": highlighted_name or hit.get("name", "Gremium"),
            "subtitle": hit.get("organization_type"),
            "url": f"/insight/gremien/{hit.get('id')}/",
        }

    elif result_type == "meeting":
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
            "title": highlighted_name or hit.get("name", "Sitzung"),
            "subtitle": subtitle,
            "url": f"/insight/termine/{hit.get('id')}/",
        }

    elif result_type == "file":
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
            "title": highlighted_name or escape(hit.get("name") or hit.get("file_name", "Datei")),
            "subtitle": " \u00b7 ".join(subtitle_parts) if subtitle_parts else None,
            "url": f"/insight/vorgaenge/{hit.get('paper_id')}/",
            "access_url": hit.get("access_url", ""),
            "text_preview": highlighted_text or escape(hit.get("text_preview", "")),
            "paper_id": hit.get("paper_id"),
            "highlight": highlighted_text if highlighted_text else None,
        }

    return {
        "type": result_type,
        "title": str(hit.get("id", "Unbekannt")),
        "subtitle": None,
        "url": "#",
    }

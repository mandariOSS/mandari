"""
Meilisearch Service für Django.

Bietet Volltextsuche über Meilisearch für alle OParl-Entitäten.
"""

import logging
from typing import Any

import meilisearch
from django.conf import settings
from meilisearch.errors import MeilisearchApiError

logger = logging.getLogger(__name__)

# Index names (müssen mit apps/api/src/search/service.py übereinstimmen)
INDEX_MEETINGS = "meetings"
INDEX_PAPERS = "papers"
INDEX_PERSONS = "persons"
INDEX_ORGANIZATIONS = "organizations"
INDEX_FILES = "files"

ALL_INDEXES = [INDEX_MEETINGS, INDEX_PAPERS, INDEX_PERSONS, INDEX_ORGANIZATIONS, INDEX_FILES]


class MeilisearchService:
    """Service für Meilisearch-Integration in Django."""

    def __init__(self):
        """Initialisiert den Meilisearch-Client."""
        self.client = meilisearch.Client(
            settings.MEILISEARCH_URL,
            settings.MEILISEARCH_KEY,
        )

    def is_healthy(self) -> bool:
        """Prüft ob Meilisearch verfügbar ist."""
        try:
            health = self.client.health()
            return health.get("status") == "available"
        except Exception as e:
            logger.warning(f"Meilisearch health check fehlgeschlagen: {e}")
            return False

    def search_all(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
        index_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Multi-Index-Suche über alle Entitäten.

        Args:
            query: Suchbegriff
            body_id: Filter nach Kommune (UUID als String)
            page: Seitennummer (1-indiziert)
            page_size: Ergebnisse pro Seite
            index_names: Zu durchsuchende Indexe (Standard: alle)

        Returns:
            Dict mit results, total, page, page_size, pages
        """
        if index_names is None:
            index_names = ALL_INDEXES

        all_results: list[dict[str, Any]] = []
        total_hits = 0

        # Filter aufbauen
        filter_str = None
        if body_id:
            filter_str = f"body_id = '{body_id}'"

        # Jeden Index durchsuchen
        for index_name in index_names:
            try:
                index = self.client.index(index_name)
                search_params: dict[str, Any] = {
                    "limit": page_size * 2,  # Mehr laden für Merge
                    "offset": 0,
                    "showRankingScore": True,
                }
                if filter_str:
                    search_params["filter"] = filter_str

                result = index.search(query, search_params)

                # Typ zu jedem Treffer hinzufügen
                for hit in result.get("hits", []):
                    # Singularform für Type (meetings -> meeting)
                    hit["_index"] = index_name
                    if "type" not in hit:
                        hit["type"] = index_name.rstrip("s")
                    all_results.append(hit)

                total_hits += result.get("estimatedTotalHits", 0)

            except MeilisearchApiError as e:
                logger.warning(f"Suche in Index '{index_name}' fehlgeschlagen: {e}")
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

    def search_papers(
        self,
        query: str,
        body_id: str | None = None,
        filters: dict[str, Any] | None = None,
        page: int = 1,
        page_size: int = 20,
        include_files: bool = True,
    ) -> dict[str, Any]:
        """
        Sucht in Papers und optional deren Dateien.

        Args:
            query: Suchbegriff
            body_id: Filter nach Kommune
            filters: Zusätzliche Filter (paper_type, date, etc.)
            page: Seitennummer
            page_size: Ergebnisse pro Seite
            include_files: Auch in Dateiinhalten suchen

        Returns:
            Dict mit results, total, page, page_size, pages
        """
        indexes = [INDEX_PAPERS]
        if include_files:
            indexes.append(INDEX_FILES)

        return self.search_all(
            query=query,
            body_id=body_id,
            page=page,
            page_size=page_size,
            index_names=indexes,
        )

    def search_meetings(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        Sucht nur in Sitzungen.

        Args:
            query: Suchbegriff
            body_id: Filter nach Kommune
            page: Seitennummer
            page_size: Ergebnisse pro Seite

        Returns:
            Dict mit results, total, page, page_size, pages
        """
        return self.search_all(
            query=query,
            body_id=body_id,
            page=page,
            page_size=page_size,
            index_names=[INDEX_MEETINGS],
        )

    def search_persons(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        Sucht nur in Personen.

        Args:
            query: Suchbegriff
            body_id: Filter nach Kommune
            page: Seitennummer
            page_size: Ergebnisse pro Seite

        Returns:
            Dict mit results, total, page, page_size, pages
        """
        return self.search_all(
            query=query,
            body_id=body_id,
            page=page,
            page_size=page_size,
            index_names=[INDEX_PERSONS],
        )

    def search_organizations(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        Sucht nur in Gremien.

        Args:
            query: Suchbegriff
            body_id: Filter nach Kommune
            page: Seitennummer
            page_size: Ergebnisse pro Seite

        Returns:
            Dict mit results, total, page, page_size, pages
        """
        return self.search_all(
            query=query,
            body_id=body_id,
            page=page,
            page_size=page_size,
            index_names=[INDEX_ORGANIZATIONS],
        )

    def search_files(
        self,
        query: str,
        body_id: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """
        Sucht nur in Dateiinhalten.

        Args:
            query: Suchbegriff
            body_id: Filter nach Kommune
            page: Seitennummer
            page_size: Ergebnisse pro Seite

        Returns:
            Dict mit results, total, page, page_size, pages
        """
        return self.search_all(
            query=query,
            body_id=body_id,
            page=page,
            page_size=page_size,
            index_names=[INDEX_FILES],
        )

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

        Returns:
            Dict mit results, total, page, page_size, pages
        """
        try:
            index = self.client.index(index_name)

            # Filter aufbauen
            filter_parts = []
            if body_id:
                filter_parts.append(f"body_id = '{body_id}'")
            if filters:
                for key, value in filters.items():
                    if isinstance(value, list):
                        filter_parts.append(f"{key} IN {value}")
                    elif value is not None:
                        filter_parts.append(f"{key} = '{value}'")

            search_params: dict[str, Any] = {
                "limit": page_size,
                "offset": (page - 1) * page_size,
                "showRankingScore": True,
            }

            if filter_parts:
                search_params["filter"] = " AND ".join(filter_parts)
            if sort:
                search_params["sort"] = sort

            result = index.search(query, search_params)

            return {
                "results": result.get("hits", []),
                "total": result.get("estimatedTotalHits", 0),
                "page": page,
                "page_size": page_size,
                "pages": (result.get("estimatedTotalHits", 0) + page_size - 1) // page_size,
                "processing_time_ms": result.get("processingTimeMs", 0),
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
                index = self.client.index(index_name)
                index_stats = index.get_stats()
                stats[index_name] = {
                    "numberOfDocuments": index_stats.get("numberOfDocuments", 0),
                    "isIndexing": index_stats.get("isIndexing", False),
                }
            except Exception as e:
                stats[index_name] = {"error": str(e)}
        return stats


# Singleton-Instanz
_search_service: MeilisearchService | None = None


def get_search_service() -> MeilisearchService:
    """Gibt die Singleton-Instanz des Search-Service zurück."""
    global _search_service
    if _search_service is None:
        _search_service = MeilisearchService()
    return _search_service


def format_search_result(hit: dict[str, Any]) -> dict[str, Any]:
    """
    Formatiert ein Meilisearch-Treffer für die Template-Anzeige.

    Returns:
        Dict mit type, title, subtitle, url, highlight
    """
    result_type = hit.get("type", "unknown")

    if result_type == "paper":
        return {
            "type": "paper",
            "title": hit.get("name") or hit.get("reference", "Vorgang"),
            "subtitle": hit.get("paper_type"),
            "url": f"/vorgaenge/{hit.get('id')}/",
            "reference": hit.get("reference"),
        }

    elif result_type == "person":
        title = hit.get("name")
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
            "url": f"/personen/{hit.get('id')}/",
        }

    elif result_type == "organization":
        return {
            "type": "organization",
            "title": hit.get("name", "Gremium"),
            "subtitle": hit.get("organization_type"),
            "url": f"/gremien/{hit.get('id')}/",
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
            "title": hit.get("name", "Sitzung"),
            "subtitle": subtitle,
            "url": f"/termine/{hit.get('id')}/",
        }

    elif result_type == "file":
        return {
            "type": "file",
            "title": hit.get("name") or hit.get("file_name", "Datei"),
            "subtitle": hit.get("paper_name") or hit.get("paper_reference"),
            "url": f"/vorgaenge/{hit.get('paper_id')}/",
            "text_preview": hit.get("text_preview", ""),
            "paper_id": hit.get("paper_id"),
        }

    return {
        "type": result_type,
        "title": str(hit.get("id", "Unbekannt")),
        "subtitle": None,
        "url": "#",
    }

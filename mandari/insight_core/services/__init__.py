"""
Services für Mandari Insight Core.

Enthält Business-Logik und externe Service-Integrationen.
"""

from .document_extraction import (
    DocumentDownloadError,
    ExtractedDocument,
    download_and_extract,
    extract_text_from_file,
)
from .search_service import (
    INDEX_FILES,
    INDEX_MEETINGS,
    INDEX_ORGANIZATIONS,
    INDEX_PAPERS,
    INDEX_PERSONS,
    MeilisearchService,
    format_search_result,
    get_search_service,
)

__all__ = [
    # Document extraction
    "ExtractedDocument",
    "DocumentDownloadError",
    "download_and_extract",
    "extract_text_from_file",
    # Search service
    "MeilisearchService",
    "get_search_service",
    "format_search_result",
    "INDEX_MEETINGS",
    "INDEX_PAPERS",
    "INDEX_PERSONS",
    "INDEX_ORGANIZATIONS",
    "INDEX_FILES",
]

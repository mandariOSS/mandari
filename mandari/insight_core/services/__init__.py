"""
Services für Mandari Insight Core.

Enthält Business-Logik und externe Service-Integrationen.
"""

from .document_extraction import (
    ExtractedDocument,
    DocumentDownloadError,
    download_and_extract,
    extract_text_from_file,
)
from .search_service import (
    MeilisearchService,
    get_search_service,
    format_search_result,
    INDEX_MEETINGS,
    INDEX_PAPERS,
    INDEX_PERSONS,
    INDEX_ORGANIZATIONS,
    INDEX_FILES,
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

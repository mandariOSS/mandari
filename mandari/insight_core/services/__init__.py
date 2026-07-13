# SPDX-License-Identifier: AGPL-3.0-or-later
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
    ElasticsearchService,
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
    "ElasticsearchService",
    "get_search_service",
    "format_search_result",
    "INDEX_MEETINGS",
    "INDEX_PAPERS",
    "INDEX_PERSONS",
    "INDEX_ORGANIZATIONS",
    "INDEX_FILES",
]

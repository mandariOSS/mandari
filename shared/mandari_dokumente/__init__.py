"""
mandari-dokumente: Dokumentbibliothek für Ingestor und Anwendung.

Enthält die eine Texterkennung (Issue #530, ``docs/adr/20261004-texterkennung-shared.md``): Datei → Text mit
pypdf, optional Mistral und Tesseract Seite für Seite mit Speicher- und Zeitgrenzen (Issue #817). Ohne
Django- oder Datenbankbezug; Einstellungen, Abruf und Speichern bleiben beim Aufrufer.
"""

from .mistral import MistralConfig, MistralError, MistralRateLimitError
from .ocr import MEMORY_LIMIT_REASON, OcrLimits, OcrMemoryLimitError, OcrResult, ocr_pdf, page_dpi
from .texterkennung import (
    METHOD_MISTRAL,
    METHOD_NONE,
    METHOD_PYPDF,
    METHOD_TESSERACT,
    METHOD_TEXT,
    PDF_MIME_TYPES,
    ExtractionConfig,
    ExtractionResult,
    extract_text,
)

__all__ = [
    "MEMORY_LIMIT_REASON",
    "METHOD_MISTRAL",
    "METHOD_NONE",
    "METHOD_PYPDF",
    "METHOD_TESSERACT",
    "METHOD_TEXT",
    "PDF_MIME_TYPES",
    "ExtractionConfig",
    "ExtractionResult",
    "MistralConfig",
    "MistralError",
    "MistralRateLimitError",
    "OcrLimits",
    "OcrMemoryLimitError",
    "OcrResult",
    "extract_text",
    "ocr_pdf",
    "page_dpi",
]

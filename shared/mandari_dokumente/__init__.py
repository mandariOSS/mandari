"""
mandari-dokumente: Dokumentbibliothek für Ingestor und Anwendung.

Enthält die eine Texterkennung (Issue #530, ``docs/adr/20261004-texterkennung-shared.md``): Datei → Text mit
pypdf, optional Mistral und Tesseract Seite für Seite mit Speicher- und Zeitgrenzen (Issue #817). Ohne
Django- oder Datenbankbezug; Einstellungen, Abruf und Speichern bleiben beim Aufrufer.

Dazu die Positivliste erlaubter KI-Endpunkte (``ki_hosts``, Issue #950), die Anwendung und Ingestor teilen.
"""

from .ki_hosts import STANDARD_ERLAUBTE_HOSTS, erlaubte_hosts_aus_umgebung, ist_erlaubter_host
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
    "STANDARD_ERLAUBTE_HOSTS",
    "ExtractionConfig",
    "ExtractionResult",
    "MistralConfig",
    "MistralError",
    "MistralRateLimitError",
    "OcrLimits",
    "OcrMemoryLimitError",
    "OcrResult",
    "erlaubte_hosts_aus_umgebung",
    "extract_text",
    "ist_erlaubter_host",
    "ocr_pdf",
    "page_dpi",
]

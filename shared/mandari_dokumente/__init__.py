"""
mandari-dokumente: Dokumentbibliothek für Ingestor und Anwendung.

Enthält die eine Texterkennung (Issue #530, ``docs/adr/20261004-texterkennung-shared.md``): Datei → Text mit
pypdf, optional Mistral und Tesseract Seite für Seite mit Speicher- und Zeitgrenzen (Issue #817). Ohne
Django- oder Datenbankbezug; Einstellungen, Abruf und Speichern bleiben beim Aufrufer. Die erste Stufe (nur die
Textebene, ``textebene``) läuft auch allein und in einem eigenen Prozess (Issue #1107).

Dazu die Positivliste erlaubter KI-Endpunkte (``ki_hosts``, Issue #950), die Anwendung und Ingestor teilen. Sie
hat keinen Standard: Ohne ``KI_ERLAUBTE_HOSTS`` ist jeder Host gesperrt (``STANDARD_ERLAUBTE_HOSTS`` ist leer).
"""

from .ki_hosts import STANDARD_ERLAUBTE_HOSTS, erlaubte_hosts_aus_umgebung, ist_erlaubter_host
from .mistral import MistralConfig, MistralError, MistralRateLimitError
from .ocr import MEMORY_LIMIT_REASON, OcrLimits, OcrMemoryLimitError, OcrResult, ocr_pdf, page_dpi
from .textebene import MIN_PAGE_CHARS, TextLayer, read_text_layer
from .texterkennung import (
    EXTRACTION_VERSION,
    LAYER_ERROR,
    LAYER_MEMORY,
    LAYER_NEEDS_OCR,
    LAYER_OK,
    LAYER_TIMEOUT,
    METHOD_MISTRAL,
    METHOD_NONE,
    METHOD_PYPDF,
    METHOD_TESSERACT,
    METHOD_TEXT,
    PDF_MIME_TYPES,
    ExtractionConfig,
    ExtractionResult,
    IsolationLimits,
    TextLayerOutcome,
    extract_text,
    extract_text_layer,
    extract_text_layer_isolated,
)

__all__ = [
    "EXTRACTION_VERSION",
    "LAYER_ERROR",
    "LAYER_MEMORY",
    "LAYER_NEEDS_OCR",
    "LAYER_OK",
    "LAYER_TIMEOUT",
    "MEMORY_LIMIT_REASON",
    "MIN_PAGE_CHARS",
    "METHOD_MISTRAL",
    "METHOD_NONE",
    "METHOD_PYPDF",
    "METHOD_TESSERACT",
    "METHOD_TEXT",
    "PDF_MIME_TYPES",
    "STANDARD_ERLAUBTE_HOSTS",
    "ExtractionConfig",
    "ExtractionResult",
    "IsolationLimits",
    "MistralConfig",
    "MistralError",
    "MistralRateLimitError",
    "OcrLimits",
    "OcrMemoryLimitError",
    "OcrResult",
    "TextLayer",
    "TextLayerOutcome",
    "erlaubte_hosts_aus_umgebung",
    "extract_text",
    "extract_text_layer",
    "extract_text_layer_isolated",
    "ist_erlaubter_host",
    "ocr_pdf",
    "page_dpi",
    "read_text_layer",
]

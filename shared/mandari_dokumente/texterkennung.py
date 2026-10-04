"""
Texterkennung: Datei (Pfad oder Bytes) → Text. Die eine Implementierung für den OCR-Worker des Ingestors
und den Auftrag ``file.extract_text`` der Anwendung (Issue #530).

Für PDFs gilt die Kette

1. pypdf (Textebene; liefert auch die Seitengrößen),
2. Mistral (nur mit API-Schlüssel),
3. Tesseract Seite für Seite mit Speicher- und Zeitgrenzen (``mandari_dokumente.ocr``).

Text- und HTML-Dateien werden dekodiert, andere Dateien nur, wenn sie wie Text aussehen; Word-Dateien und
andere Binärformate ergeben keinen Text. Das Ergebnis ist ein ``ExtractionResult`` ohne Null-Bytes
(PostgreSQL lehnt sie in Textfeldern ab). ``OcrMemoryLimitError`` aus der Texterkennung geht an den Aufrufer:
Er entscheidet, ob die Datei als gescheitert gilt („Speichergrenze“).
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from io import BytesIO
from pathlib import Path
from typing import Any, Final

from .mistral import MistralConfig, MistralError, extract_text_with_mistral
from .ocr import OcrLimits, ocr_pdf

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - pypdf ist Abhängigkeit beider Verbraucher
    PdfReader = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

PDF_MIME_TYPES: Final = frozenset({"application/pdf", "application/x-pdf"})
WORD_MIME_TYPES: Final = frozenset(
    {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
#: Textdateien werden höchstens bis zu dieser Größe gelesen
MAX_TEXT_BYTES: Final = 20 * 1024 * 1024

METHOD_PYPDF: Final = "pypdf"
METHOD_MISTRAL: Final = "mistral"
METHOD_TESSERACT: Final = "tesseract"
METHOD_TEXT: Final = "text"
METHOD_NONE: Final = "none"
OCR_METHODS: Final = frozenset({METHOD_MISTRAL, METHOD_TESSERACT})


@dataclass(frozen=True)
class ExtractionConfig:
    """Grenzen der Texterkennung und Zugang zu Mistral (aus den Einstellungen des Aufrufers)."""

    ocr: OcrLimits = field(default_factory=OcrLimits)
    mistral: MistralConfig = field(default_factory=MistralConfig)


@dataclass(frozen=True)
class ExtractionResult:
    """Ergebnis der Texterkennung einer Datei."""

    text: str
    method: str
    page_count: int | None = None
    #: Hinweise ohne Inhalte, etwa übersprungene Seiten an der Speichergrenze
    notes: tuple[str, ...] = ()

    @property
    def ocr_performed(self) -> bool:
        return self.method in OCR_METHODS


def extract_text(
    source: Path | bytes,
    mime_type: str | None = None,
    file_name: str = "",
    config: ExtractionConfig | None = None,
) -> ExtractionResult:
    """Text aus einer Datei (Pfad) oder aus Bytes; Art nach MIME-Typ, Dateiname bzw. Dateianfang."""
    config = config or ExtractionConfig()
    mime = (mime_type or "").split(";")[0].strip().lower()
    head = _head(source)
    is_pdf = mime in PDF_MIME_TYPES or file_name.lower().endswith(".pdf") or (not mime and head[:5] == b"%PDF-")
    if is_pdf:
        result = _extract_pdf(source, file_name, config)
    elif mime in WORD_MIME_TYPES:
        logger.info("Word-Datei ohne Texterkennung: %s", file_name or "ohne Namen")
        result = ExtractionResult("", METHOD_NONE)
    else:
        data = _read(source, MAX_TEXT_BYTES)
        if mime.startswith("text/"):
            text = _decode(data)
            if mime == "text/html":
                text = _strip_html_tags(text)
            result = ExtractionResult(text, METHOD_TEXT)
        elif _looks_like_text(data):
            text = _decode(data)
            result = ExtractionResult(text, METHOD_TEXT if text.strip() else METHOD_NONE)
        else:
            result = ExtractionResult("", METHOD_NONE)
    return replace(result, text=result.text.replace("\x00", "").strip())


def _extract_pdf(source: Path | bytes, file_name: str, config: ExtractionConfig) -> ExtractionResult:
    page_count: int | None = None
    page_sizes: list[tuple[float, float] | None] | None = None

    # 1. pypdf: Textebene und Seitengrößen (für die Auflösung der Texterkennung)
    if PdfReader is not None:
        try:
            reader = PdfReader(BytesIO(source) if isinstance(source, bytes) else source)
            page_count = len(reader.pages)
            fragments: list[str] = []
            page_sizes = []
            for page in reader.pages:
                try:
                    fragments.append((page.extract_text() or "").strip())
                except Exception:  # noqa: BLE001 - eine defekte Seite verhindert den Rest nicht
                    fragments.append("")
                if len(page_sizes) < config.ocr.max_pages:
                    page_sizes.append(_page_size(page))
            text = "\n\n".join(f for f in fragments if f)
            del reader, fragments
            if text.strip():
                return ExtractionResult(text, METHOD_PYPDF, page_count)
        except Exception as exc:  # noqa: BLE001 - kaputte Textebene: weiter mit Texterkennung
            logger.warning("pypdf: Textebene nicht lesbar (%s)", type(exc).__name__)

    # 2. Mistral (optional)
    if config.mistral.enabled:
        try:
            text = extract_text_with_mistral(_read(source, None), config.mistral, file_name)
            if text.strip():
                return ExtractionResult(text, METHOD_MISTRAL, page_count)
        except MistralError as exc:
            logger.warning("Mistral ohne Ergebnis für %s, weiter mit Tesseract: %s", file_name or "PDF", exc)

    # 3. Tesseract Seite für Seite mit Grenzen; OcrMemoryLimitError geht an den Aufrufer
    with _as_path(source) as path:
        result = ocr_pdf(path, page_count=page_count, page_sizes=page_sizes, limits=config.ocr)
    if result.notes:
        logger.warning("Texterkennung %s: %s", file_name or "PDF", "; ".join(result.notes))
    if result.text.strip():
        return ExtractionResult(result.text, METHOD_TESSERACT, page_count, tuple(result.notes))
    return ExtractionResult("", METHOD_NONE, page_count, tuple(result.notes))


def _page_size(page: Any) -> tuple[float, float] | None:
    """Breite und Höhe in PDF-Punkten (MediaBox wie pdftoppm, mit UserUnit); ``None`` bei Fehlern."""
    try:
        box = page.mediabox
        unit = float(getattr(page, "user_unit", 1) or 1)
        return abs(float(box.width)) * unit, abs(float(box.height)) * unit
    except Exception:  # noqa: BLE001 - ohne Größe skaliert die Texterkennung über die lange Seite
        return None


@contextmanager
def _as_path(source: Path | bytes) -> Iterator[Path]:
    """Pfad für ``pdftoppm``; Bytes landen dafür in einer temporären Datei, die danach verschwindet."""
    if not isinstance(source, bytes):
        yield source
        return
    handle, name = tempfile.mkstemp(suffix=".pdf", prefix="texterkennung-")
    path = Path(name)
    try:
        with os.fdopen(handle, "wb") as target:
            target.write(source)
        yield path
    finally:
        path.unlink(missing_ok=True)


def _head(source: Path | bytes) -> bytes:
    return _read(source, 512)


def _read(source: Path | bytes, limit: int | None) -> bytes:
    if isinstance(source, bytes):
        return source if limit is None else source[:limit]
    with open(source, "rb") as handle:
        return handle.read() if limit is None else handle.read(limit)


def _looks_like_text(data: bytes) -> bool:
    """Binärdateien (Null-Bytes am Anfang) ergeben keinen Text statt Zeichensalat."""
    return bool(data) and b"\x00" not in data[:8192]


def _decode(data: bytes) -> str:
    """UTF-8, sonst latin-1."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="ignore")


def _strip_html_tags(html: str) -> str:
    """Text eines HTML-Dokuments ohne Auszeichnung."""
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.fragments: list[str] = []

        def handle_data(self, data: str) -> None:
            cleaned = data.strip()
            if cleaned:
                self.fragments.append(cleaned)

    parser = _Text()
    parser.feed(html)
    return "\n".join(parser.fragments)

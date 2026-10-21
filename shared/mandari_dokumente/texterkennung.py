"""
Texterkennung: Datei (Pfad oder Bytes) → Text. Die eine Implementierung für den OCR-Worker des Ingestors
und die Aufträge der Anwendung (Issues #530, #1107).

Für PDFs gilt die Kette

1. pypdf (Textebene; liefert auch die Seitengrößen, ``mandari_dokumente.textebene``),
2. Mistral (nur mit Schlüssel, Adresse und erlaubtem Host, siehe ``MistralConfig.enabled``),
3. Tesseract Seite für Seite mit Speicher- und Zeitgrenzen (``mandari_dokumente.ocr``).

Reicht die Textebene (``TextLayer.sufficient``), endet die Kette nach Schritt 1. Mit ``ocr_mixed_pages`` erkennt
Tesseract bei einem sonst getippten PDF nur die eingescannten Seiten und setzt ihren Text an ihre Stelle; Mistral gilt
wie bisher nur für Dateien ganz ohne Textebene.

**Zwei Stufen** (Issue #1107): ``extract_text_layer`` ist Schritt 1 allein und liefert ``None``, wenn die Datei die
Erkennung aus dem Bild braucht; ``extract_text_layer_isolated`` führt ihn in einem eigenen Prozess mit Speicher- und
Zeitgrenze aus (``python -m mandari_dokumente textebene``). Die Anwendung nutzt das für den leichten Auftrag
``file.read_text_layer``; nur Dateien, für die er ``None`` liefert, gehen an ``extract_text`` im schweren Worker.

Text- und HTML-Dateien werden dekodiert, andere Dateien nur, wenn sie wie Text aussehen; Word-Dateien und
andere Binärformate ergeben keinen Text. Das Ergebnis ist ein ``ExtractionResult`` ohne Null-Bytes
(PostgreSQL lehnt sie in Textfeldern ab). ``OcrMemoryLimitError`` aus der Texterkennung geht an den Aufrufer:
Er entscheidet, ob die Datei als gescheitert gilt („Speichergrenze“).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from .mistral import MistralConfig, MistralError, extract_text_with_mistral
from .ocr import MEMORY_LIMIT_REASON, OcrLimits, OcrMemoryLimitError, ocr_pdf
from .textebene import TextLayer, read_text_layer

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

#: Version der Texterkennung (Issue #919, ``docs/adr/20261007-dokumentkette.md``, Abschnitt 4). Steigt um eins,
#: wenn eine Änderung bestehende Texte verbessert und der Bestand deshalb neu erkannt werden soll; die Anwendung
#: plant Dateien mit älterer Version dann schrittweise aus der Ablage neu ein. Texte ohne Version (vor #919) gelten
#: als Version 1.
EXTRACTION_VERSION: Final = 1

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
    #: Eingescannte Seiten eines sonst getippten PDFs erkennen (Issue #1107); aus = jeder Text reicht (bisher)
    ocr_mixed_pages: bool = False


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
    mime = _mime(mime_type)
    if _is_pdf(source, mime, file_name):
        layer = read_text_layer(source, config.ocr.max_pages)
        result = _extract_pdf(source, file_name, config, layer)
    else:
        result = _extract_other(source, mime, file_name)
    return _clean(result)


def extract_text_layer(
    source: Path | bytes,
    mime_type: str | None = None,
    file_name: str = "",
    config: ExtractionConfig | None = None,
) -> ExtractionResult | None:
    """
    Erste Stufe allein (Issue #1107): Text ohne Erkennung aus dem Bild, sonst ``None``. Dateien, die keine PDFs
    sind, erkennt sie vollständig (sie brauchen nie eine Erkennung aus dem Bild); ein PDF nur, wenn seine Textebene
    reicht (``TextLayer.sufficient``, dieselbe Entscheidung wie in ``extract_text``).
    """
    config = config or ExtractionConfig()
    mime = _mime(mime_type)
    if not _is_pdf(source, mime, file_name):
        return _clean(_extract_other(source, mime, file_name))
    layer = read_text_layer(source, config.ocr.max_pages)
    if not layer.sufficient(config.ocr_mixed_pages):
        return None
    return _clean(_from_layer(layer))


def _mime(mime_type: str | None) -> str:
    return (mime_type or "").split(";")[0].strip().lower()


def _is_pdf(source: Path | bytes, mime: str, file_name: str) -> bool:
    return mime in PDF_MIME_TYPES or file_name.lower().endswith(".pdf") or (not mime and _head(source)[:5] == b"%PDF-")


def _clean(result: ExtractionResult) -> ExtractionResult:
    return replace(result, text=result.text.replace("\x00", "").strip())


def _from_layer(layer: TextLayer) -> ExtractionResult:
    return ExtractionResult(layer.text, METHOD_PYPDF, layer.page_count)


def _extract_other(source: Path | bytes, mime: str, file_name: str) -> ExtractionResult:
    """Dateien außer PDFs: Text und HTML dekodiert, Word- und Binärdateien ohne Text."""
    if mime in WORD_MIME_TYPES:
        logger.info("Word-Datei ohne Texterkennung: %s", file_name or "ohne Namen")
        return ExtractionResult("", METHOD_NONE)
    data = _read(source, MAX_TEXT_BYTES)
    if mime.startswith("text/"):
        text = _decode(data)
        if mime == "text/html":
            text = _strip_html_tags(text)
        return ExtractionResult(text, METHOD_TEXT)
    if _looks_like_text(data):
        text = _decode(data)
        return ExtractionResult(text, METHOD_TEXT if text.strip() else METHOD_NONE)
    return ExtractionResult("", METHOD_NONE)


def _extract_pdf(source: Path | bytes, file_name: str, config: ExtractionConfig, layer: TextLayer) -> ExtractionResult:
    # 1. pypdf: Reicht die Textebene, ist die Datei fertig
    if layer.sufficient(config.ocr_mixed_pages):
        return _from_layer(layer)
    if layer.has_text:
        # Getipptes PDF mit eingescannten Seiten (nur mit ocr_mixed_pages): nur diese erkennen
        return _extract_scan_pages(source, file_name, config, layer)
    page_sizes = list(layer.page_sizes) if layer.page_count is not None else None

    # 2. Mistral (optional)
    if config.mistral.enabled:
        try:
            text = extract_text_with_mistral(_read(source, None), config.mistral, file_name)
            if text.strip():
                return ExtractionResult(text, METHOD_MISTRAL, layer.page_count)
        except MistralError as exc:
            logger.warning("Mistral ohne Ergebnis für %s, weiter mit Tesseract: %s", file_name or "PDF", exc)

    # 3. Tesseract Seite für Seite mit Grenzen; OcrMemoryLimitError geht an den Aufrufer
    with _as_path(source) as path:
        result = ocr_pdf(path, page_count=layer.page_count, page_sizes=page_sizes, limits=config.ocr)
    if result.notes:
        logger.warning("Texterkennung %s: %s", file_name or "PDF", "; ".join(result.notes))
    if result.text.strip():
        return ExtractionResult(result.text, METHOD_TESSERACT, layer.page_count, tuple(result.notes))
    return ExtractionResult("", METHOD_NONE, layer.page_count, tuple(result.notes))


def _extract_scan_pages(
    source: Path | bytes, file_name: str, config: ExtractionConfig, layer: TextLayer
) -> ExtractionResult:
    """
    Tesseract nur für die eingescannten Seiten (``layer.scan_pages``); ihr Text steht an ihrer Stelle, sonst der der
    Textebene. Ergibt die Erkennung nichts (auch an der Speichergrenze), bleibt der Text der Textebene.
    """
    try:
        with _as_path(source) as path:
            result = ocr_pdf(
                path,
                page_count=layer.page_count,
                page_sizes=list(layer.page_sizes),
                limits=config.ocr,
                pages=layer.scan_pages,
            )
    except OcrMemoryLimitError:
        logger.warning("Texterkennung %s: eingescannte Seiten an der Speichergrenze", file_name or "PDF")
        return replace(_from_layer(layer), notes=(f"Eingescannte Seiten an der {MEMORY_LIMIT_REASON}",))
    notes = tuple(result.notes)
    if notes:
        logger.warning("Texterkennung %s: %s", file_name or "PDF", "; ".join(notes))
    if not result.page_texts:
        return replace(_from_layer(layer), notes=notes)
    seiten = [result.page_texts.get(nummer, text) for nummer, text in enumerate(layer.pages, start=1)]
    text = "\n\n".join(seite for seite in seiten if seite)
    return ExtractionResult(text, METHOD_TESSERACT, layer.page_count, notes)


# =============================================================================
# Erste Stufe in einem eigenen Prozess (Issue #1107)
# =============================================================================


@dataclass(frozen=True)
class IsolationLimits:
    """Grenzen des Prozesses der ersten Stufe: Adressraum in MB (0 = ohne) und Zeit in Sekunden."""

    memory_limit_mb: int = 512
    timeout: float = 120.0


#: Ergebnis der ersten Stufe im eigenen Prozess (``TextLayerOutcome.reason``)
LAYER_OK: Final = "textebene"
LAYER_NEEDS_OCR: Final = "texterkennung"
LAYER_MEMORY: Final = "speicher"
LAYER_TIMEOUT: Final = "zeit"
LAYER_ERROR: Final = "fehler"
#: Exit-Code des Prozesses, wenn ihm der Speicher ausgeht (``MemoryError``)
EXIT_MEMORY: Final = 3
#: Beendet vom Speicherwächter (SIGKILL), direkt oder über die Shell
_KILLED: Final = frozenset({-9, 137})

#: Aufruf des Prozesses: (Befehl, Zeitgrenze, Speichergrenze in MB, Umgebung); in Tests ersetzbar
IsolatedRunner = Callable[[Sequence[str], float, int, dict[str, str]], "subprocess.CompletedProcess[bytes]"]


@dataclass(frozen=True)
class TextLayerOutcome:
    """Ergebnis der ersten Stufe: Text oder ``None`` (die Datei braucht die zweite Stufe) und der Grund."""

    result: ExtractionResult | None
    reason: str


def run_isolated(
    command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """
    Prozess der ersten Stufe mit Grenze des Adressraums (``ulimit -v``), Zeitgrenze und niedriger Priorität
    (``nice``). Er meldet sich dem Speicherwächter als Erster (``oom_score_adj`` 1000): Reicht der Speicher des
    Containers nicht, trifft es diesen Prozess, nicht den Worker. Ohne ``/bin/sh`` (Windows, Entwicklung) läuft der
    Befehl ohne diese Grenzen.
    """
    argv = list(command)
    if os.name == "posix" and Path("/bin/sh").exists():
        grenze = str(memory_limit_mb * 1024) if memory_limit_mb > 0 else "unlimited"
        skript = (
            'echo 1000 2>/dev/null >/proc/self/oom_score_adj; ulimit -v "$0" || exit 125; '
            'if command -v nice >/dev/null 2>&1; then exec nice -n 10 "$@"; fi; exec "$@"'
        )
        argv = ["/bin/sh", "-c", skript, grenze, *argv]
    return subprocess.run(argv, capture_output=True, timeout=timeout, env=env, check=False)


def extract_text_layer_isolated(
    source: Path,
    mime_type: str | None,
    file_name: str,
    config: ExtractionConfig,
    limits: IsolationLimits | None = None,
    runner: IsolatedRunner | None = None,
) -> TextLayerOutcome:
    """
    ``extract_text_layer`` in einem eigenen Prozess (``python -m mandari_dokumente textebene``). Endet er an Speicher
    oder Zeit oder bricht er ab, braucht die Datei die zweite Stufe (``result`` ist ``None``): Dort liest
    ``extract_text`` mit mehr Speicher und Zeit, und ein unlesbarer Inhalt endet wie bisher als gescheitert.

    Eigener Prozess, weil pypdf reines Python ist: Mehrere Threads eines Workers liefen nacheinander (GIL), und eine
    große oder defekte Datei träfe den ganzen Worker.
    """
    limits = limits or IsolationLimits()
    command = [
        sys.executable,
        "-m",
        "mandari_dokumente",
        "textebene",
        "--mime",
        mime_type or "",
        "--name",
        file_name or "",
        "--max-pages",
        str(config.ocr.max_pages),
    ]
    if config.ocr_mixed_pages:
        command.append("--gemischt")
    command.append(str(source))
    env = dict(os.environ)
    # Der Prozess findet die Bibliothek auch, wenn der Aufrufer sie nur über seinen sys.path kennt
    paket = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(teil for teil in (paket, env.get("PYTHONPATH", "")) if teil)
    try:
        lauf = (runner or run_isolated)(command, limits.timeout, limits.memory_limit_mb, env)
    except subprocess.TimeoutExpired:
        logger.warning("Textebene %s: Zeitgrenze (%.0f s), weiter mit der Texterkennung", file_name, limits.timeout)
        return TextLayerOutcome(None, LAYER_TIMEOUT)
    if lauf.returncode != 0:
        speicher = (
            lauf.returncode == EXIT_MEMORY or lauf.returncode in _KILLED or b"MemoryError" in (lauf.stderr or b"")
        )
        logger.warning(
            "Textebene %s: Prozess endete mit %s%s, weiter mit der Texterkennung",
            file_name,
            lauf.returncode,
            " (Speichergrenze)" if speicher else "",
        )
        return TextLayerOutcome(None, LAYER_MEMORY if speicher else LAYER_ERROR)
    try:
        daten = json.loads((lauf.stdout or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("Textebene %s: Ausgabe nicht lesbar, weiter mit der Texterkennung", file_name)
        return TextLayerOutcome(None, LAYER_ERROR)
    if not isinstance(daten, dict) or daten.get("needs_ocr", True):
        return TextLayerOutcome(None, LAYER_NEEDS_OCR)
    seiten = daten.get("page_count")
    result = ExtractionResult(
        str(daten.get("text") or ""),
        str(daten.get("method") or METHOD_NONE),
        seiten if isinstance(seiten, int) else None,
        tuple(str(note) for note in daten.get("notes") or ()),
    )
    return TextLayerOutcome(_clean(result), LAYER_OK)


def text_layer_main(argv: Sequence[str] | None = None) -> int:
    """Einstieg des Prozesses (``python -m mandari_dokumente textebene``): Ergebnis als JSON auf stdout."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m mandari_dokumente textebene")
    parser.add_argument("--mime", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--max-pages", type=int, default=OcrLimits().max_pages)
    parser.add_argument("--gemischt", action="store_true")
    parser.add_argument("pfad")
    args = parser.parse_args(argv)
    config = ExtractionConfig(ocr=OcrLimits(max_pages=args.max_pages), ocr_mixed_pages=args.gemischt)
    try:
        result = extract_text_layer(Path(args.pfad), args.mime, args.name, config)
    except MemoryError:
        sys.stderr.write("MemoryError\n")
        return EXIT_MEMORY
    daten: dict[str, object] = {"needs_ocr": True}
    if result is not None:
        daten = {
            "needs_ocr": False,
            "text": result.text,
            "method": result.method,
            "page_count": result.page_count,
            "notes": list(result.notes),
        }
    sys.stdout.buffer.write(json.dumps(daten, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


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

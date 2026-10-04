"""
Seitenweise Texterkennung mit Speicher- und Zeitgrenzen (Issue #817).

Der OCR-Worker wurde vom Kernel wegen Speichermangels beendet, sobald eine Seite sehr groß war (Pläne,
Anlagenbände): ``pdf2image`` lud jede Seite als Bild in den Python-Prozess, ``pytesseract`` schrieb sie
noch einmal heraus, und Tesseract lief ohne Grenze. Hier laufen Rendern und Erkennen als eigene
Unterprozesse, das Bild liegt nur als Datei vor:

- ``pdftoppm`` rendert genau eine Seite in Graustufen, mit einer Auflösung, die aus der Seitengröße
  folgt: höchstens ``max_pixels`` Bildpunkte je Seite, für A4 bleibt es bei ``dpi``.
- ``tesseract`` erkennt die Seite mit einem Thread (``OMP_THREAD_LIMIT=1``).
- Beide Unterprozesse bekommen eine Grenze für den Adressraum (``ulimit -v``) und eine Zeitgrenze. Reißt
  eine Seite die Speichergrenze, folgt ein zweiter Versuch mit halber Auflösung, danach wird die Seite
  übersprungen. Der Worker selbst bleibt am Leben.
- Für die ganze Datei gilt ein Zeitbudget; danach endet die Erkennung mit dem bis dahin erkannten Text.

Kommt aus keiner Seite Text und lag es an der Speichergrenze, meldet ``OcrMemoryLimitError`` das als
eigenen Grund („Speichergrenze“), damit die Datei als gescheitert gilt statt immer wieder zu laufen.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Feste Begründung für Dateien, deren Erkennung an der Speichergrenze scheitert (Fehlerfeld der Datei)
MEMORY_LIMIT_REASON = "Speichergrenze"

#: Ausgaben von Tesseract/Leptonica/Poppler, wenn eine Speicheranforderung scheitert
_MEMORY_MARKERS = (
    "bad_alloc",
    "malloc fail",
    "cannot allocate",
    "out of memory",
    "memory allocation",
    "allocation failed",
)

#: Beendet durch SIGKILL (Speicherwächter des Kernels) oder SIGABRT (``std::bad_alloc``), Linux-Nummern;
#: direkt als Signal (negativ) oder über eine Shell (128 + Signal)
_KILLED_RETURNCODES = frozenset({-9, -6, 137, 134})

#: Untere Grenze der Auflösung; die Grenze der Bildpunkte geht immer vor
MIN_DPI = 1

#: Verkleinerung je Versuch: erst volle, dann halbe Auflösung (ein Viertel der Bildpunkte)
_SCALES = (1.0, 0.5)


@dataclass(frozen=True)
class OcrLimits:
    """Grenzen der Texterkennung je Seite und Datei."""

    dpi: int = 200
    max_pixels: int = 8_000_000
    memory_limit_mb: int = 1024
    page_timeout: float = 120.0
    file_budget: float = 1200.0
    max_pages: int = 100
    lang: str = "deu"


@dataclass
class OcrResult:
    """Ergebnis der seitenweisen Erkennung einer Datei."""

    text: str = ""
    pages_rendered: int = 0
    memory_limited_pages: int = 0
    timed_out_pages: int = 0
    budget_exhausted: bool = False
    notes: list[str] = field(default_factory=list)


class OcrMemoryLimitError(Exception):
    """Keine Seite ergab Text, weil die Erkennung an der Speichergrenze scheiterte."""


class _SubprocessError(Exception):
    def __init__(self, step: str, memory: bool, timeout: bool = False, detail: str = "") -> None:
        super().__init__(detail)
        self.step = step
        self.memory = memory
        self.timeout = timeout


#: Aufruf eines Unterprozesses: (Befehl, Zeitgrenze, Speichergrenze in MB, Umgebung); in Tests ersetzbar
Runner = Callable[[Sequence[str], float, int, dict[str, str]], "subprocess.CompletedProcess[bytes]"]


def page_dpi(width_pt: float | None, height_pt: float | None, limits: OcrLimits) -> int:
    """
    Auflösung für eine Seite der Größe ``width_pt`` × ``height_pt`` (PDF-Punkte, 1/72 Zoll).

    A4 (595 × 842 pt) bei 200 dpi sind 3,9 Mio. Bildpunkte; ein A0-Plan wären 62 Mio. und damit mehrere
    Gigabyte im Speicher von Tesseract. Die Auflösung sinkt deshalb, bis die Seite höchstens
    ``limits.max_pixels`` Bildpunkte hat. Ohne bekannte Größe gilt ``limits.dpi``.
    """
    if not width_pt or not height_pt or width_pt <= 0 or height_pt <= 0:
        return limits.dpi
    area = (width_pt / 72.0) * (height_pt / 72.0)
    cap = math.floor(math.sqrt(limits.max_pixels / area))
    return max(MIN_DPI, min(limits.dpi, cap))


def tools_available() -> bool:
    return shutil.which("pdftoppm") is not None and shutil.which("tesseract") is not None


def run_limited(
    command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """
    Unterprozess mit Zeit- und Speichergrenze.

    Unter Linux setzt ``/bin/sh`` vor dem ``exec`` die Grenze des Adressraums (``ulimit -v``); so gilt sie
    ab dem ersten Byte, ohne ``preexec_fn`` in einem Prozess mit Threads. Ohne ``/bin/sh`` (Windows,
    Entwicklung) läuft der Befehl ohne Speichergrenze. Bei Zeitablauf beendet ``subprocess.run`` den
    Prozess und wirft ``TimeoutExpired``.
    """
    argv = list(command)
    if memory_limit_mb > 0 and os.name == "posix" and Path("/bin/sh").exists():
        argv = ["/bin/sh", "-c", 'ulimit -v "$0" && exec "$@"', str(memory_limit_mb * 1024), *argv]
    return subprocess.run(argv, capture_output=True, timeout=timeout, env=env, check=False)


def _is_memory_failure(result: subprocess.CompletedProcess[bytes]) -> bool:
    if result.returncode in _KILLED_RETURNCODES:
        return True
    stderr = (result.stderr or b"").decode("utf-8", errors="replace").lower()
    return any(marker in stderr for marker in _MEMORY_MARKERS)


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    # Tesseract startet sonst je Kern einen Thread mit eigenem Speicher; ein Thread reicht je Seite
    env["OMP_THREAD_LIMIT"] = "1"
    return env


class PageOcr:
    """Rendert und erkennt einzelne Seiten über Unterprozesse."""

    def __init__(self, limits: OcrLimits, runner: Runner | None = None) -> None:
        self.limits = limits
        self.runner: Runner = runner or run_limited
        self.env = _environment()

    def _run(self, step: str, command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        try:
            result = self.runner(command, self.limits.page_timeout, self.limits.memory_limit_mb, self.env)
        except subprocess.TimeoutExpired as exc:
            raise _SubprocessError(step, memory=False, timeout=True, detail="Zeitgrenze") from exc
        if result.returncode != 0:
            raise _SubprocessError(step, memory=_is_memory_failure(result), detail=f"Exit-Code {result.returncode}")
        return result

    def render(self, source: Path, page_no: int, dpi: int | None, workdir: Path, scale: float = 1.0) -> Path:
        """
        Eine Seite als Graustufenbild (PGM) in ``workdir``. ``dpi=None``: Seitengröße unbekannt, die lange
        Seite wird auf die Grenze der Bildpunkte skaliert (eine quadratische Seite erreicht sie genau);
        ``scale`` verkleinert sie für einen neuen Versuch.
        """
        prefix = workdir / f"seite-{page_no}"
        command = ["pdftoppm", "-f", str(page_no), "-l", str(page_no), "-gray", "-singlefile"]
        if dpi is None:
            command += ["-scale-to", str(max(1, math.floor(math.sqrt(self.limits.max_pixels) * scale)))]
        else:
            command += ["-r", str(dpi)]
        self._run("render", [*command, str(source), str(prefix)])
        image = prefix.with_suffix(".pgm")
        if not image.is_file():
            raise _SubprocessError("render", memory=False, detail="kein Bild")
        return image

    def recognize(self, image: Path, dpi: int | None) -> str:
        command = ["tesseract", str(image), "stdout", "-l", self.limits.lang]
        if dpi is not None:
            # PGM trägt keine Auflösung; ohne Angabe schätzt Tesseract sie (und meldet 70 dpi)
            command += ["--dpi", str(dpi)]
        result = self._run("ocr", command)
        return (result.stdout or b"").decode("utf-8", errors="replace").strip()


def ocr_pdf(
    source: Path,
    page_count: int | None = None,
    page_sizes: Sequence[tuple[float, float] | None] | None = None,
    limits: OcrLimits | None = None,
    runner: Runner | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> OcrResult:
    """
    Text eines PDFs Seite für Seite erkennen, mit Speicher- und Zeitgrenzen je Seite und Budget je Datei.

    ``page_sizes`` (Breite, Höhe in PDF-Punkten je Seite, etwa aus pypdf) bestimmt die Auflösung je Seite;
    ohne Größe wird die lange Seite auf die Grenze der Bildpunkte skaliert. Ohne ``page_count`` läuft die
    Erkennung bis zur ersten Seite, die sich nicht rendern lässt.
    """
    limits = limits or OcrLimits()
    result = OcrResult()
    if runner is None and not tools_available():
        logger.warning("OCR nicht verfügbar (pdftoppm oder tesseract fehlt)")
        return result

    ocr = PageOcr(limits, runner)
    max_pages = min(page_count, limits.max_pages) if page_count else limits.max_pages
    started = clock()
    fragments: list[str] = []

    for page_no in range(1, max_pages + 1):
        if clock() - started > limits.file_budget:
            result.budget_exhausted = True
            result.notes.append(f"Zeitbudget erschöpft nach {page_no - 1} Seiten")
            logger.warning("OCR %s: Zeitbudget erschöpft nach %d Seiten", source.name, page_no - 1)
            break
        size = page_sizes[page_no - 1] if page_sizes and page_no - 1 < len(page_sizes) else None
        dpi = page_dpi(size[0], size[1], limits) if size else None
        text, status = _page(ocr, source, page_no, dpi)
        if status == "nicht_renderbar":
            if page_no == 1:
                # Schon die erste Seite nicht renderbar: kein OCR möglich (defekte Datei)
                return result
            if not page_count:
                # Ohne bekannte Seitenzahl: hinter der letzten Seite
                break
            continue
        result.pages_rendered += 1
        if status == "speicher":
            result.memory_limited_pages += 1
        elif status == "zeit":
            result.timed_out_pages += 1
        if text:
            fragments.append(text)

    result.text = "\n\n".join(fragments)
    if result.memory_limited_pages:
        result.notes.append(f"{result.memory_limited_pages} Seiten an der {MEMORY_LIMIT_REASON}")
    if result.timed_out_pages:
        result.notes.append(f"{result.timed_out_pages} Seiten über der Zeitgrenze")
    if not result.text and result.memory_limited_pages:
        raise OcrMemoryLimitError(
            f"{MEMORY_LIMIT_REASON}: {result.memory_limited_pages} von {result.pages_rendered} Seiten "
            "auch mit halber Auflösung nicht erkennbar"
        )
    return result


def _page(ocr: PageOcr, source: Path, page_no: int, dpi: int | None) -> tuple[str, str]:
    """
    Eine Seite rendern und erkennen. Status: ``ok``, ``speicher`` (auch mit halber Auflösung an der
    Speichergrenze), ``zeit`` (Zeitgrenze), ``fehler`` (Tesseract scheiterte) oder ``nicht_renderbar``.
    """
    for attempt, scale in enumerate(_SCALES):
        last = attempt == len(_SCALES) - 1
        current = None if dpi is None else max(MIN_DPI, math.floor(dpi * scale))
        # Eigenes Verzeichnis je Versuch: Bilder verschwinden sofort wieder, auch bei Abbruch
        with tempfile.TemporaryDirectory(prefix="ocr-seite-") as tmpdir:
            try:
                image = ocr.render(source, page_no, current, Path(tmpdir), scale)
                return ocr.recognize(image, current), "ok"
            except _SubprocessError as exc:
                if exc.memory and not last:
                    logger.info("OCR Seite %d: Speichergrenze, neuer Versuch mit halber Auflösung", page_no)
                    continue
                if exc.memory:
                    logger.warning("OCR Seite %d: Speichergrenze auch mit halber Auflösung", page_no)
                    return "", "speicher"
                if exc.timeout:
                    logger.warning("OCR Seite %d: Zeitgrenze überschritten (%s)", page_no, exc.step)
                    return "", "zeit"
                if exc.step == "render":
                    return "", "nicht_renderbar"
                logger.warning("OCR Seite %d: Tesseract fehlgeschlagen (%s)", page_no, exc)
                return "", "fehler"
    return "", "speicher"

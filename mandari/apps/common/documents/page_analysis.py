# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seitenanalyse von PDFs: Karten und Pläne erkennen (Issue #599).

Je Seite werden gemessen: Format, Vektorzeichnung (Pfadsegmente und Dichte), eingebettete Rasterbilder
mit effektiver Auflösung und Flächenanteil, der Textlayer (auf Wunsch mit Positionen), Kartenstichworte,
Koordinatenbeschriftungen, der maschinenlesbare Maßstab aus ``/VP``/``/Measure`` (AutoCAD-Plots) und
Merkmale eines GeoPDF. ``classify_page`` ordnet die Seite nach den Regeln aus dem Machbarkeitstest
(Stufe C) als Karte bzw. Plan ein.

Bibliotheken (Begründung: ``docs/adr/20260930-pdf-seitenanalyse-bibliothek.md``):

- **pypdfium2** (PDFium; Apache-2.0 bzw. BSD-3-Clause) für Seitenobjekte, Rasterbilder, Text mit
  Positionen und Vorschaubilder,
- **pypdf** (BSD-3-Clause) für Einträge des Seitenverzeichnisses, die PDFium nicht herausgibt
  (``/VP`` mit ``/Measure``, ``/LGIDict``).

Das Modul ist fachfrei (Plattform-Schicht): Es kennt weder Kommunen noch Vorlagen. Straßennamen
zählt der Aufrufer mit seinem Straßenverzeichnis und übergibt die Zahl an ``classify_page``.
Nichts wird gespeichert und nichts aus dem Netz geladen.
"""

from __future__ import annotations

import io
import logging
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pypdf.errors import PyPdfError

logger = logging.getLogger(__name__)

MM_PER_PT = 25.4 / 72
METERS_PER_PT = 0.0254 / 72
CM2_PER_PT2 = (2.54 / 72) ** 2
PAPER_FORMATS = {"A4": (210, 297), "A3": (297, 420), "A2": (420, 594), "A1": (594, 841), "A0": (841, 1189)}
UNIT_TO_METERS = {"": 1.0, "m": 1.0, "mm": 0.001, "cm": 0.01, "km": 1000.0, "ft": 0.3048, "in": 0.0254, "mi": 1609.344}

# Obergrenzen gegen Ausreißer (ein A0-Plan hat einige hunderttausend Objekte)
MAX_PAGES = 300
MAX_OBJECTS_PER_PAGE = 2_000_000
MAX_FORM_DEPTH = 8
PALETTE_DPI = 40

# Regeln aus dem Machbarkeitstest (Stufe C), siehe docs/INSIGHT_KARTENANALYSE.md
VECTOR_MIN_SEGMENTS = 5_000
VECTOR_MIN_DENSITY = 10.0  # Segmente je cm²
TEXT_HEAVY_CHARS = 1_500
IMAGE_DOMINANT_COVERAGE = 0.5
PALETTE_MAP_SHARE = 0.8
MAP_SCORE_THRESHOLD = 3

MAP_KEYWORDS = re.compile(
    r"maßstab|lageplan|übersichtsplan|geltungsbereich|gemarkung|\bflur\b|flurstück|legende|planzeich"
    r"|kartengrundlage|liegenschaftskataster|flurkarte|1\s*:\s*\d{3,5}\b",
    re.IGNORECASE,
)
# Koordinatenpaar Rechts-/Hochwert: UTM (6 Stellen, optional mit Zone 32/33) oder Gauß-Krüger (7 Stellen)
COORDINATE_PAIR = re.compile(
    r"(?<![\d.,])(?:(?:3[23]\s?)?[2-9]\d{5}|[2-5]\d{6})(?:[.,]\d{1,3})?"
    r"\s*[/;,]?\s*5[2-9]\d{5}(?:[.,]\d{1,3})?(?!\d)"
)


class DocumentError(Exception):
    """Das PDF lässt sich nicht öffnen (beschädigt, verschlüsselt, kein PDF)."""


@dataclass(frozen=True)
class ImageInfo:
    """Eingebettetes Rasterbild auf einer Seite."""

    width_px: int
    height_px: int
    bits_per_pixel: int
    filters: tuple[str, ...]
    dpi: float | None  # effektive Auflösung in der Darstellung auf der Seite
    coverage: float  # Anteil an der Seitenfläche (0–1)


@dataclass(frozen=True)
class TextSpan:
    """Textabschnitt mit Rechteck in PDF-Punkten (Ursprung unten links)."""

    text: str
    left: float
    bottom: float
    right: float
    top: float


@dataclass
class PageAnalysis:
    """Messwerte einer Seite."""

    number: int  # ab 1
    width_pt: float
    height_pt: float
    rotation: int = 0
    path_objects: int = 0
    path_segments: int = 0
    text_objects: int = 0
    image_objects: int = 0
    form_objects: int = 0
    images: list[ImageInfo] = field(default_factory=list)
    text: str = ""
    spans: list[TextSpan] = field(default_factory=list)
    scale_denominators: list[float] = field(default_factory=list)
    geo_pdf: bool = False
    palette_share: float | None = None
    truncated: bool = False

    @property
    def width_mm(self) -> float:
        return self.width_pt * MM_PER_PT

    @property
    def height_mm(self) -> float:
        return self.height_pt * MM_PER_PT

    @property
    def paper_format(self) -> str:
        short, long = sorted((self.width_mm, self.height_mm))
        for name, (a, b) in PAPER_FORMATS.items():
            if abs(short - a) < 8 and abs(long - b) < 8:
                return name
        return f"{short:.0f}×{long:.0f} mm"

    @property
    def area_cm2(self) -> float:
        return max(self.width_pt * self.height_pt * CM2_PER_PT2, 1e-9)

    @property
    def vector_density(self) -> float:
        """Pfadsegmente je cm²."""
        return self.path_segments / self.area_cm2

    @property
    def image_coverage(self) -> float:
        return min(sum(image.coverage for image in self.images), 1.0)

    @property
    def bitonal_basemap(self) -> bool:
        """1-bit-Rastergrundkarte, wie sie AutoCAD-Map-Plots unter die Vektorebenen legen."""
        return sum(image.coverage for image in self.images if image.bits_per_pixel == 1) > 0.05

    @property
    def text_chars(self) -> int:
        return len(self.text.strip())

    @property
    def is_scan(self) -> bool:
        """Bildseite ohne Textlayer (Scan, Foto) – braucht OCR für Beschriftungen."""
        return self.image_coverage >= IMAGE_DOMINANT_COVERAGE and self.text_chars < 20

    @property
    def keyword_count(self) -> int:
        return len({match.group(0).lower() for match in MAP_KEYWORDS.finditer(self.text)})

    @property
    def coordinate_labels(self) -> int:
        return len(COORDINATE_PAIR.findall(self.text))

    @property
    def has_scale(self) -> bool:
        return bool(self.scale_denominators)

    @property
    def max_image_dpi(self) -> float | None:
        """Höchste effektive Auflösung der Bilder, die mindestens 1 % der Seite bedecken (auch Kacheln)."""
        values = [image.dpi for image in self.images if image.dpi and image.coverage >= 0.01]
        return max(values) if values else None


@dataclass
class DocumentAnalysis:
    """Messwerte eines Dokuments."""

    page_count: int
    pages: list[PageAnalysis]
    producer: str = ""
    creator: str = ""
    truncated: bool = False  # mehr Seiten als ausgewertet
    unreadable_pages: int = 0  # übersprungen, weil PDFium sie nicht lesen konnte


@dataclass(frozen=True)
class MapVerdict:
    is_map: bool
    score: int
    reasons: tuple[str, ...]


# =============================================================================
# Auswertung
# =============================================================================


def _open_pypdf_pages(data: bytes) -> list[Any] | None:
    """Seitenobjekte über pypdf – nur für ``/VP`` und ``/LGIDict``; Fehler sind nicht fatal."""
    try:
        from pypdf import PdfReader

        return list(PdfReader(io.BytesIO(data), strict=False).pages)
    except Exception:  # noqa: BLE001 – beschädigte Nebenstrukturen: dann eben ohne Maßstab
        logger.debug("pypdf konnte die Seitenverzeichnisse nicht lesen", exc_info=True)
        return None


def _resolve(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value


def _box(raw: Any) -> tuple[float, float, float, float] | None:
    values = [float(value) for value in _resolve(raw) or []]
    if len(values) != 4:
        return None
    return (min(values[0], values[2]), min(values[1], values[3]), max(values[0], values[2]), max(values[1], values[3]))


def _encloses(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> bool:
    tolerance = 2.0
    return (
        outer[0] - tolerance <= inner[0]
        and outer[1] - tolerance <= inner[1]
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def viewport_scales(page_dict: Any, width_pt: float, height_pt: float) -> tuple[list[float], bool]:
    """Maßstäbe (1:N) der Modell-Viewports und ob die Seite GeoPDF-Angaben trägt.

    AutoCAD schreibt je Viewport ``/Measure /Subtype /RL`` mit dem Umrechnungsfaktor ``/C`` (Meter je
    PDF-Punkt). Der Viewport des Papierbereichs (1 Punkt = 0,353 mm, rechnerisch 1:1000) ist kein
    Kartenmaßstab und entfällt: Er umschließt die Modell-Viewports bzw. füllt als einziger die Seite.
    """
    found: list[tuple[float, tuple[float, float, float, float] | None]] = []
    geo = False
    try:
        if page_dict.get("/LGIDict") is not None:
            geo = True
        for raw_viewport in _resolve(page_dict.get("/VP")) or []:
            viewport = _resolve(raw_viewport)
            measure = _resolve(viewport.get("/Measure"))
            if measure is None:
                continue
            subtype = str(measure.get("/Subtype", ""))
            if subtype == "/GEO":
                geo = True
                continue
            formats = _resolve(measure.get("/X")) or []
            if subtype != "/RL" or not formats:
                continue
            number_format = _resolve(formats[0])
            unit = str(number_format.get("/U", "")).strip().lower()
            meters_per_pt = float(number_format.get("/C", 0)) * UNIT_TO_METERS.get(unit, 1.0)
            if meters_per_pt > 0:
                found.append((meters_per_pt / METERS_PER_PT, _box(viewport.get("/BBox"))))
    except (AttributeError, TypeError, ValueError, KeyError, IndexError, PyPdfError):
        # pypdf löst indirekte Objekte erst beim Zugriff auf; defekte Verweise werfen dann PdfReadError
        logger.debug("Viewport-Angaben nicht lesbar", exc_info=True)
        return [], geo

    page_area = max(width_pt * height_pt, 1e-9)
    scales: list[float] = []
    for denominator, box in found:
        if 990 <= denominator <= 1010 and box is not None:
            others = [other for _, other in found if other is not None and other is not box]
            area = (box[2] - box[0]) * (box[3] - box[1])
            if (others and all(_encloses(box, other) for other in others)) or (not others and area / page_area >= 0.9):
                continue  # Papierbereich
        if 10 <= denominator <= 1_000_000:
            scales.append(round(denominator, 1))
    return scales, geo


def _image_dpi(width_px: int, height_px: int, width_pt: float, height_pt: float) -> float | None:
    if width_pt <= 1 or height_pt <= 1 or width_px <= 0 or height_px <= 0:
        return None
    px_long, px_short = sorted((width_px, height_px), reverse=True)
    in_long, in_short = sorted((width_pt / 72, height_pt / 72), reverse=True)
    return round(min(px_long / in_long, px_short / in_short), 1)


def _iter_objects(page: Any) -> Iterator[tuple[Any, Any]]:
    """Seitenobjekte samt Matrix in den Seitenraum (auch in Form-XObjects)."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_raw

    matrices: dict[int, Any] = {0: pdfium.PdfMatrix()}
    for obj in page.get_objects(max_depth=MAX_FORM_DEPTH):
        level = int(getattr(obj, "level", 0))
        matrix = matrices.get(level, matrices[0])
        if obj.type == pdfium_raw.FPDF_PAGEOBJ_FORM:
            matrices[level + 1] = obj.get_matrix().multiply(matrix)
        yield obj, matrix


def _measure_objects(page: Any, result: PageAnalysis) -> None:
    import pypdfium2.raw as pdfium_raw

    page_area = max(result.width_pt * result.height_pt, 1e-9)
    for count, (obj, matrix) in enumerate(_iter_objects(page), start=1):
        if count > MAX_OBJECTS_PER_PAGE:
            result.truncated = True
            return
        kind = obj.type
        if kind == pdfium_raw.FPDF_PAGEOBJ_PATH:
            result.path_objects += 1
            result.path_segments += max(int(pdfium_raw.FPDFPath_CountSegments(obj.raw)), 0)
        elif kind == pdfium_raw.FPDF_PAGEOBJ_TEXT:
            result.text_objects += 1
        elif kind == pdfium_raw.FPDF_PAGEOBJ_FORM:
            result.form_objects += 1
        elif kind == pdfium_raw.FPDF_PAGEOBJ_IMAGE:
            result.image_objects += 1
            result.images.append(_image_info(obj, matrix, page_area))


def _image_info(obj: Any, matrix: Any, page_area: float) -> ImageInfo:
    width_px, height_px = (int(value) for value in obj.get_px_size())
    left, bottom, right, top = matrix.on_rect(*obj.get_bounds())
    width_pt = abs(right - left)
    height_pt = abs(top - bottom)
    try:
        bits = int(obj.get_metadata().bits_per_pixel)
    except Exception:  # noqa: BLE001 – Metadaten fehlen bei manchen Inline-Bildern
        bits = 0
    try:
        filters = tuple(str(name) for name in obj.get_filters())
    except Exception:  # noqa: BLE001
        filters = ()
    return ImageInfo(
        width_px=width_px,
        height_px=height_px,
        bits_per_pixel=bits,
        filters=filters,
        dpi=_image_dpi(width_px, height_px, width_pt, height_pt),
        coverage=round(min(width_pt * height_pt / page_area, 1.0), 4),
    )


def _read_text(page: Any, result: PageAnalysis, with_spans: bool) -> None:
    textpage = page.get_textpage()
    try:
        result.text = textpage.get_text_range() or ""
        if with_spans:
            for index in range(textpage.count_rects()):
                left, bottom, right, top = textpage.get_rect(index)
                text = (textpage.get_text_bounded(left, bottom, right, top) or "").strip()
                if text:
                    result.spans.append(TextSpan(text, left, bottom, right, top))
    finally:
        textpage.close()


def palette_share(page: Any, dpi: int = PALETTE_DPI) -> float:
    """Anteil der acht häufigsten von 64 Farben in einer groben Vorschau (Karten: wenige Farbtöne)."""
    bitmap = page.render(scale=dpi / 72)
    try:
        image = bitmap.to_pil().convert("RGB").quantize(64)
        histogram = sorted(image.histogram()[:64], reverse=True)
        total = sum(histogram)
        return round(sum(histogram[:8]) / total, 3) if total else 0.0
    finally:
        bitmap.close()


def analyze_pdf(
    source: str | Path | bytes,
    *,
    max_pages: int = MAX_PAGES,
    with_spans: bool = False,
    with_palette: bool = True,
) -> DocumentAnalysis:
    """Alle Seiten eines PDFs messen (höchstens ``max_pages``); wirft ``DocumentError``.

    Eine einzelne Seite, die PDFium nicht lesen kann, wird übersprungen und in ``unreadable_pages``
    gezählt; erst wenn keine Seite lesbar ist, gilt das ganze Dokument als nicht lesbar.
    """
    import pypdfium2 as pdfium

    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        raise DocumentError("PDF lässt sich nicht öffnen") from None
    try:
        page_count = len(pdf)
        try:
            metadata = pdf.get_metadata_dict()
        except pdfium.PdfiumError:
            metadata = {}
        pypdf_pages = _open_pypdf_pages(data)
        pages: list[PageAnalysis] = []
        unreadable = 0
        for index in range(min(page_count, max_pages)):
            pypdf_page = pypdf_pages[index] if pypdf_pages is not None and index < len(pypdf_pages) else None
            try:
                pages.append(_analyze_page(pdf, index, pypdf_page, with_spans=with_spans, with_palette=with_palette))
            except pdfium.PdfiumError:
                logger.debug("Seite %s nicht lesbar, übersprungen", index + 1, exc_info=True)
                unreadable += 1
        if unreadable and not pages:
            raise DocumentError("Keine Seite des PDFs lesbar")
        return DocumentAnalysis(
            page_count=page_count,
            pages=pages,
            producer=str(metadata.get("Producer") or "")[:200],
            creator=str(metadata.get("Creator") or "")[:200],
            truncated=page_count > max_pages,
            unreadable_pages=unreadable,
        )
    finally:
        pdf.close()


def _analyze_page(pdf: Any, index: int, pypdf_page: Any, *, with_spans: bool, with_palette: bool) -> PageAnalysis:
    """Eine Seite messen; PDFium-Fehler (``PdfiumError``) gehen an den Aufrufer."""
    import pypdfium2 as pdfium

    page = pdf[index]
    try:
        width_pt, height_pt = page.get_size()
        result = PageAnalysis(number=index + 1, width_pt=width_pt, height_pt=height_pt, rotation=page.get_rotation())
        _measure_objects(page, result)
        _read_text(page, result, with_spans)
        if pypdf_page is not None:
            result.scale_denominators, result.geo_pdf = viewport_scales(pypdf_page, width_pt, height_pt)
        if with_palette and result.image_coverage >= IMAGE_DOMINANT_COVERAGE:
            try:
                result.palette_share = palette_share(page)
            except (pdfium.PdfiumError, OSError, ValueError):
                # Vorschau nicht darstellbar: Seite bleibt ausgewertet, nur ohne Farbpalette
                logger.debug("Vorschau der Seite %s nicht darstellbar", index + 1, exc_info=True)
        return result
    finally:
        page.close()


def classify_page(page: PageAnalysis, street_count: int = 0) -> MapVerdict:
    """Karte bzw. Plan ja/nein nach Punkten (Regeln Stufe C des Machbarkeitstests, ab 3 Punkten)."""
    score = 0
    reasons: list[str] = []

    def add(points: int, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"{points:+d} {reason}")

    vector = page.path_segments >= VECTOR_MIN_SEGMENTS and page.vector_density >= VECTOR_MIN_DENSITY
    if vector:
        add(2, "Vektorzeichnung")
    if page.has_scale:
        add(2, "Maßstab im PDF (/VP)")
    if page.geo_pdf:
        add(3, "GeoPDF")
    if max(page.width_mm, page.height_mm) > 310:
        add(1, "größer als A4")
    keywords = page.keyword_count
    if keywords >= 2:
        add(1, "Kartenstichworte")
    if keywords >= 4:
        add(1, "viele Kartenstichworte")
    if street_count >= 2:
        add(1, "Straßennamen")
    if page.text_chars > TEXT_HEAVY_CHARS and page.vector_density < 1:
        add(-3, "Textseite")
    if page.image_coverage >= IMAGE_DOMINANT_COVERAGE:
        few_colors = page.palette_share is not None and page.palette_share >= PALETTE_MAP_SHARE
        if few_colors or page.bitonal_basemap or vector or page.has_scale:
            add(2, "Bildseite wie eine Karte")
        else:
            add(-2, "Bildseite wie ein Foto")
    return MapVerdict(score >= MAP_SCORE_THRESHOLD, score, tuple(reasons))


def format_scale(denominator: float) -> str:
    """1:N lesbar auf drei gültige Stellen, etwa ``1:500``, ``1:5.000`` oder ``1:14.300``."""
    digits = max(int(math.floor(math.log10(max(denominator, 1.0)))) - 2, 0)
    value = int(round(denominator / 10**digits) * 10**digits)
    return "1:" + f"{value:,}".replace(",", ".")

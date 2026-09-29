# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seitenanalyse von PDFs (Issue #599): Messwerte je Seite und Einordnung als Karte bzw. Plan.

Die PDFs entstehen im Test (``pdf_samples``); nichts kommt aus dem Netz.
"""

from __future__ import annotations

import io
from typing import Any

import pypdfium2 as pdfium
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import DictionaryObject
from reportlab.lib.pagesizes import A1, A4

from apps.common.documents import page_analysis
from apps.common.documents.page_analysis import (
    DocumentError,
    analyze_pdf,
    classify_page,
    format_scale,
    viewport_scales,
)
from apps.common.tests.pdf_samples import map_pdf, photo_pdf, text_pdf, viewport_dict, with_page_entries


def _two_pages() -> bytes:
    writer = PdfWriter()
    for data in (text_pdf(), map_pdf()):
        writer.append(PdfReader(io.BytesIO(data)))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_map_page_measures_and_classification() -> None:
    document = analyze_pdf(map_pdf(), with_spans=True)
    assert document.page_count == 1 and not document.truncated
    page = document.pages[0]

    assert page.paper_format == "A4"
    assert page.path_objects >= 4000 and page.path_segments >= 8000
    assert page.vector_density >= 10
    assert page.scale_denominators == [pytest.approx(500, abs=0.5)]  # Papierbereich entfällt
    assert page.keyword_count >= 4
    assert page.coordinate_labels == 1
    assert "Maßstab" in page.text
    span = next(item for item in page.spans if "Lageplan" in item.text)
    assert 30 < span.left < 60 and 40 < span.bottom < 70  # Position in PDF-Punkten

    verdict = classify_page(page)
    assert verdict.is_map and verdict.score >= 5
    assert "+2 Maßstab im PDF (/VP)" in verdict.reasons


def test_text_page_is_no_map() -> None:
    page = analyze_pdf(text_pdf()).pages[0]
    assert page.text_chars > 1500 and page.path_segments == 0
    verdict = classify_page(page)
    assert not verdict.is_map and "-3 Textseite" in verdict.reasons


def test_photo_page_is_scan_without_text_and_no_map() -> None:
    page = analyze_pdf(photo_pdf()).pages[0]
    assert page.image_coverage == pytest.approx(1.0, abs=0.01)
    assert page.is_scan
    image = page.images[0]
    assert (image.width_px, image.height_px) == (300, 400)
    # Effektive Auflösung: die schwächere Achse zählt (400 px auf 11,7 Zoll Höhe ≈ 34 dpi)
    assert image.dpi == pytest.approx(min(400 / (A4[1] / 72), 300 / (A4[0] / 72)), rel=0.02)
    assert page.palette_share is not None and page.palette_share < 0.8
    assert not classify_page(page).is_map


def test_large_format_and_street_names_add_points() -> None:
    page = analyze_pdf(photo_pdf(size=A1)).pages[0]
    assert page.paper_format == "A1"
    assert "+1 größer als A4" in classify_page(page).reasons
    few_lines = analyze_pdf(map_pdf(viewport=False, lines=10, text="Lageplan")).pages[0]
    assert classify_page(few_lines, street_count=0).score + 1 == classify_page(few_lines, street_count=3).score


def test_geopdf_marker_counts() -> None:
    data = with_page_entries(map_pdf(viewport=False, lines=10, text="Anlage"), {"/LGIDict": DictionaryObject()})
    page = analyze_pdf(data).pages[0]
    assert page.geo_pdf
    assert "+3 GeoPDF" in classify_page(page).reasons


def test_viewport_scales_skip_paper_space_and_read_units() -> None:
    page = {"/VP": [viewport_dict([0, 0, 595, 842], 0.35278)]}
    assert viewport_scales(page, 595, 842) == ([], False)
    # Einziger Viewport 1:1000 über einen Teil der Seite zählt
    page = {"/VP": [viewport_dict([100, 100, 300, 300], 0.35278)]}
    assert viewport_scales(page, 595, 842)[0] == [pytest.approx(1000, abs=0.5)]
    assert viewport_scales({}, 595, 842) == ([], False)


def test_unreadable_pdf_raises_document_error() -> None:
    with pytest.raises(DocumentError):
        analyze_pdf(b"kein PDF")


def test_unreadable_page_is_skipped_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein PDFium-Fehler auf einer Seite beendet nicht die Auswertung des Dokuments."""
    read_text = page_analysis._read_text
    calls: list[int] = []

    def _first_page_breaks(page: Any, result: Any, with_spans: bool) -> None:
        calls.append(result.number)
        if result.number == 1:
            raise pdfium.PdfiumError("Seite kaputt")
        read_text(page, result, with_spans)

    monkeypatch.setattr(page_analysis, "_read_text", _first_page_breaks)
    document = analyze_pdf(_two_pages())

    assert calls == [1, 2]
    assert document.page_count == 2 and document.unreadable_pages == 1
    assert [page.number for page in document.pages] == [2]
    assert classify_page(document.pages[0]).is_map


def test_document_without_readable_page_raises_document_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(*args: Any, **kwargs: Any) -> None:
        raise pdfium.PdfiumError("kaputt")

    monkeypatch.setattr(page_analysis, "_measure_objects", _broken)
    with pytest.raises(DocumentError):
        analyze_pdf(map_pdf())


def test_broken_preview_keeps_page_without_palette(monkeypatch: pytest.MonkeyPatch) -> None:
    def _broken(*args: Any, **kwargs: Any) -> float:
        raise pdfium.PdfiumError("Vorschau kaputt")

    monkeypatch.setattr(page_analysis, "palette_share", _broken)
    document = analyze_pdf(photo_pdf())
    assert document.unreadable_pages == 0
    assert document.pages[0].is_scan and document.pages[0].palette_share is None


def test_viewport_scales_tolerate_broken_indirect_objects() -> None:
    """pypdf löst Verweise erst beim Zugriff auf; ein defekter Verweis kostet nur den Maßstab."""

    class _BrokenPage(dict):  # type: ignore[type-arg]
        def get(self, key: Any, default: Any = None) -> Any:
            if key == "/VP":
                raise PdfReadError("Verweis zeigt ins Leere")
            return super().get(key, default)

    assert viewport_scales(_BrokenPage(), 595, 842) == ([], False)


@pytest.mark.parametrize(
    ("denominator", "text"),
    [(500.0, "1:500"), (4999.4, "1:5.000"), (14284.0, "1:14.300"), (2141.0, "1:2.140"), (99.96, "1:100")],
)
def test_format_scale(denominator: float, text: str) -> None:
    assert format_scale(denominator) == text

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beispiel-PDFs für Tests der Seitenanalyse (Issue #599): Plan mit Maßstab im ``/VP``, Textseite, Foto.

Entstehen mit reportlab; Zusatzeinträge im Seitenverzeichnis (``/VP``, ``/LGIDict``) setzt pypdf.
"""

from __future__ import annotations

import io
import random

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

MAP_TEXT = "Lageplan  Maßstab 1:500  Legende  Flurstück 12  Gemarkung Gievenbeck"
LOREM = (
    "Der Rat der Stadt beschließt die Aufstellung des Bebauungsplans. Die Verwaltung wird beauftragt, "
    "die frühzeitige Beteiligung der Öffentlichkeit durchzuführen und die Stellungnahmen auszuwerten. "
)


def viewport_dict(bbox: list[float], meters_per_pt: float) -> DictionaryObject:
    number_format = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/NumberFormat"),
            NameObject("/U"): TextStringObject(" "),
            NameObject("/C"): FloatObject(meters_per_pt),
        }
    )
    measure = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Measure"),
            NameObject("/Subtype"): NameObject("/RL"),
            NameObject("/X"): ArrayObject([number_format]),
        }
    )
    return DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Viewport"),
            NameObject("/BBox"): ArrayObject([FloatObject(value) for value in bbox]),
            NameObject("/Measure"): measure,
        }
    )


def with_page_entries(data: bytes, entries: dict[str, object]) -> bytes:
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(data)))
    for key, value in entries.items():
        writer.pages[0][NameObject(key)] = value
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def map_pdf(*, viewport: bool = True, lines: int = 4000, text: str = MAP_TEXT) -> bytes:
    """A4-Plan: viele kurze Linien (Vektorzeichnung), Kartenstichworte, Koordinate, Maßstab im /VP."""
    rng = random.Random(1)
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    for _ in range(lines):
        x, y = rng.uniform(30, width - 30), rng.uniform(80, height - 30)
        pdf.line(x, y, x + rng.uniform(-6, 6), y + rng.uniform(-6, 6))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(40, 50, text)
    pdf.drawString(40, 38, "UTM-Koordinate 401962,85/5757789,39")
    pdf.showPage()
    pdf.save()
    data = buffer.getvalue()
    if not viewport:
        return data
    # Papierbereich (ganze Seite, 1 Punkt = 0,353 mm) und Modell-Viewport 1:500 wie in AutoCAD-Plots
    viewports = ArrayObject([viewport_dict([0, 0, width, height], 0.35278), viewport_dict([30, 80, 560, 810], 0.17639)])
    return with_page_entries(data, {"/VP": viewports})


def text_pdf() -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setFont("Helvetica", 10)
    y = 800.0
    for _ in range(18):
        for line in range(0, len(LOREM), 95):
            pdf.drawString(40, y, LOREM[line : line + 95])
            y -= 12
        if y < 60:
            break
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def photo_pdf(*, size: tuple[float, float] = A4) -> bytes:
    """Ganzseitiges Foto (Rauschen, viele Farben), kein Textlayer."""
    rng = random.Random(2)
    image = Image.new("RGB", (300, 400))
    image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(300 * 400)])
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=size)
    pdf.drawImage(ImageReader(image), 0, 0, width=size[0], height=size[1])
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()

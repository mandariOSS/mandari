# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gemeinsame PDF-Bausteine (Issue #29).

Generalisiert die Briefkopf-/PDF-Mechanik aus apps/work/motions/ für die
Wiederverwendung in anderen Apps (Session: Einladungen, Niederschriften,
Beschlussauszüge; Work: Antrags-Export).

Bausteine:
- html_to_pdf: HTML → PDF (xhtml2pdf, Fallback: reportlab-Minimal-PDF)
- overlay_pdf_letterhead: erzeugtes PDF auf ein Briefkopf-PDF legen
"""

import contextlib
import io
import logging
import re
import threading
import time
import zlib
from typing import Any

from apps.common.metrics import PDF_DOCUMENTS, PDF_DURATION

logger = logging.getLogger(__name__)

# Standard-Ränder in mm (A4)
DEFAULT_MARGINS = {"top": 30, "right": 25, "bottom": 25, "left": 25}


#: Grenzen der Umwandlung. Dokumente mit eingebetteten Bildern (``data:``) dürfen groß sein, deshalb großzügiger
#: als die Vorgaben der Bibliothek; die Pixelgrenze je Bild bleibt bei deren Vorgabe.
MAX_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_RENDER_SECONDS = 300


def _resource_policy() -> Any:
    """
    Richtlinie der Bibliothek für Dateien und Adressen im HTML: nichts außerhalb des Dokuments.

    Das HTML enthält Text aus dem Editor. Ohne Richtlinie läse die Bibliothek für ``<img src>`` Dateien unterhalb
    des Arbeitsverzeichnisses (dort liegt die Medienablage aller Mandanten) und riefe fremde Server ab, beides
    während sie die prozessweite Sperre der Umwandlung hält. Deshalb: keine lokalen Dateien, keine Fernabrufe,
    dazu Grenzen für Bildgröße, Dokumentgröße und Dauer. Bilder stehen als ``data:``-Adresse im HTML; wer Dateien
    oder Bilder fremder Server braucht, bettet sie vorher ein (``apps.work.pdf_bilder``).
    """
    from xhtml2pdf.config.resources import ResourceAccessPolicy

    return ResourceAccessPolicy.server(
        base_dir=None,
        allow_remote=False,
        max_document_bytes=MAX_DOCUMENT_BYTES,
        max_total_bytes=MAX_TOTAL_BYTES,
        max_render_seconds=MAX_RENDER_SECONDS,
    )


def html_to_pdf(html_content: str) -> bytes:
    """
    HTML in ein PDF umwandeln (xhtml2pdf, Fallback: reportlab).

    Zentrale Stelle für Einladungen, Niederschriften, Beschlussauszüge, Exporte und
    Bescheinigungen – deshalb werden hier Anzahl und Dauer als Metrik erfasst (Issue #231).
    Dateien und fremde Adressen im HTML werden nicht gelesen (``_resource_policy``).

    Args:
        html_content: vollständiges HTML-Dokument

    Returns:
        bytes: PDF-Inhalt
    """
    start = time.perf_counter()
    try:
        try:
            from xhtml2pdf import pisa

            result = io.BytesIO()
            pisa_status = pisa.CreatePDF(
                src=html_content, dest=result, encoding="UTF-8", resource_policy=_resource_policy()
            )
            if pisa_status.err:
                raise RuntimeError(f"PDF generation error: {pisa_status.err}")
            pdf = result.getvalue()
        except ImportError:
            pdf = simple_text_pdf(html_content)
    except Exception:
        PDF_DOCUMENTS.labels(result="failed").inc()
        raise
    PDF_DURATION.observe(time.perf_counter() - start)
    PDF_DOCUMENTS.labels(result="ok").inc()
    return pdf


def simple_text_pdf(html_content: str, margins: dict | None = None) -> bytes:
    """
    Fallback: schlichtes Text-PDF mit reportlab (HTML-Tags werden entfernt).
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    m = {**DEFAULT_MARGINS, **(margins or {})}
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=m["left"] * mm,
        rightMargin=m["right"] * mm,
        topMargin=m["top"] * mm,
        bottomMargin=m["bottom"] * mm,
    )

    styles = getSampleStyleSheet()
    text = re.sub(r"<[^>]+>", "", html_content)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")

    story = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            story.append(Paragraph(line, styles["Normal"]))
            story.append(Spacer(1, 6))

    doc.build(story)
    return buffer.getvalue()


#: Größte entpackte Länge des Seiteninhalts eines Briefkopfs (Summe seiner Inhaltsströme). Das Zusammenführen liest
#: den Seiteninhalt ganz in den Speicher und belegt dabei ein Vielfaches seiner Länge. Ein Briefkopf zeichnet Logo,
#: Linien und Adresszeilen; sein Seiteninhalt ist wenige Kilobyte lang, Bilder liegen außerhalb davon.
LETTERHEAD_MAX_CONTENT_BYTES = 8 * 1024 * 1024

#: Je Prozess führt nur ein Export zugleich einen Briefkopf zusammen: Der Speicherbedarf mehrerer gleichzeitiger
#: Exporte addiert sich sonst in dem Prozess, den alle Mandanten teilen.
_LETTERHEAD_LOCK = threading.Lock()


def _flate_length(data: bytes, limit: int) -> int | None:
    """Entpackte Länge eines Flate-Stroms, gezählt in Stücken und höchstens bis knapp über ``limit``."""
    unpacker = zlib.decompressobj()
    length = 0
    try:
        while data and length <= limit:
            length += len(unpacker.decompress(data, 64 * 1024))
            data = unpacker.unconsumed_tail
    except zlib.error:
        return None
    return length


def _stream_length(data: bytes, filters: list[str], limit: int) -> int | None:
    """
    Entpackte Länge eines Stroms mit seiner Filterkette; ``None``, wenn sie sich nicht in Stücken zählen lässt.

    Gezählt wird, was PDF-Programme für Seiteninhalte schreiben: unkomprimiert oder Flate, davor wahlweise eine
    Textkodierung (ASCII85, ASCIIHex). Die Textkodierungen vergrößern höchstens auf das Vierfache und werden nur bis
    zu einer Eingabe in Höhe der Grenze gelesen; andere Kodierungen ließen sich nur durch vollständiges Entpacken
    messen.
    """
    from pypdf.filters import ASCII85Decode, ASCIIHexDecode

    text_filters = {"/ASCII85Decode": ASCII85Decode, "/ASCIIHexDecode": ASCIIHexDecode}
    for position, name in enumerate(filters):
        if name in text_filters:
            if len(data) > limit:
                return len(data)
            try:
                data = bytes(text_filters[name].decode(data))
            except Exception:  # noqa: BLE001 – beschädigter Strom: nicht messbar
                return None
        elif name == "/FlateDecode" and position == len(filters) - 1:
            return _flate_length(data, limit)
        else:
            return None
    return len(data)


def _page_content_length(page: Any, limit: int) -> int | None:
    """Entpackte Länge des Seiteninhalts, ohne ihn im Speicher aufzubauen; ``None``, wenn nicht bestimmbar."""
    from pypdf.generic import ArrayObject, StreamObject

    contents = page.get("/Contents")
    if contents is None:
        return 0
    contents = contents.get_object()
    streams = [entry.get_object() for entry in contents] if isinstance(contents, ArrayObject) else [contents]
    total = 0
    for stream in streams:
        data = getattr(stream, "_data", None)
        if not isinstance(stream, StreamObject) or not isinstance(data, bytes):
            return None
        filters = stream.get("/Filter")
        # Ein einzelner Filter steht als Name oder als Liste mit einem Eintrag im Strom
        names = [str(name) for name in filters] if isinstance(filters, ArrayObject) else [str(filters)] * bool(filters)
        length = _stream_length(data, names, limit - total)
        if length is None:
            return None
        total += length
        if total > limit:
            break
    return total


def _read_from_start(datei: Any) -> bytes:
    """Den ganzen Inhalt eines dateiartigen Objekts lesen, unabhängig davon, wo es gerade steht."""
    # Nicht jede Ablage kann zurückspringen; ein frisch geöffnetes Objekt steht ohnehin am Anfang
    with contextlib.suppress(AttributeError, OSError, ValueError):
        datei.seek(0)
    return bytes(datei.read())


def overlay_pdf_letterhead(pdf_content: bytes, letterhead_pdf_file) -> bytes:
    """
    Erzeugtes PDF seitenweise auf die erste Seite eines Briefkopf-PDFs legen.

    Den Briefkopf lädt die Organisation hoch. Sein Seiteninhalt darf entpackt höchstens
    ``LETTERHEAD_MAX_CONTENT_BYTES`` lang sein, sonst bleibt es beim Inhalts-PDF; geprüft wird hier und nicht nur
    beim Hochladen, weil ältere Briefköpfe keine Prüfung durchlaufen haben.

    Args:
        pdf_content: das erzeugte Inhalts-PDF
        letterhead_pdf_file: dateiartiges Objekt des Briefkopf-PDFs

    Returns:
        bytes: zusammengeführtes PDF (bei Fehlern: Inhalts-PDF unverändert)
    """
    try:
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import FloatObject, NameObject

        # Aus der Ablage lesen, bevor die Sperre gilt: Ein langsamer Abruf (Objektspeicher) hielte sonst die Exporte
        # aller anderen im selben Prozess auf. Unter der Sperre steht nur noch das Zusammenführen im Speicher.
        letterhead_bytes = _read_from_start(letterhead_pdf_file)

        with _LETTERHEAD_LOCK:
            content_pdf = PdfReader(io.BytesIO(pdf_content))
            letterhead_pdf = PdfReader(io.BytesIO(letterhead_bytes))
            letterhead_page = letterhead_pdf.pages[0]

            length = _page_content_length(letterhead_page, LETTERHEAD_MAX_CONTENT_BYTES)
            if length is None or length > LETTERHEAD_MAX_CONTENT_BYTES:
                logger.warning("Briefkopf nicht anwendbar: Seiteninhalt zu groß oder nicht prüfbar")
                return pdf_content

            output = PdfWriter()
            for content_page in content_pdf.pages:
                # Je Inhaltsseite ein eigenes leeres Blatt im Writer, darauf erst der Briefkopf, dann der Inhalt.
                # Eine Kopie der Briefkopf-Seite außerhalb des Writers lässt pypdf 7 nicht mehr zusammenführen, und
                # pypdf 6 schrieb dabei den Inhalt der ersten Seite auf alle Seiten. Ein Blatt aus
                # add_page(letterhead_page) teilt seinen Seiteninhalt mit den übrigen: Dann stünde jeder Inhalt auf
                # jeder Seite (#1464).
                mediabox = letterhead_page.mediabox
                new_page = output.add_blank_page(width=mediabox.width, height=mediabox.height)
                new_page.mediabox = mediabox
                new_page.cropbox = letterhead_page.cropbox
                if letterhead_page.rotation:
                    new_page.rotation = letterhead_page.rotation
                if "/UserUnit" in letterhead_page:
                    new_page[NameObject("/UserUnit")] = FloatObject(float(letterhead_page["/UserUnit"]))
                new_page.merge_page(letterhead_page)
                new_page.merge_page(content_page)

            result = io.BytesIO()
            output.write(result)
            return result.getvalue()
    except Exception as e:
        # Nur die Art: Der Text stammt aus dem hochgeladenen Briefkopf-PDF (Namen aus der Datei, auch Zeilenumbrüche)
        logger.warning("Briefkopf nicht anwendbar: %s", type(e).__name__)
        return pdf_content

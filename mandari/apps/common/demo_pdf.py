# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kleine, echte PDF-Dokumente für die Demo-Umgebung (Drucksachen, Einladungen, Niederschriften).

Die Texte stehen zusätzlich als ``text_content`` in der Datenbank, eine Texterkennung ist nicht nötig. Lange
Zeilen werden umbrochen, lange Dokumente bekommen weitere Seiten. Jede Seite trägt den Hinweis, dass es sich um
ein synthetisches Musterdokument handelt.
"""

from __future__ import annotations

import textwrap
from io import BytesIO

FUSSZEILE = "Mandari Demo-Umgebung - synthetisches Musterdokument, kein amtliches Dokument."
#: Zeichen je Zeile bei 10 pt Helvetica auf A4 mit 50 pt Rand
ZEICHEN_JE_ZEILE = 95


def _minimal() -> bytes:
    """Gültiges leeres PDF, falls reportlab fehlt."""
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n0\n%%EOF\n"
    )


def demo_pdf(titel: str, zeilen: list[str], *, kopf: str = "") -> tuple[bytes, int]:
    """PDF mit Titel und Textzeilen erzeugen; liefert (Bytes, Seitenzahl)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
    except ImportError:  # pragma: no cover - reportlab ist eine feste Abhängigkeit
        return _minimal(), 1

    puffer = BytesIO()
    pdf = canvas.Canvas(puffer, pagesize=A4)
    _breite, hoehe = A4
    pdf.setTitle(titel)
    seiten = 1

    def seitenkopf() -> float:
        if kopf:
            pdf.setFont("Helvetica", 8)
            pdf.drawString(50, hoehe - 35, kopf)
        pdf.setFont("Helvetica-Oblique", 8)
        pdf.drawString(50, 40, FUSSZEILE)
        pdf.drawRightString(545, 40, f"Seite {seiten}")
        return float(hoehe) - 60

    y = seitenkopf()
    pdf.setFont("Helvetica-Bold", 14)
    for teil in textwrap.wrap(titel, 70) or [""]:
        pdf.drawString(50, y, teil)
        y -= 18
    y -= 10
    for zeile in zeilen:
        fett = bool(zeile) and len(zeile) < 60 and not zeile.endswith((".", ":", ")")) and zeile[0].isupper()
        for teil in textwrap.wrap(zeile, ZEICHEN_JE_ZEILE) or [""]:
            if y < 70:
                pdf.showPage()
                seiten += 1
                y = seitenkopf()
            pdf.setFont("Helvetica-Bold" if fett else "Helvetica", 10)
            pdf.drawString(50, y, teil)
            y -= 14
    pdf.showPage()
    pdf.save()
    return puffer.getvalue(), seiten

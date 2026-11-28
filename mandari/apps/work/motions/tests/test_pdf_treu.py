# SPDX-License-Identifier: AGPL-3.0-or-later
"""Treue PDF-Übernahme (Issue #1600): Hintergrund, Fließtext mit Satzmaßen, Seitenzahl, Briefpapier des Dokuments."""

from __future__ import annotations

import io
import re
from typing import Any

import pypdfium2 as pdfium
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from apps.common.pdf import overlay_pdf_letterhead
from apps.work.motions import pdf_treu
from apps.work.motions.import_service import MotionImportService
from apps.work.motions.models import Motion, OrganizationLetterhead
from apps.work.motions.satz import satz_konfig
from apps.work.sanitize import sanitize_editor_html

LINKS = 72.0
ABSATZ = (
    "Die Verwaltung wird beauftragt, auf der städtischen Brachfläche an der Ecke Musterweg einen inklusiven "
    "Spielplatz zu planen und dem Ausschuss für Bauen und Verkehr einen Entwurf mit Kostenschätzung vorzulegen. "
    "Kinder und Eltern aus dem Viertel werden an der Planung beteiligt und frühzeitig informiert."
)


def _zeilenumbruch(text: str, breite: float, schrift: str, groesse: float) -> list[str]:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    zeilen: list[str] = []
    aktuell = ""
    for wort in text.split():
        probe = f"{aktuell} {wort}".strip()
        if stringWidth(probe, schrift, groesse) > breite and aktuell:
            zeilen.append(aktuell)
            aktuell = wort
        else:
            aktuell = probe
    zeilen.append(aktuell)
    return zeilen


def _antrag(seiten: int = 2) -> bytes:
    """Antrag wie aus Word: Logo oben, Überschrift, Absätze, nummerierte Liste mit Tabulator, Unterstreichung, Seitenzahl."""
    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    breite, hoehe = A4
    for seite in range(seiten):
        if seite == 0:
            leinwand.setFillColorRGB(0.2, 0.3, 0.8)
            leinwand.rect(400, hoehe - 120, 120, 60, fill=1, stroke=0)  # „Logo“
            leinwand.setFillColorRGB(0, 0, 0)
        leinwand.setFont("Helvetica", 10)
        leinwand.drawCentredString(breite / 2, hoehe - 40, f"- {seite + 1} -")
        y = hoehe - 170
        leinwand.setFont("Helvetica-Bold", 14)
        leinwand.drawString(LINKS, y, "Antrag: Inklusiver Spielplatz" if seite == 0 else "Begründung")
        y -= 30
        leinwand.setFont("Helvetica", 11)
        for zeile in _zeilenumbruch(ABSATZ, breite - 2 * LINKS, "Helvetica", 11):
            leinwand.drawString(LINKS, y, zeile)
            y -= 14
        y -= 10
        # Liste: „1.“ bei 90, Text bei 108 (hängender Einzug wie Word)
        for nummer in (1, 2):
            zeilen = _zeilenumbruch(ABSATZ, breite - LINKS - 108, "Helvetica", 11)
            leinwand.drawString(90, y, f"{nummer}.")
            for zeile in zeilen:
                leinwand.drawString(108, y, zeile)
                y -= 14
            y -= 4
        y -= 10
        leinwand.drawString(LINKS, y, "Gez. Musterfraktion")
        unterstrichen = "Begründung"
        y -= 24
        leinwand.drawString(LINKS, y, unterstrichen)
        from reportlab.pdfbase.pdfmetrics import stringWidth

        leinwand.setLineWidth(0.6)
        leinwand.line(LINKS, y - 1.5, LINKS + stringWidth(unterstrichen, "Helvetica", 11), y - 1.5)
        leinwand.showPage()
    leinwand.save()
    return puffer.getvalue()


def _text(pdf: bytes) -> str:
    dokument = pdfium.PdfDocument(pdf)
    return "\n".join(dokument[i].get_textpage().get_text_range() for i in range(len(dokument)))


def test_uebernahme_trennt_hintergrund_und_fliesstext() -> None:
    ergebnis = pdf_treu.uebernehmen(_antrag())
    html = sanitize_editor_html(ergebnis.html)

    # Fließtext mit Satzmaßen, die die Positivliste stehen lässt
    assert "Inklusiver Spielplatz" in html and "Gez. Musterfraktion" in html
    assert re.search(r'style="[^"]*line-height: [\d.]+pt', html)
    assert re.search(r'style="[^"]*margin-top: [\d.]+pt', html)
    assert "<strong>" in html
    assert "<u>" in html and "Begründung</u>" in html
    # Liste: Zeichen, Tabulator und hängender Einzug wie im Original
    assert "1.\t" in html
    assert re.search(r"padding-left: 3[56](?:\.\d+)?pt; text-indent: -1[78](?:\.\d+)?pt; tab-size: 1[78]", html)

    # Seitenzahl wird zur Vorlage an der Stelle des Originals, nicht zu Fließtext
    assert ergebnis.seitenzahl is not None and ergebnis.seitenzahl["text"] == "- {n} -"
    assert ergebnis.seitenzahl["ausrichtung"] == "mitte"
    assert "- 1 -" not in html

    # Ränder: linker Rand des Satzspiegels wie im Original (72 pt = 25,4 mm)
    assert ergebnis.raender["links"] == pytest.approx(25.2, abs=0.4)
    assert ergebnis.seiten == 2 and ergebnis.hintergrund_seiten == [0, 1]

    # Hintergrund: Logo bleibt, Text und Seitenzahl sind entfernt
    hintergrund = pdfium.PdfDocument(ergebnis.hintergrund_pdf)
    assert len(hintergrund) == 2
    assert "Spielplatz" not in _text(ergebnis.hintergrund_pdf)
    assert "- 1 -" not in _text(ergebnis.hintergrund_pdf)
    bilder = [o for o in hintergrund[0].get_objects() if o.type == pdfium.raw.FPDF_PAGEOBJ_PATH]
    assert bilder, "Das Logo (Fläche) gehört zum Hintergrund"


def test_ohne_textebene_nicht_geeignet() -> None:
    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    leinwand.rect(100, 100, 200, 200, fill=1)
    leinwand.showPage()
    leinwand.save()
    with pytest.raises(pdf_treu.NichtGeeignetError):
        pdf_treu.uebernehmen(puffer.getvalue())


def test_kaputte_datei_nicht_geeignet() -> None:
    with pytest.raises(pdf_treu.NichtGeeignetError):
        pdf_treu.uebernehmen(b"%PDF-1.4 kaputt")


def test_grundlinie_wie_chromium() -> None:
    # Gemessen an Gotenberg (Chromium): Arimo 12 pt bei 16,84 pt Zeilenhöhe → Grundlinie 12 pt unter der Oberkante
    assert pdf_treu._grundlinie_im_kasten(16.84, "arimo", 12) == pytest.approx(12.0)
    assert pdf_treu._grundlinie_im_kasten(14.03, "arimo", 10) == pytest.approx(9.75)
    assert pdf_treu._grundlinie_im_kasten(15.71, "carlito", 11.2) == pytest.approx(11.25)


@pytest.mark.parametrize(
    ("stil", "bleibt"),
    [
        ("margin-top: -0.4pt", True),
        ("margin-top: 12.5pt", True),
        ("line-height: 16.8pt", True),
        ("padding-left: 36.3pt", True),
        ("text-indent: -18pt", True),
        ("tab-size: 18pt", True),
        ("word-spacing: -0.42pt", True),
        ("letter-spacing: -0.12pt", True),
        ("letter-spacing: 3pt", False),
        ("line-height: expression(1)", False),
        ("tab-size: 1e9pt", False),
    ],
)
def test_positivliste_absatzformat(stil: str, bleibt: bool) -> None:
    ergebnis = sanitize_editor_html(f'<p style="{stil}">Text</p>')
    assert (stil in ergebnis) is bleibt


def test_briefpapier_je_seite_im_export() -> None:
    """Jede Originalseite liegt unter ihrer Seite; Seiten darüber hinaus nehmen die letzte."""

    def pdf_mit_seiten(texte: list[str]) -> bytes:
        puffer = io.BytesIO()
        leinwand = canvas.Canvas(puffer, pagesize=A4)
        for text in texte:
            leinwand.drawString(100, 700, text)
            leinwand.showPage()
        leinwand.save()
        return puffer.getvalue()

    hintergrund = io.BytesIO(pdf_mit_seiten(["HINTERGRUND-A", "HINTERGRUND-B"]))
    inhalt = pdf_mit_seiten(["Inhalt 1", "Inhalt 2", "Inhalt 3"])
    ergebnis = pdfium.PdfDocument(overlay_pdf_letterhead(inhalt, hintergrund, [0, 1]))
    seiten = [ergebnis[i].get_textpage().get_text_range() for i in range(len(ergebnis))]
    assert "HINTERGRUND-A" in seiten[0] and "Inhalt 1" in seiten[0]
    assert "HINTERGRUND-B" in seiten[1] and "Inhalt 2" in seiten[1]
    assert "HINTERGRUND-B" in seiten[2] and "Inhalt 3" in seiten[2]


@pytest.mark.django_db
def test_import_legt_dokumenteigenes_briefpapier_an(org: Any, make_member: Any) -> None:
    mitglied = make_member(org, ["motions.view", "motions.create", "motions.edit"])
    datei = SimpleUploadedFile("antrag.pdf", _antrag(), content_type="application/pdf")
    ergebnis = MotionImportService.import_pdf(datei, org, mitglied)
    assert ergebnis.success

    motion = Motion.objects.get(pk=ergebnis.motion.pk)
    briefpapier = motion.letterhead
    assert briefpapier is not None and briefpapier.dokument_id == motion.id
    assert briefpapier.kind == "pdf" and briefpapier.pdf_file
    # Steht in keiner Auswahl der Organisation
    assert not OrganizationLetterhead.objects.filter(pk=briefpapier.pk).exists()
    assert OrganizationLetterhead.alle.filter(pk=briefpapier.pk).exists()
    # Satz mit genauen Maßen, Seitenzahl und ohne Unterschneidung (Word)
    konfig = satz_konfig(briefpapier)
    assert konfig["treu"] is True
    assert konfig["briefpapier"]["art"] == "pdf"
    assert konfig["briefpapier"]["seiten"] == [0, 1]
    assert konfig["briefpapier"]["seitenzahl"]["text"] == "- {n} -"
    assert konfig["raender"]["links"] == pytest.approx(25.2, abs=0.4)
    # Der Text bleibt als Anhang durchsuchbar
    assert "Spielplatz" in motion.documents.get().text_content


@pytest.mark.django_db
def test_briefpapier_datei_nur_mit_zugang_zum_dokument(org: Any, make_member: Any) -> None:
    autor = make_member(org, ["motions.view", "motions.create", "motions.edit"])
    andere = make_member(org, ["motions.view"])
    datei = SimpleUploadedFile("antrag.pdf", _antrag(1), content_type="application/pdf")
    ergebnis = MotionImportService.import_pdf(datei, org, autor)
    motion = Motion.objects.get(pk=ergebnis.motion.pk)
    assert motion.visibility == "private"
    adresse = reverse(
        "work:document_letterhead_file", kwargs={"org_slug": org.slug, "letterhead_id": motion.letterhead_id}
    )
    autor_client, fremd_client = Client(), Client()
    autor_client.force_login(autor.user)
    fremd_client.force_login(andere.user)
    assert autor_client.get(adresse).status_code == 200
    assert fremd_client.get(adresse).status_code == 404

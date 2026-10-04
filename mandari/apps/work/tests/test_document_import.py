# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokument-Import (PDF/DOCX) in Work: Die Seite war ohne Funktion, weil ihr Skript in einem
nicht existierenden Template-Block stand; zudem gingen Fehlschläge verloren, wenn genau eine
Datei gelang (direkter Sprung in den Editor vor den Fehlermeldungen).
"""

from __future__ import annotations

import io
import re
import shutil
from typing import Any
from unittest import mock

import docx
import pytest
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from mandari_dokumente import OcrResult

from apps.work.motions.models import Motion

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx() -> bytes:
    dokument = docx.Document()
    dokument.add_heading("Antrag: Mehr Bäume in der Innenstadt", 1)
    dokument.add_paragraph("Der Rat beschließt, 50 Bäume zu pflanzen.")
    puffer = io.BytesIO()
    dokument.save(puffer)
    return puffer.getvalue()


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "motions.view", "motions.create", "motions.edit"])


@pytest.mark.django_db
def test_import_seite_nutzt_komponente_mit_serverlimits(org: Any, mitglied: Any, client_for: Any) -> None:
    """#620: Komponente aus dem Bundle statt Inline-Skript; Größenangabe wie auf dem Server (nicht 50 MB)."""
    seite = client_for(mitglied.user).get(f"/work/{org.slug}/documents/import/").content.decode()
    assert 'x-data="documentImport"' in seite
    assert "function importForm()" not in seite
    assert 'id="document-import-config"' in seite and '"maxBytes": 26214400' in seite
    assert "bis 25 MB" in seite and "bis 50 MB" not in seite


@pytest.mark.django_db
def test_docx_wird_bearbeitbares_dokument(org: Any, mitglied: Any, client_for: Any) -> None:
    datei = SimpleUploadedFile("baeume.docx", _docx(), content_type=DOCX)
    antwort = client_for(mitglied.user).post(f"/work/{org.slug}/documents/import/", {"import_files": [datei]})
    assert antwort.status_code == 302 and "/documents/" in antwort["Location"]
    motion = Motion.objects.get(organization=org)
    assert "50 Bäume" in (motion.content or "")


@pytest.mark.django_db
def test_fehlschlag_wird_auch_neben_erfolg_gemeldet(org: Any, mitglied: Any, client_for: Any) -> None:
    gut = SimpleUploadedFile("baeume.docx", _docx(), content_type=DOCX)
    kaputt = SimpleUploadedFile("kaputt.docx", b"PK\x03\x04 kein Word", content_type=DOCX)
    antwort = client_for(mitglied.user).post(f"/work/{org.slug}/documents/import/", {"import_files": [gut, kaputt]})
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert any("erfolgreich importiert" in t for t in texte)
    assert any("fehlgeschlagen" in t or "übersprungen" in t for t in texte), texte


@pytest.mark.django_db
def test_ohne_recht_kein_import(org: Any, make_member: Any, client_for: Any) -> None:
    lesend = make_member(org, ["dashboard.view", "motions.view"])
    datei = SimpleUploadedFile("baeume.docx", _docx(), content_type=DOCX)
    antwort = client_for(lesend.user).post(f"/work/{org.slug}/documents/import/", {"import_files": [datei]})
    assert antwort.status_code in (302, 403)
    assert not Motion.objects.filter(organization=org).exists()


def _pdf(text: str = "Der Rat beschließt, 50 Bäume zu pflanzen.") -> bytes:
    """Kleine echte Text-PDF (reportlab), deren Text pypdf ohne OCR liest."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    leinwand.drawString(72, 750, "Antrag: Mehr Baeume in der Innenstadt")
    leinwand.drawString(72, 730, text)
    leinwand.showPage()
    leinwand.save()
    return puffer.getvalue()


@pytest.mark.django_db
def test_pdf_import_service_legt_dokument_mit_text_an(org: Any, mitglied: Any) -> None:
    """Issue #422: Der Import entpackte drei statt vier Rückgabewerte und scheiterte immer."""
    from apps.work.motions.import_service import MotionImportService

    datei = SimpleUploadedFile("mehr_baeume.pdf", _pdf(), content_type="application/pdf")
    ergebnis = MotionImportService.import_pdf(datei, org, mitglied)

    assert ergebnis.success, ergebnis.error
    assert ergebnis.motion is not None and ergebnis.document is not None
    assert ergebnis.extracted_text_length > 0
    assert ergebnis.ocr_performed is False
    assert "50 Bäume" in (ergebnis.motion.content or "")
    assert "50 Bäume" in ergebnis.document.text_content
    assert ergebnis.document.mime_type == "application/pdf"
    assert ergebnis.motion.title == "Mehr baeume"


@pytest.mark.django_db
def test_pdf_wird_bearbeitbares_dokument(org: Any, mitglied: Any, client_for: Any) -> None:
    datei = SimpleUploadedFile("baeume.pdf", _pdf(), content_type="application/pdf")
    antwort = client_for(mitglied.user).post(f"/work/{org.slug}/documents/import/", {"import_files": [datei]})
    assert antwort.status_code == 302 and "/documents/" in antwort["Location"]
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert not any("fehlgeschlagen" in t for t in texte), texte
    motion = Motion.objects.get(organization=org)
    assert "50 Bäume" in (motion.content or "")
    assert motion.documents.filter(mime_type="application/pdf").count() == 1


def _gescannte_pdf() -> bytes:
    """PDF ohne Textebene (nur Grafik), wie ein Scan: pypdf findet keinen Text."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    leinwand.rect(72, 600, 400, 150, fill=1)
    leinwand.showPage()
    leinwand.save()
    return puffer.getvalue()


@pytest.mark.django_db
@override_settings(MISTRAL_API_KEY="")
def test_gescannte_pdf_nutzt_texterkennung(org: Any, mitglied: Any) -> None:
    from apps.work.motions.import_service import MotionImportService

    datei = SimpleUploadedFile("scan.pdf", _gescannte_pdf(), content_type="application/pdf")
    with mock.patch(
        "mandari_dokumente.texterkennung.ocr_pdf",
        return_value=OcrResult(text="Gescannter Antrag: 50 Bäume", pages_rendered=1),
    ) as ocr:
        ergebnis = MotionImportService.import_pdf(datei, org, mitglied)

    ocr.assert_called_once()
    assert ergebnis.success, ergebnis.error
    assert ergebnis.ocr_performed is True
    assert ergebnis.motion is not None
    assert "Gescannter Antrag: 50 Bäume" in (ergebnis.motion.content or "")


@pytest.mark.django_db
@override_settings(MISTRAL_API_KEY="")
def test_gescannte_pdf_ohne_texterkennung_wird_mit_hinweis_uebernommen(org: Any, mitglied: Any) -> None:
    from apps.work.motions.import_service import MotionImportService

    datei = SimpleUploadedFile("scan.pdf", _gescannte_pdf(), content_type="application/pdf")
    with mock.patch("mandari_dokumente.texterkennung.ocr_pdf", return_value=OcrResult()):
        ergebnis = MotionImportService.import_pdf(datei, org, mitglied)

    assert ergebnis.success, ergebnis.error
    assert ergebnis.motion is not None and ergebnis.document is not None
    assert "Text konnte nicht extrahiert werden" in (ergebnis.motion.content or "")


@pytest.mark.django_db
@override_settings(MISTRAL_API_KEY="")
def test_unlesbare_pdf_meldet_fehler_ohne_dokument(org: Any, mitglied: Any) -> None:
    from apps.work.motions.import_service import IMPORT_FAILED_MESSAGE, MotionImportService

    datei = SimpleUploadedFile("kaputt.pdf", b"%PDF-1.4 kein echtes PDF", content_type="application/pdf")
    with mock.patch("mandari_dokumente.texterkennung.ocr_pdf", return_value=OcrResult()):
        ergebnis = MotionImportService.import_pdf(datei, org, mitglied)

    assert not ergebnis.success
    assert ergebnis.error == IMPORT_FAILED_MESSAGE
    assert not Motion.objects.filter(organization=org).exists()


# ---------------------------------------------------------------------------
# #620: Gliederung, Texterkennung, Rückfall
# ---------------------------------------------------------------------------


def _antrag_pdf(zerfasert: bool = False) -> bytes:
    """Antrag wie aus Word exportiert: Kopf, umbrochene Absätze mit Silbentrennung, Punkte, Seitenzahl.

    ``zerfasert``: Leerzeichen stehen leicht versetzt über der Zeile (so erzeugen manche Programme
    ihre PDFs) – pypdf liefert dann jedes Wort in einer eigenen Zeile.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)

    def zeile(y: float, text: str) -> None:
        if not zerfasert:
            leinwand.setFont("Helvetica", 10)
            leinwand.drawString(72, y, text)
            return
        objekt = leinwand.beginText(72, y)
        objekt.setFont("Helvetica", 10)
        x = 72.0
        for wort in text.split():
            objekt.setTextOrigin(x, y)
            objekt.textOut(wort)
            x += leinwand.stringWidth(wort, "Helvetica", 10)
            objekt.setTextOrigin(x, y + 9.3)
            objekt.textOut(" ")
            x += leinwand.stringWidth(" ", "Helvetica", 10)
        leinwand.drawText(objekt)

    zeilen = [
        (780, "Ratsantrag"),
        (766, "Münster, 28.09.2026"),
        (738, "Der Rat möge beschließen:"),
        (724, "1. Der Rat bekräftigt, dass digitale Gremienarbeit in Münster Standard ist und bleibt. Bera-"),
        (710, "tungs- und Arbeitsunterlagen sind grundsätzlich digital über das Ratsinformationssystem"),
        (696, "bereitzustellen."),
        (682, "2. Die Verwaltung wird beauftragt, die sog. Ratspost einzustellen. Die Umstellung ist spätes-"),
        (668, "tens zum 01.01.2027 vorzunehmen."),
        (640, "Begründung:"),
        (626, "Die Stadt Münster verfolgt den Anspruch, die Digitalisierung konsequent und glaubwürdig um-"),
        (612, "zusetzen. Dazu gehört, dass nicht nur Verwaltungsprozesse modernisiert werden, sondern die"),
        (598, "politische Arbeit selbst."),
        (40, "Seite 1 von 1"),
    ]
    for y, text in zeilen:
        zeile(y, text)
    leinwand.showPage()
    leinwand.save()
    return puffer.getvalue()


def _absaetze(html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", teil) for teil in re.findall(r"<p>(.*?)</p>", html, re.S)]


@pytest.mark.django_db
@pytest.mark.parametrize("zerfasert", [False, True], ids=["word-export", "wort-je-zeile"])
def test_pdf_import_uebernimmt_absaetze_und_punkte(org: Any, mitglied: Any, client_for: Any, zerfasert: bool) -> None:
    """#620: Früher stand eine ganze Seite in einem Absatz (bzw. ein Wort je Zeile)."""
    datei = SimpleUploadedFile("antrag.pdf", _antrag_pdf(zerfasert), content_type="application/pdf")
    antwort = client_for(mitglied.user).post(f"/work/{org.slug}/documents/import/", {"import_files": [datei]})
    assert antwort.status_code == 302
    motion = Motion.objects.get(organization=org)
    absaetze = _absaetze(motion.content or "")

    assert absaetze[:3] == ["Ratsantrag", "Münster, 28.09.2026", "Der Rat möge beschließen:"]
    assert absaetze[3] == (
        "1. Der Rat bekräftigt, dass digitale Gremienarbeit in Münster Standard ist und bleibt. "
        "Beratungs- und Arbeitsunterlagen sind grundsätzlich digital über das Ratsinformationssystem "
        "bereitzustellen."
    )
    assert absaetze[4].startswith("2. Die Verwaltung") and "spätestens zum 01.01.2027" in absaetze[4]
    assert absaetze[5] == "Begründung:"
    assert "glaubwürdig umzusetzen. Dazu gehört" in absaetze[6]
    assert "Seite 1 von 1" not in (motion.content or "")
    anhang = motion.documents.get()
    assert anhang.mime_type == "application/pdf" and "Ratsinformationssystem" in anhang.text_content


@pytest.mark.django_db
def test_docx_import_uebernimmt_liste_und_unterschriften(org: Any, mitglied: Any, client_for: Any) -> None:
    """#620: Nummerierung wurde zu Aufzählungspunkten, Tabellen (Unterschriften) fehlten ganz."""
    from apps.work.tests.test_import_text import _antrag_docx

    datei = SimpleUploadedFile("antrag.docx", _antrag_docx(), content_type=DOCX)
    antwort = client_for(mitglied.user).post(f"/work/{org.slug}/documents/import/", {"import_files": [datei]})
    assert antwort.status_code == 302
    motion = Motion.objects.get(organization=org)
    inhalt = motion.content or ""
    assert "<ol>" in inhalt and '<ol type="a">' in inhalt
    assert "<table>" in inhalt and "Albert Wenzel" in inhalt and "Katja Martinewski" in inhalt
    assert "<p>Der Rat möge beschließen:</p>" in inhalt
    anhang = motion.documents.get()
    assert "Albert Wenzel" in anhang.text_content


@pytest.mark.django_db
@override_settings(MISTRAL_API_KEY="", WORK_IMPORT_OCR_MAX_PAGES=2)
def test_gescannte_pdf_mit_vielen_seiten_begrenzt_texterkennung(org: Any, mitglied: Any, client_for: Any) -> None:
    """#620: Texterkennung im Seitenaufruf nur für die ersten Seiten, mit Hinweis."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    for _ in range(5):
        leinwand.rect(72, 600, 400, 150, fill=1)
        leinwand.showPage()
    leinwand.save()

    datei = SimpleUploadedFile("scan.pdf", puffer.getvalue(), content_type="application/pdf")
    with mock.patch(
        "mandari_dokumente.texterkennung.ocr_pdf",
        return_value=OcrResult(text="Gescannter Antrag\n\nDer Rat beschließt.", pages_rendered=2),
    ) as ocr:
        antwort = client_for(mitglied.user).post(f"/work/{org.slug}/documents/import/", {"import_files": [datei]})

    assert ocr.call_args.kwargs["limits"].max_pages == 2
    assert ocr.call_args.kwargs["page_count"] == 5
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert any("ersten 2 von 5 Seiten" in t for t in texte), texte
    motion = Motion.objects.get(organization=org)
    assert _absaetze(motion.content or "") == ["Gescannter Antrag", "Der Rat beschließt."]


@pytest.mark.django_db
def test_scheitert_der_anhang_entsteht_kein_halbes_dokument(org: Any, mitglied: Any) -> None:
    """#620: Früher blieb ein Dokument ohne Anhang zurück, obwohl der Import als fehlgeschlagen galt."""
    from apps.work.motions.import_service import IMPORT_FAILED_MESSAGE, MotionImportService
    from apps.work.motions.models import MotionDocument

    datei = SimpleUploadedFile("antrag.docx", _docx(), content_type=DOCX)
    with mock.patch.object(MotionDocument, "save", side_effect=RuntimeError("Speicher voll")):
        ergebnis = MotionImportService.import_docx(datei, org, mitglied)
    assert not ergebnis.success and ergebnis.error == IMPORT_FAILED_MESSAGE
    assert not Motion.objects.filter(organization=org).exists()


@pytest.mark.django_db
def test_odt_wird_mit_hinweis_uebersprungen(org: Any, mitglied: Any, client_for: Any) -> None:
    datei = SimpleUploadedFile("antrag.odt", b"PK\x03\x04odt", content_type="application/vnd.oasis.opendocument.text")
    antwort = client_for(mitglied.user).post(f"/work/{org.slug}/documents/import/", {"import_files": [datei]})
    texte = [str(m) for m in get_messages(antwort.wsgi_request)]
    assert any("als DOCX oder PDF speichern" in t for t in texte), texte


def _texterkennung_verfuegbar() -> bool:
    return bool(shutil.which("tesseract") and shutil.which("pdftoppm"))


def _schrift() -> Any:
    from PIL import ImageFont

    for pfad in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(pfad, 44)
        except OSError:
            continue
    return None


@pytest.mark.django_db
@pytest.mark.skipif(not _texterkennung_verfuegbar(), reason="Tesseract/Poppler nicht installiert")
@override_settings(MISTRAL_API_KEY="")
def test_echter_scan_wird_per_texterkennung_uebernommen(org: Any, mitglied: Any) -> None:
    """Gerasterte Seite ohne Textebene (wie vom Scanner) läuft durch Poppler und Tesseract."""
    from PIL import Image, ImageDraw

    from apps.work.motions.import_service import MotionImportService

    schrift = _schrift()
    if schrift is None:
        pytest.skip("Keine TrueType-Schrift gefunden")
    seite = Image.new("L", (2480, 3508), 255)
    zeichnen = ImageDraw.Draw(seite)
    zeichnen.text((200, 300), "Antrag der Fraktion", font=schrift, fill=0)
    zeichnen.text((200, 420), "Der Rat beschließt einen Radweg an der Hauptstraße.", font=schrift, fill=0)
    puffer = io.BytesIO()
    seite.save(puffer, format="PDF", resolution=300)

    datei = SimpleUploadedFile("scan.pdf", puffer.getvalue(), content_type="application/pdf")
    ergebnis = MotionImportService.import_pdf(datei, org, mitglied)

    assert ergebnis.success, ergebnis.error
    assert ergebnis.ocr_performed is True
    assert ergebnis.motion is not None
    assert "Radweg" in (ergebnis.motion.content or "")

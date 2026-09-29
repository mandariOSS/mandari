# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dokument-Import (PDF/DOCX) in Work: Die Seite war ohne Funktion, weil ihr Skript in einem
nicht existierenden Template-Block stand; zudem gingen Fehlschläge verloren, wenn genau eine
Datei gelang (direkter Sprung in den Editor vor den Fehlermeldungen).
"""

from __future__ import annotations

import io
from typing import Any
from unittest import mock

import docx
import pytest
from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

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
def test_import_seite_liefert_ihr_skript(org: Any, mitglied: Any, client_for: Any) -> None:
    seite = client_for(mitglied.user).get(f"/work/{org.slug}/documents/import/").content.decode()
    assert 'x-data="importForm()"' in seite and "function importForm()" in seite


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
        "insight_core.services.document_extraction._extract_text_with_ocr",
        return_value=("Gescannter Antrag: 50 Bäume", True),
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
    with mock.patch("insight_core.services.document_extraction._extract_text_with_ocr", return_value=("", False)):
        ergebnis = MotionImportService.import_pdf(datei, org, mitglied)

    assert ergebnis.success, ergebnis.error
    assert ergebnis.motion is not None and ergebnis.document is not None
    assert "Text konnte nicht extrahiert werden" in (ergebnis.motion.content or "")


@pytest.mark.django_db
@override_settings(MISTRAL_API_KEY="")
def test_unlesbare_pdf_meldet_fehler_ohne_dokument(org: Any, mitglied: Any) -> None:
    from apps.work.motions.import_service import IMPORT_FAILED_MESSAGE, MotionImportService

    datei = SimpleUploadedFile("kaputt.pdf", b"%PDF-1.4 kein echtes PDF", content_type="application/pdf")
    with mock.patch("insight_core.services.document_extraction._extract_text_with_ocr", return_value=("", False)):
        ergebnis = MotionImportService.import_pdf(datei, org, mitglied)

    assert not ergebnis.success
    assert ergebnis.error == IMPORT_FAILED_MESSAGE
    assert not Motion.objects.filter(organization=org).exists()

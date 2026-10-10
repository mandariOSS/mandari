# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Externe Texterkennung nur ausdrücklich und nur für öffentliche RIS-Dateien (Issue #950).

Standard ist die Erkennung im eigenen Betrieb (pypdf, Tesseract). Work-Import und Anlagen in Session bleiben
lokal; nur Abruf und Texterkennung öffentlicher RIS-Dateien geben ``allow_external=True``. Auch dann wirkt
Mistral nur mit Basis-URL, deren Host in ``KI_ERLAUBTE_HOSTS`` steht. Die Liste hat keinen Standard: Ohne
Freigabe geht keine Anfrage nach außen.
"""

from __future__ import annotations

import inspect
from io import BytesIO
from typing import Any
from unittest import mock

import httpx
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from mandari_dokumente import OcrResult
from pypdf import PdfWriter

from insight_core.services import document_extraction

#: Beispiel-Host, den die Tests ausdrücklich freigeben (die Positivliste hat keinen Standard)
ERLAUBT_HOST = "api.openai-compat.model-serving.eu01.onstackit.cloud"
ERLAUBT = f"https://{ERLAUBT_HOST}/v1"


def _scan() -> bytes:
    """PDF ohne Textebene: pypdf findet nichts, als Nächstes käme Mistral (wenn eingerichtet)."""
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    puffer = BytesIO()
    writer.write(puffer)
    return puffer.getvalue()


class TestEinstellungen:
    @override_settings(MISTRAL_API_KEY="schluessel", MISTRAL_BASE_URL=ERLAUBT, KI_ERLAUBTE_HOSTS=[ERLAUBT_HOST])
    def test_standard_ist_lokal(self) -> None:
        assert document_extraction.extraction_config().mistral.enabled is False
        assert document_extraction.extraction_config(allow_external=True).mistral.enabled is True

    @override_settings(MISTRAL_API_KEY="schluessel", MISTRAL_BASE_URL="", KI_ERLAUBTE_HOSTS=[ERLAUBT_HOST])
    def test_ohne_adresse_aus(self) -> None:
        assert document_extraction.extraction_config(allow_external=True).mistral.enabled is False

    @override_settings(MISTRAL_API_KEY="schluessel", MISTRAL_BASE_URL=ERLAUBT, KI_ERLAUBTE_HOSTS=["api.scaleway.ai"])
    def test_positivliste_gilt_auch_hier(self) -> None:
        assert document_extraction.extraction_config(allow_external=True).mistral.enabled is False

    @override_settings(MISTRAL_API_KEY="schluessel", MISTRAL_BASE_URL=ERLAUBT, KI_ERLAUBTE_HOSTS=[])
    def test_ohne_freigabeliste_kein_aufruf(self) -> None:
        """Schlüssel und Basis-URL genügen nicht: Ohne Host in KI_ERLAUBTE_HOSTS bleibt es bei Tesseract."""
        assert document_extraction.extraction_config(allow_external=True).mistral.enabled is False
        gesendet: list[str] = []
        with (
            mock.patch.object(httpx, "post", side_effect=lambda url, **kwargs: gesendet.append(url)),
            mock.patch("mandari_dokumente.texterkennung.ocr_pdf", return_value=OcrResult(text="Tesseract-Text")),
        ):
            text, _, _, methode = document_extraction.extract_text_from_file(
                _scan(), "application/pdf", "scan.pdf", allow_external=True
            )
        assert (text, methode) == ("Tesseract-Text", "tesseract")
        assert gesendet == []

    def test_standardwerte_der_schnittstellen(self) -> None:
        for funktion in (
            document_extraction.extraction_config,
            document_extraction.extract_text_from_file,
            document_extraction.download_and_extract,
        ):
            assert inspect.signature(funktion).parameters["allow_external"].default is False, funktion.__name__


@pytest.mark.django_db
def test_work_import_bleibt_lokal(org: Any, make_member: Any) -> None:
    from apps.work.motions import import_service

    mitglied = make_member(org, ["dashboard.view", "motions.view", "motions.create", "motions.edit"])
    datei = SimpleUploadedFile("scan.pdf", b"%PDF-1.4 Scan", content_type="application/pdf")
    with (
        mock.patch("apps.work.motions.import_text.pdf_page_lines", return_value=None),
        mock.patch.object(
            import_service, "extract_text_from_file", return_value=("Erkannter Text", True, 1, "tesseract")
        ) as erkennung,
    ):
        import_service.MotionImportService.import_pdf(datei, org, mitglied)
    erkennung.assert_called_once()
    assert erkennung.call_args.kwargs["allow_external"] is False


def test_session_anlage_bleibt_lokal() -> None:
    from apps.session.services import file_service

    with mock.patch.object(
        document_extraction, "extract_text_from_file", return_value=("Text", False, 1, "pypdf")
    ) as erkennung:
        assert file_service.extract_text(b"%PDF-1.4", "application/pdf", "anlage.pdf") == "Text"
    erkennung.assert_called_once()
    assert erkennung.call_args.kwargs["allow_external"] is False


def test_oeffentliche_ris_dateien_duerfen_extern() -> None:
    """Die Aufrufer für öffentliche RIS-Dateien geben die Erlaubnis ausdrücklich (Quelltext statt Netz)."""
    from insight_ai.services import summarizer
    from insight_core.management.commands import extract_texts
    from insight_core.services import text_extraction_job

    assert "extraction_config(allow_external=True)" in inspect.getsource(text_extraction_job)
    for modul in (summarizer, extract_texts):
        assert "allow_external=True" in inspect.getsource(modul), modul.__name__

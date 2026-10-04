# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Texterkennung in der Anwendung über die gemeinsame Bibliothek ``mandari_dokumente`` (Issues #620, #530).

Die Anwendung hat keine eigene Texterkennung mehr: Seitenweises Rendern mit Speicher- und Zeitgrenzen und die
Mistral-Anbindung liegen in ``shared/mandari_dokumente`` (getestet beim Ingestor, dessen CI Poppler und
Tesseract installiert). Hier: Weitergabe der Einstellungen und der Seitengrenze, Verhalten an der Speichergrenze.
"""

from __future__ import annotations

import importlib.util
from unittest import mock

from django.test import override_settings
from mandari_dokumente import OcrMemoryLimitError, OcrResult

from insight_core.services import document_extraction


def test_keine_eigene_texterkennung_mehr() -> None:
    assert not hasattr(document_extraction, "_extract_text_with_ocr")
    assert not hasattr(document_extraction, "pytesseract")
    assert importlib.util.find_spec("insight_core.services.mistral_ocr") is None


@override_settings(MISTRAL_API_KEY="", OCR_MAX_PAGES=100, OCR_MAX_MEGAPIXELS=6, OCR_MEMORY_LIMIT_MB=900)
def test_einstellungen_und_seitengrenze_gehen_an_die_bibliothek() -> None:
    with mock.patch("mandari_dokumente.texterkennung.ocr_pdf", return_value=OcrResult(text="Scan")) as ocr:
        text, ocr_genutzt, _seiten, methode = document_extraction.extract_text_from_file(
            b"%PDF-1.4 ohne Textebene", "application/pdf", "scan.pdf", ocr_max_pages=4
        )

    assert (text, ocr_genutzt, methode) == ("Scan", True, "tesseract")
    grenzen = ocr.call_args.kwargs["limits"]
    assert (grenzen.max_pages, grenzen.max_pixels, grenzen.memory_limit_mb) == (4, 6_000_000, 900)


@override_settings(MISTRAL_API_KEY="")
def test_speichergrenze_ergibt_leeren_text_statt_abbruch() -> None:
    with mock.patch("mandari_dokumente.texterkennung.ocr_pdf", side_effect=OcrMemoryLimitError("Speichergrenze: x")):
        ergebnis = document_extraction.extract_text_from_file(b"%PDF-1.4", "application/pdf", "plan.pdf")

    assert ergebnis == ("", False, None, "none")

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Texterkennung Seite für Seite (Issue #620).

Früher wurden alle Seiten auf einmal in Farbe gerastert: ein gescannter Antrag mit zwanzig
Seiten brauchte über 1 GB Arbeitsspeicher – mehr, als der Web-Container hat.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from django.test import override_settings

from insight_core.services import document_extraction


class _Seite:
    def __init__(self, nummer: int) -> None:
        self.nummer = nummer
        self.geschlossen = False

    def close(self) -> None:
        self.geschlossen = True


def test_rastert_jede_seite_einzeln_in_graustufen_und_begrenzt() -> None:
    seiten: list[_Seite] = []

    def rastern(_data: bytes, **kwargs: Any) -> list[_Seite]:
        assert kwargs["first_page"] == kwargs["last_page"], "immer nur eine Seite im Speicher"
        assert kwargs["grayscale"] is True
        seite = _Seite(kwargs["first_page"])
        seiten.append(seite)
        return [seite]

    with (
        mock.patch.object(document_extraction, "convert_from_bytes", side_effect=rastern),
        mock.patch.object(document_extraction, "pytesseract") as tesseract,
    ):
        tesseract.image_to_string.side_effect = lambda seite, lang: f"Seite {seite.nummer}"
        text, erfolg = document_extraction._extract_text_with_ocr(b"%PDF", max_pages=3, page_count=15)

    assert erfolg is True
    assert [s.nummer for s in seiten] == [1, 2, 3]
    assert all(s.geschlossen for s in seiten)
    assert text == "Seite 1\n\nSeite 2\n\nSeite 3"


def test_ohne_seitenzahl_fragt_pdfinfo_und_ueberspringt_kaputte_seite() -> None:
    def rastern(_data: bytes, **kwargs: Any) -> list[_Seite]:
        if kwargs["first_page"] == 2:
            raise RuntimeError("defekte Seite")
        return [_Seite(kwargs["first_page"])]

    with (
        mock.patch.object(document_extraction, "pdfinfo_from_bytes", return_value={"Pages": 3}),
        mock.patch.object(document_extraction, "convert_from_bytes", side_effect=rastern),
        mock.patch.object(document_extraction, "pytesseract") as tesseract,
    ):
        tesseract.image_to_string.side_effect = lambda seite, lang: f"Seite {seite.nummer}"
        text, erfolg = document_extraction._extract_text_with_ocr(b"%PDF")

    assert erfolg is True
    assert text == "Seite 1\n\nSeite 3"


@override_settings(MISTRAL_API_KEY="")
def test_extract_text_from_file_reicht_seitengrenze_durch() -> None:
    with (
        mock.patch.object(document_extraction, "PdfReader", side_effect=RuntimeError("kein Text")),
        mock.patch.object(document_extraction, "_extract_text_with_ocr", return_value=("Scan", True)) as ocr,
    ):
        text, ocr_genutzt, _seiten, methode = document_extraction.extract_text_from_file(
            b"%PDF", "application/pdf", "scan.pdf", ocr_max_pages=4
        )
    assert (text, ocr_genutzt, methode) == ("Scan", True, "tesseract")
    assert ocr.call_args.kwargs["max_pages"] == 4

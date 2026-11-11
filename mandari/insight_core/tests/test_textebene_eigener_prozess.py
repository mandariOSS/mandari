# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die Textebene eines PDFs liest nie der Anwendungsprozess selbst.

- Hochgeladene Dateien (Session-Anlagen, Import in Work) und die zweite Stufe der Texterkennung für Dateien der
  Ratsinformationssysteme geben das Lesen an einen eigenen Prozess mit Speicher- und Zeitgrenze. Endet er an einer
  Grenze, geht die Datei mit leerer Textebene an die Erkennung aus dem Bild.
- Der Import mit Zeilen und Abständen liest ein PDF nur dann selbst, wenn der eigene Prozess es innerhalb der
  Grenzen lesen konnte, und höchstens bis zu einer Seitengrenze.
- Die Bildsuche der Textebene besucht jedes Formular je Seite einmal.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import pytest
from mandari_dokumente import LAYER_MEMORY, LAYER_NEEDS_OCR, ExtractionResult, TextLayer, textebene
from mandari_dokumente.texterkennung import TextLayerOutcome
from pypdf.generic import DictionaryObject, NameObject

from apps.work.motions import import_text
from hub.ris import erkennung
from insight_core.services import document_extraction


def _verboten(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Die Textebene darf nicht im aufrufenden Prozess gelesen werden")


def _text_pdf(seiten: int = 1) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    blatt = canvas.Canvas(puffer, pagesize=A4)
    for nummer in range(seiten):
        blatt.drawString(72, 720, f"Der Rat beschließt Punkt {nummer + 1} der Tagesordnung.")
        blatt.showPage()
    blatt.save()
    return puffer.getvalue()


@pytest.fixture
def an_der_grenze(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Der eigene Prozess endet an der Speichergrenze; das Lesen im aufrufenden Prozess ist verboten."""
    aufrufe: list[Path] = []

    def isoliert(path: Path, *args: Any, **kwargs: Any) -> TextLayerOutcome:
        aufrufe.append(path)
        return TextLayerOutcome(None, LAYER_MEMORY)

    monkeypatch.setattr(document_extraction, "extract_text_layer_isolated", isoliert)
    monkeypatch.setattr("mandari_dokumente.texterkennung.read_text_layer", _verboten)
    return aufrufe


def test_hochgeladene_pdf_liest_ein_eigener_prozess(an_der_grenze: list[Path], monkeypatch: pytest.MonkeyPatch) -> None:
    gelesen: list[TextLayer | None] = []

    def erkennung_aus_dem_bild(path: Path, mime: Any, name: str, config: Any, *, layer: Any = None) -> Any:
        gelesen.append(layer)
        return ExtractionResult("", "none", None)

    monkeypatch.setattr(document_extraction, "extract_text", erkennung_aus_dem_bild)

    text, ocr, seiten, methode = document_extraction.extract_text_from_file(_text_pdf(), "application/pdf", "a.pdf")

    assert (text, ocr, seiten, methode) == ("", False, None, "none")
    assert len(an_der_grenze) == 1
    # An der Grenze geht die Datei mit leerer Textebene weiter, nie ohne (dann läse die Bibliothek sie selbst)
    assert gelesen == [TextLayer()]


def test_textebene_aus_dem_eigenen_prozess_wird_uebernommen(monkeypatch: pytest.MonkeyPatch) -> None:
    ebene = TextLayer(pages=("",), page_count=1, scan_pages=(1,))
    monkeypatch.setattr(
        document_extraction,
        "extract_text_layer_isolated",
        lambda *args, **kwargs: TextLayerOutcome(None, LAYER_NEEDS_OCR, ebene),
    )
    gelesen: list[Any] = []

    def erkennung_aus_dem_bild(path: Path, mime: Any, name: str, config: Any, *, layer: Any = None) -> Any:
        gelesen.append(layer)
        return ExtractionResult("erkannt", "tesseract", 1)

    monkeypatch.setattr(document_extraction, "extract_text", erkennung_aus_dem_bild)

    assert document_extraction.extract_text_from_file(_text_pdf(), "application/pdf", "a.pdf")[0] == "erkannt"
    assert gelesen == [ebene]


def test_echte_pdf_wird_weiter_gelesen() -> None:
    """Ohne Ersatz: Der eigene Prozess liest die Textebene, das Ergebnis kommt wie bisher zurück."""
    text, ocr, seiten, _methode = document_extraction.extract_text_from_file(_text_pdf(), "application/pdf", "a.pdf")

    assert "Der Rat beschließt Punkt 1" in text
    assert (ocr, seiten) == (False, 1)


def test_zweite_stufe_der_ris_texterkennung_liest_nicht_selbst(
    an_der_grenze: list[Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    datei = tmp_path / "vorlage.pdf"
    datei.write_bytes(_text_pdf())
    monkeypatch.setattr(document_extraction, "extract_text", lambda *a, **k: ExtractionResult("", "none", None))

    ergebnis = erkennung.extract_text(datei, "application/pdf", "vorlage.pdf", document_extraction.extraction_config())

    assert ergebnis.text == ""
    assert an_der_grenze == [datei]


# --- Import mit Zeilen und Abständen ---------------------------------------------------------------


def test_import_liest_nur_was_der_eigene_prozess_lesen_konnte(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(document_extraction, "pdf_readable_within_limits", lambda data, file_name="": False)
    monkeypatch.setattr("pypdf.PdfReader", _verboten)

    assert import_text.pdf_page_lines(_text_pdf()) is None


def test_vorpruefung_erkennt_lesbare_und_unlesbare_dateien(monkeypatch: pytest.MonkeyPatch) -> None:
    assert document_extraction.pdf_readable_within_limits(_text_pdf()) is True

    monkeypatch.setattr(
        document_extraction, "extract_text_layer_isolated", lambda *a, **k: TextLayerOutcome(None, LAYER_MEMORY)
    )
    assert document_extraction.pdf_readable_within_limits(_text_pdf()) is False


def test_import_liest_hoechstens_die_seitengrenze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(import_text, "MAX_PDF_IMPORT_PAGES", 2)

    ergebnis = import_text.pdf_page_lines(_text_pdf(seiten=5))

    assert ergebnis is not None
    zeilen, seitenzahl = ergebnis
    assert (len(zeilen), seitenzahl) == (2, 5)
    assert "Punkt 2" in " ".join(zeilen[1])


def test_gekuerzte_nichtoeffentliche_unterlage_traegt_einen_hinweis(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.work.motions import non_public

    vollstaendig = non_public.extract(_text_pdf(seiten=2), "unterlage.pdf")
    assert vollstaendig is not None and vollstaendig.notice == ""

    monkeypatch.setattr(import_text, "MAX_PDF_IMPORT_PAGES", 2)
    gekuerzt = non_public.extract(_text_pdf(seiten=5), "unterlage.pdf")

    assert gekuerzt is not None and gekuerzt.page_count == 5
    assert "ersten 2 von 5 Seiten" in gekuerzt.notice


# --- Bildsuche der Textebene -------------------------------------------------------------------------


def _formular(ressourcen: DictionaryObject | None = None, art: str = "/Form") -> DictionaryObject:
    objekt = DictionaryObject({NameObject("/Subtype"): NameObject(art)})
    if ressourcen is not None:
        objekt[NameObject("/Resources")] = ressourcen
    return objekt


def _ebene(ziel: DictionaryObject, verweise: int) -> DictionaryObject:
    """Ressourcen, deren Einträge alle auf dasselbe Formular zeigen."""
    xobjects = DictionaryObject({NameObject(f"/F{i}"): ziel for i in range(verweise)})
    return DictionaryObject({NameObject("/XObject"): xobjects})


def _seite(verweise: int, mit_bild: bool) -> DictionaryObject:
    innerstes = _formular(art="/Image") if mit_bild else _formular()
    ressourcen = _ebene(innerstes, verweise)
    for _ in range(3):
        ressourcen = _ebene(_formular(ressourcen), verweise)
    return DictionaryObject({NameObject("/Resources"): ressourcen})


def test_bildsuche_besucht_jedes_formular_einmal() -> None:
    """Viele Verweise je Ebene auf dasselbe Formular: Ohne Gedächtnis wüchsen die Besuche mit jeder Ebene."""
    seite = _seite(verweise=60, mit_bild=False)

    start = time.perf_counter()
    gefunden = textebene._has_image(seite)

    assert gefunden is False
    assert time.perf_counter() - start < 1.0


def test_bildsuche_findet_bilder_weiter_und_gilt_ueber_dem_budget_als_ja() -> None:
    assert textebene._has_image(_seite(verweise=3, mit_bild=True)) is True
    # Mehr Einträge als das Budget, kein Bild: im Zweifel ja
    assert textebene._has_image(_seite(verweise=textebene._MAX_IMAGE_CHECKS + 1, mit_bild=False)) is True

"""
Die eine Texterkennung in ``shared/mandari_dokumente`` (Issue #530): Dateiarten, Mistral-Anbindung mit
Rückfall auf Tesseract, Ergebnisobjekt. Ohne Netz; Mistral und Unterprozesse sind ersetzt.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from mandari_dokumente import ExtractionConfig, MistralConfig, extract_text, mistral, ocr

from tests.test_ocr_grenzen import pdf_bytes


def _ocr_liefert(monkeypatch: pytest.MonkeyPatch, text: str) -> list[list[str]]:
    aufrufe: list[list[str]] = []

    def runner(
        command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        aufrufe.append(list(command))
        if command[0] == "pdftoppm":
            Path(command[-1]).with_suffix(".pgm").write_bytes(b"P5\n1 1\n255\n\x00")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(command, 0, text.encode(), b"")

    monkeypatch.setattr(ocr, "tools_available", lambda: True)
    monkeypatch.setattr(ocr, "run_limited", runner)
    return aufrufe


def test_text_und_html() -> None:
    assert extract_text(b"Beschluss\x00 des Rates ", "text/plain").text == "Beschluss des Rates"
    html = extract_text(b"<html><body><h1>Rat</h1><p>Tagesordnung</p></body></html>", "text/html; charset=utf-8")
    assert (html.text, html.method) == ("Rat\nTagesordnung", "text")
    assert extract_text("Größe".encode("latin-1"), "text/plain").text == "Größe"


def test_binaerdateien_ergeben_keinen_zeichensalat() -> None:
    word = extract_text(
        b"PK\x03\x04\x00\x00docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert (word.text, word.method) == ("", "none")
    assert extract_text(b"\x89PNG\r\n\x1a\n\x00\x00", "application/octet-stream").method == "none"
    # Unbekannter Typ, der wie Text aussieht
    assert extract_text(b"Niederschrift", "").method == "text"


def test_pdf_mit_textebene_ohne_texterkennung(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    aufrufe = _ocr_liefert(monkeypatch, "darf nicht vorkommen")
    pfad = tmp_path / "vorlage.pdf"
    pfad.write_bytes(pdf_bytes([(595.0, 842.0, "Antrag Radweg")]))

    ergebnis = extract_text(pfad, None, "vorlage.pdf")

    assert (ergebnis.method, ergebnis.page_count, ergebnis.ocr_performed) == ("pypdf", 1, False)
    assert "Antrag Radweg" in ergebnis.text
    assert aufrufe == []


def test_pdf_erkannt_am_dateianfang(monkeypatch: pytest.MonkeyPatch) -> None:
    _ocr_liefert(monkeypatch, "Gescannter Beschluss")
    ergebnis = extract_text(pdf_bytes([(595.0, 842.0, "")]), None, "")
    assert (ergebnis.method, ergebnis.text) == ("tesseract", "Gescannter Beschluss")


class _Antwort:
    def __init__(self, status: int, daten: Any = None) -> None:
        self.status_code = status
        self._daten = daten

    def json(self) -> Any:
        return self._daten


def test_mistral_vor_tesseract_und_rueckfall(monkeypatch: pytest.MonkeyPatch) -> None:
    aufrufe = _ocr_liefert(monkeypatch, "Tesseract-Text")
    gesendet: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _Antwort:
        gesendet.append(kwargs)
        return _Antwort(200, {"choices": [{"message": {"content": " Mistral-Text "}}]})

    monkeypatch.setattr(httpx, "post", post)
    config = ExtractionConfig(mistral=MistralConfig(api_key="k" * 32, requests_per_minute=0))
    scan = pdf_bytes([(595.0, 842.0, "")])

    ergebnis = extract_text(scan, "application/pdf", "scan.pdf", config)
    assert (ergebnis.method, ergebnis.text) == ("mistral", "Mistral-Text")
    assert gesendet[0]["headers"]["Authorization"].startswith("Bearer ")
    assert aufrufe == []

    # Fehler der Schnittstelle: Rückfall auf Tesseract, Schlüssel nie in Meldungen
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: _Antwort(500))
    ergebnis = extract_text(scan, "application/pdf", "scan.pdf", config)
    assert (ergebnis.method, ergebnis.text) == ("tesseract", "Tesseract-Text")


def test_mistral_begrenzung_je_minute() -> None:
    begrenzer = mistral._RateLimiter()
    assert begrenzer.acquire(2, now=0.0) and begrenzer.acquire(2, now=1.0)
    assert not begrenzer.acquire(2, now=2.0)
    assert begrenzer.acquire(2, now=61.0)
    assert begrenzer.acquire(0)

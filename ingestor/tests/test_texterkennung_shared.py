"""
Die eine Texterkennung in ``shared/mandari_dokumente`` (Issue #530): Dateiarten, Mistral-Anbindung mit
Rückfall auf Tesseract, Ergebnisobjekt. Ohne Netz; Mistral und Unterprozesse sind ersetzt.

Seit Issue #950 wirkt Mistral nur mit Basis-URL, deren Host in der Positivliste ``KI_ERLAUBTE_HOSTS`` steht.
Die Liste hat keinen Standard: Ohne ausdrückliche Freigabe geht keine Anfrage nach außen.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from mandari_dokumente import ExtractionConfig, MistralConfig, extract_text, ki_hosts, mistral, ocr

from tests.test_ocr_grenzen import pdf_bytes

#: Beispiel-Host, den die Tests ausdrücklich freigeben (die Positivliste hat keinen Standard)
ERLAUBT_HOST = "api.openai-compat.model-serving.eu01.onstackit.cloud"
ERLAUBT = f"https://{ERLAUBT_HOST}/v1"
FREIGABE = (ERLAUBT_HOST,)


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
        gesendet.append({"url": url, **kwargs})
        return _Antwort(200, {"choices": [{"message": {"content": " Mistral-Text "}}]})

    monkeypatch.setattr(httpx, "post", post)
    config = ExtractionConfig(
        mistral=MistralConfig(api_key="k" * 32, url=ERLAUBT, erlaubte_hosts=FREIGABE, requests_per_minute=0)
    )
    scan = pdf_bytes([(595.0, 842.0, "")])

    ergebnis = extract_text(scan, "application/pdf", "scan.pdf", config)
    assert (ergebnis.method, ergebnis.text) == ("mistral", "Mistral-Text")
    assert gesendet[0]["headers"]["Authorization"].startswith("Bearer ")
    assert gesendet[0]["url"] == ERLAUBT + "/chat/completions"
    assert gesendet[0]["follow_redirects"] is False
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


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("", id="ohne-url"),
        pytest.param("https://api.mistral.ai/v1", id="nicht-freigegeben"),
        pytest.param("http://api.openai-compat.model-serving.eu01.onstackit.cloud/v1", id="http"),
        pytest.param("https://api.openai-compat.model-serving.eu01.onstackit.cloud.example.com/v1", id="suffix"),
    ],
)
def test_mistral_ohne_freigegebene_adresse_aus(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein gesetzter Schlüssel allein schickt nichts nach außen (Issue #950), auch nicht mit Freigabeliste."""
    assert MistralConfig(api_key="x", erlaubte_hosts=FREIGABE).enabled is False
    config = MistralConfig(api_key="x", url=url, erlaubte_hosts=FREIGABE)
    assert config.enabled is False
    gesendet: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: gesendet.append(url))
    _ocr_liefert(monkeypatch, "Tesseract-Text")
    ergebnis = extract_text(
        pdf_bytes([(595.0, 842.0, "")]), "application/pdf", "scan.pdf", ExtractionConfig(mistral=config)
    )
    assert ergebnis.method == "tesseract"
    assert gesendet == []
    with pytest.raises(mistral.MistralError):
        mistral.extract_text_with_mistral(b"%PDF", config)


def test_ohne_freigabeliste_kein_aufruf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schlüssel und Basis-URL genügen nicht: Ohne Host in der Positivliste geht nichts nach außen."""
    assert ki_hosts.STANDARD_ERLAUBTE_HOSTS == ()
    config = MistralConfig(api_key="x", url=ERLAUBT, requests_per_minute=0)
    assert config.erlaubte_hosts == () and config.enabled is False
    gesendet: list[str] = []
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: gesendet.append(url))
    _ocr_liefert(monkeypatch, "Tesseract-Text")
    ergebnis = extract_text(
        pdf_bytes([(595.0, 842.0, "")]), "application/pdf", "scan.pdf", ExtractionConfig(mistral=config)
    )
    assert ergebnis.method == "tesseract"
    with pytest.raises(mistral.MistralError):
        mistral.extract_text_with_mistral(b"%PDF", config)
    assert gesendet == []


def test_mistral_mit_freigegebener_adresse_an() -> None:
    assert MistralConfig(api_key="x", url=ERLAUBT, erlaubte_hosts=FREIGABE).enabled is True
    assert MistralConfig(api_key="x", url=ERLAUBT + "/chat/completions").endpoint == ERLAUBT + "/chat/completions"
    assert MistralConfig(api_key="", url=ERLAUBT, erlaubte_hosts=FREIGABE).enabled is False
    ionos = "https://openai.inference.de-txl.ionos.com/v1"
    assert MistralConfig(api_key="x", url=ionos).enabled is False
    assert MistralConfig(api_key="x", url=ionos, erlaubte_hosts=("openai.inference.de-txl.ionos.com",)).enabled is True


def test_schluessel_nicht_in_repr() -> None:
    assert "geheim-123" not in repr(MistralConfig(api_key="geheim-123", url=ERLAUBT))


def test_ingestor_liest_url_und_positivliste(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config import settings
    from src.extraction import extractor

    monkeypatch.setattr(settings, "mistral_api_key", "x")
    monkeypatch.setattr(settings, "mistral_base_url", "")
    monkeypatch.setattr(settings, "ki_erlaubte_hosts", "")
    assert extractor.extraction_config().mistral.enabled is False
    # Ohne Freigabeliste bleibt auch eine gesetzte Basis-URL wirkungslos
    monkeypatch.setattr(settings, "mistral_base_url", ERLAUBT)
    assert extractor.extraction_config().mistral.enabled is False
    assert extractor.extraction_config().mistral.erlaubte_hosts == ()
    monkeypatch.setattr(settings, "ki_erlaubte_hosts", ERLAUBT_HOST)
    assert extractor.extraction_config().mistral.enabled is True
    monkeypatch.setattr(settings, "ki_erlaubte_hosts", "openai.inference.de-txl.ionos.com")
    assert extractor.extraction_config().mistral.enabled is False
    assert extractor.extraction_config().mistral.erlaubte_hosts == ("openai.inference.de-txl.ionos.com",)
    assert ki_hosts.erlaubte_hosts_aus_umgebung("") == ki_hosts.STANDARD_ERLAUBTE_HOSTS == ()

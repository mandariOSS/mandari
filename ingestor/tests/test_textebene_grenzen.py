"""
Textebene mit Grenzen im Ingestor, mit demselben eigenen Prozess wie die erste Stufe der Anwendung (Issue #1107).

Der Ingestor liest die Textebene eines PDFs nie im eigenen Prozess, sondern wie der Auftrag ``file.read_text_layer``
der Anwendung in einem eigenen Prozess mit Speicher- und Zeitgrenze (``extract_text_layer_isolated``), höchstens
``TEXT_LAYER_CONCURRENCY`` davon zugleich. Reicht die Textebene nicht oder endet der Prozess an einer Grenze, geht die
Datei an die Erkennung aus dem Bild, ohne dass der Ingestor die Textebene selbst liest. Andere Dateien als PDFs
brauchen keinen eigenen Prozess. Die Ausgabe des Prozesses liest der Aufrufer nur bis zu einer Grenze.
``read_text_layer`` kennt Grenzen für Seiten und Zeichen, prüft die Seitenzahl und gibt an einer Speichergrenze kein
Teilergebnis aus.

Die Tests mit echter Speichergrenze (``ulimit -v``) laufen nur unter Linux; die übrigen auch unter Windows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tracemalloc
import zlib
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mandari_dokumente import (
    LAYER_ERROR,
    LAYER_MEMORY,
    LAYER_OK,
    IsolationLimits,
    extract_text_layer_isolated,
    ocr,
    read_text_layer,
)
from mandari_dokumente import texterkennung as bibliothek
from mandari_dokumente.texterkennung import ExtractionConfig
from pypdf import PageObject, PdfReader

from src.config import settings
from src.extraction import extractor
from src.extraction.extractor import DownloadedFile, TextExtractor
from tests.test_ocr_grenzen import A0, A4, FakeRunner, _dpi, pdf_bytes

linux = pytest.mark.skipif(
    os.name != "posix" or not Path("/bin/sh").exists(), reason="Speichergrenze per ulimit nur unter Linux"
)

#: Adressraum des Prozesses der Textebene in den Linux-Tests: reicht für Python, pypdf und eine gewöhnliche Seite
#: (gemessen rund 40 MB), nicht für eine Seite mit großem Inhaltsstrom
GRENZE_MB = 160
#: Größe eines Inhaltsstroms nach dem Entpacken in MiB (unter der Grenze von pypdf je Strom)
STROM_MIB = 70


def _verboten(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("Textebene im Prozess des Ingestors gelesen")


def _ocr_ersetzt(monkeypatch: pytest.MonkeyPatch, runner: FakeRunner) -> FakeRunner:
    monkeypatch.setattr(ocr, "tools_available", lambda: True)
    monkeypatch.setattr(ocr, "run_limited", runner)
    return runner


def _inhaltsstrom(mib: int) -> bytes:
    """Inhaltsstrom (FlateDecode) mit einer Textzeile und ``mib`` MiB Leerraum."""
    packer = zlib.compressobj()
    teile = [packer.compress(b"BT /F1 12 Tf 72 720 Td (Beschlussvorlage) Tj ET\n")]
    block = b" " * (1024 * 1024)
    teile += [packer.compress(block) for _ in range(mib)]
    teile.append(packer.flush())
    return b"".join(teile)


def _pdf_mit_stroemen(inhalte: Sequence[tuple[bytes, bool]]) -> bytes:
    """Minimales PDF (A4) mit je Seite einem Inhaltsstrom: (Daten, mit FlateDecode?)."""
    objekte: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kinder: list[int] = []
    for daten, gepackt in inhalte:
        kopf = b"<< /Length %d%s >>" % (len(daten), b" /Filter /FlateDecode" if gepackt else b"")
        objekte.append(kopf + b"\nstream\n" + daten + b"\nendstream")
        inhalt = len(objekte)
        objekte.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {inhalt} 0 R >>"
            ).encode()
        )
        kinder.append(len(objekte))
    objekte[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objekte[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kinder)}] /Count {len(kinder)} >>".encode()
    aus = bytearray(b"%PDF-1.4\n")
    versatz: list[int] = []
    for nummer, rumpf in enumerate(objekte, start=1):
        versatz.append(len(aus))
        aus += f"{nummer} 0 obj\n".encode() + rumpf + b"\nendobj\n"
    xref = len(aus)
    aus += f"xref\n0 {len(objekte) + 1}\n".encode() + b"0000000000 65535 f \n"
    for wert in versatz:
        aus += f"{wert:010d} 00000 n \n".encode()
    aus += f"trailer\n<< /Size {len(objekte) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(aus)


def _pdf_mit_grossen_seiten(seiten: int) -> bytes:
    """Erste Seite gewöhnlicher Text, danach ``seiten`` Seiten mit je einem großen Inhaltsstrom."""
    strom = _inhaltsstrom(STROM_MIB)
    return _pdf_mit_stroemen([(b"BT /F1 12 Tf 72 720 Td (Antrag Radweg) Tj ET", False)] + [(strom, True)] * seiten)


@pytest.fixture
def plaetze(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plätze für Prozesse der Textebene je Test neu anlegen (nach ``TEXT_LAYER_CONCURRENCY`` des Tests)."""
    monkeypatch.setattr(extractor, "_text_layer_slots", None)


# =============================================================================
# read_text_layer: Grenzen für Seiten und Zeichen, Seitenzahl, kein Teilergebnis an der Speichergrenze
# =============================================================================


def test_textebene_liest_hoechstens_seiten_und_zeichen() -> None:
    datei = pdf_bytes([(*A4, f"Beschluss Nummer {nummer}") for nummer in range(1, 6)])

    vollstaendig = read_text_layer(datei, max_pages=100)
    assert (len(vollstaendig.pages), vollstaendig.page_count, vollstaendig.truncated) == (5, 5, False)

    seiten = read_text_layer(datei, max_pages=100, max_text_pages=3)
    assert (len(seiten.pages), seiten.page_count, seiten.truncated) == (3, 5, True)

    zeichen = read_text_layer(datei, max_pages=100, max_chars=25)
    assert sum(len(seite) for seite in zeichen.pages) == 25
    assert zeichen.page_count == 5 and zeichen.truncated
    assert zeichen.pages[0] == "Beschluss Nummer 1"


@pytest.mark.parametrize("angabe", [10**12, 100_001])
def test_unplausible_seitenzahl_gilt_als_unbekannt(monkeypatch: pytest.MonkeyPatch, angabe: int) -> None:
    """pypdf übernimmt ``/Count`` teils ungeprüft (verschlüsselte Dateien); so eine Zahl geht nie in die Datenbank."""
    monkeypatch.setattr(PdfReader, "get_num_pages", lambda self: angabe)
    datei = pdf_bytes([(*A4, f"Beschluss Nummer {nummer}") for nummer in range(1, 6)])

    ebene = read_text_layer(datei, max_pages=100, max_text_pages=3)

    assert ebene.truncated and ebene.page_count is None


def test_speichermangel_endet_nicht_als_teilergebnis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein ``MemoryError`` ab Seite 2 ergab bisher die Textebene der ersten Seite, als wäre sie vollständig."""
    echt = PageObject.extract_text
    gelesen: list[int] = []

    def extract_text(self: PageObject, *args: Any, **kwargs: Any) -> str:
        gelesen.append(1)
        if len(gelesen) >= 2:
            raise MemoryError
        return echt(self, *args, **kwargs)

    monkeypatch.setattr(PageObject, "extract_text", extract_text)
    with pytest.raises(MemoryError):
        read_text_layer(pdf_bytes([(*A4, "Antrag Radweg"), (*A4, "Begruendung")]), max_pages=100)


# =============================================================================
# Prozess der ersten Stufe: nur für PDFs, Ausgabe begrenzt
# =============================================================================


def test_andere_dateien_ohne_eigenen_prozess(tmp_path: Path) -> None:
    pfad = tmp_path / "protokoll.txt"
    pfad.write_bytes(b"Niederschrift der Sitzung\x01")

    ausgang = extract_text_layer_isolated(pfad, "text/plain", "protokoll.txt", ExtractionConfig(), runner=_verboten)

    assert ausgang.reason == LAYER_OK and ausgang.result is not None
    assert (ausgang.result.text, ausgang.result.method) == ("Niederschrift der Sitzung\x01", "text")


def test_ausgabe_des_prozesses_nur_bis_zur_grenze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bibliothek, "MAX_LAYER_OUTPUT_BYTES", 1000)
    befehl = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000)"]
    with pytest.raises(bibliothek.LayerOutputLimitError):
        bibliothek.run_isolated(befehl, 30, 0, dict(os.environ))
    klein = bibliothek.run_isolated([sys.executable, "-c", "print('ok')"], 30, 0, dict(os.environ))
    assert (klein.returncode, klein.stdout.strip()) == (0, b"ok")

    # Auch eine ersetzte Ausführung liefert nicht mehr als die Grenze an den Aufrufer
    pfad = tmp_path / "antrag.pdf"
    pfad.write_bytes(pdf_bytes([(*A4, "Antrag Radweg")]))
    gross = json.dumps({"needs_ocr": False, "text": "x" * 2000}).encode()

    def runner(command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]) -> Any:
        return subprocess.CompletedProcess(list(command), 0, gross, b"")

    ausgang = extract_text_layer_isolated(pfad, "application/pdf", "antrag.pdf", ExtractionConfig(), runner=runner)
    assert (ausgang.result, ausgang.reason) == (None, LAYER_ERROR)


# =============================================================================
# Ingestor: Textebene nur in einem eigenen Prozess mit Grenzen
# =============================================================================


@pytest.mark.usefixtures("plaetze")
def test_ingestor_liest_die_textebene_in_eigenem_prozess_mit_grenzen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bibliothek, "read_text_layer", _verboten)
    monkeypatch.setattr(settings, "text_layer_memory_limit_mb", 300)
    monkeypatch.setattr(settings, "text_layer_timeout", 90.0)
    echt = bibliothek.run_isolated
    grenzen: list[tuple[float, int]] = []

    def runner(command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]) -> Any:
        grenzen.append((timeout, memory_limit_mb))
        return echt(command, timeout, memory_limit_mb, env)

    monkeypatch.setattr(bibliothek, "run_isolated", runner)
    pfad = tmp_path / "download.part"
    pfad.write_bytes(pdf_bytes([(*A4, "Antrag Radweg"), (*A4, "Begruendung")]))

    text, seiten, methode = TextExtractor._extract_text(pfad, "application/pdf", "-anlage.pdf")

    assert "Antrag Radweg" in text and "Begruendung" in text
    assert (seiten, methode) == (2, "pypdf")
    assert grenzen == [(90.0, 300)]


def test_ingestor_erkennt_andere_dateien_ohne_eigenen_prozess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bibliothek, "run_isolated", _verboten)
    pfad = tmp_path / "download.part"
    pfad.write_bytes(b"<html><body><p>Tagesordnung</p></body></html>")

    assert TextExtractor._extract_text(pfad, "text/html", "to.html") == ("Tagesordnung", None, "text")


@pytest.mark.usefixtures("plaetze")
def test_hoechstens_so_viele_prozesse_der_textebene_zugleich(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Über alle Extraktoren und Threads des Ingestors hinweg laufen höchstens ``TEXT_LAYER_CONCURRENCY`` Prozesse."""
    monkeypatch.setattr(settings, "text_layer_concurrency", 2)
    zaehler = threading.Lock()
    laufend: list[int] = [0]
    hoechstens: list[int] = [0]
    antwort = json.dumps({"needs_ocr": False, "text": "Antrag", "method": "pypdf", "page_count": 1}).encode()

    def runner(command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]) -> Any:
        with zaehler:
            laufend[0] += 1
            hoechstens[0] = max(hoechstens[0], laufend[0])
        time.sleep(0.2)
        with zaehler:
            laufend[0] -= 1
        return subprocess.CompletedProcess(list(command), 0, antwort, b"")

    monkeypatch.setattr(bibliothek, "run_isolated", runner)
    pfad = tmp_path / "download.part"
    pfad.write_bytes(pdf_bytes([(*A4, "Antrag")]))
    ergebnisse: list[tuple[str, int | None, str]] = []

    def bearbeiten() -> None:
        ergebnisse.append(TextExtractor._extract_text(pfad, "application/pdf", "antrag.pdf"))

    threads = [threading.Thread(target=bearbeiten) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert ergebnisse == [("Antrag", 1, "pypdf")] * 6
    assert hoechstens[0] == 2


@pytest.mark.usefixtures("plaetze")
def test_scan_geht_mit_seitengroessen_aus_dem_prozess_an_die_erkennung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Der Prozess liefert Seitenzahl und -größen mit: Die Erkennung rendert wie bisher je Seitengröße."""
    monkeypatch.setattr(bibliothek, "read_text_layer", _verboten)
    runner = _ocr_ersetzt(monkeypatch, FakeRunner())
    pfad = tmp_path / "download.part"
    pfad.write_bytes(pdf_bytes([(*A4, ""), (*A0, "")]))

    text, seiten, methode = TextExtractor._extract_text(pfad, "application/pdf", "plan.pdf")

    assert (text, seiten, methode) == ("Seite 1\n\nSeite 2", 2, "tesseract")
    gerendert = runner.renders()
    assert len(gerendert) == 2 and _dpi(gerendert[0]) == 200 and _dpi(gerendert[1]) < 200


@pytest.mark.usefixtures("plaetze")
@pytest.mark.parametrize(
    "ende",
    [
        pytest.param(subprocess.TimeoutExpired("python", 120), id="zeit"),
        pytest.param(subprocess.CompletedProcess([], 3, b"", b"MemoryError"), id="speicher"),
        pytest.param(subprocess.CompletedProcess([], -9, b"", b""), id="beendet"),
        pytest.param(subprocess.CompletedProcess([], 1, b"", b"Traceback"), id="fehler"),
        pytest.param(bibliothek.LayerOutputLimitError("zu groß"), id="ausgabe"),
    ],
)
def test_an_einer_grenze_des_prozesses_erkennt_der_ingestor_aus_dem_bild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ende: Any
) -> None:
    """Endet der Prozess an einer Grenze, liest der Ingestor die Textebene nicht selbst nach."""
    monkeypatch.setattr(bibliothek, "read_text_layer", _verboten)

    def runner(command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]) -> Any:
        if isinstance(ende, BaseException):
            raise ende
        return ende

    monkeypatch.setattr(bibliothek, "run_isolated", runner)
    # Ohne Seitenzahl erkennt Tesseract bis zur ersten Seite, die sich nicht rendern lässt
    _ocr_ersetzt(monkeypatch, FakeRunner({("render", 2): [(1, b"")]}))
    pfad = tmp_path / "download.part"
    pfad.write_bytes(pdf_bytes([(*A4, "Antrag Radweg")]))

    assert TextExtractor._extract_text(pfad, "application/pdf", "antrag.pdf") == ("Seite 1", None, "tesseract")


# =============================================================================
# Linux: echte Speichergrenze
# =============================================================================


@linux
def test_eigener_prozess_endet_an_der_speichergrenze_ohne_teilergebnis(tmp_path: Path) -> None:
    kontrolle, gross = tmp_path / "kontrolle.pdf", tmp_path / "gross.pdf"
    kontrolle.write_bytes(pdf_bytes([(*A4, "Antrag Radweg")]))
    gross.write_bytes(_pdf_mit_grossen_seiten(seiten=2))
    grenzen = IsolationLimits(memory_limit_mb=GRENZE_MB, timeout=60)

    ok = extract_text_layer_isolated(kontrolle, "application/pdf", "antrag.pdf", ExtractionConfig(), grenzen)
    assert ok.reason == LAYER_OK and ok.result is not None and "Antrag Radweg" in ok.result.text

    ausgang = extract_text_layer_isolated(gross, "application/pdf", "vorlage.pdf", ExtractionConfig(), grenzen)
    assert (ausgang.result, ausgang.reason) == (None, LAYER_MEMORY)


class _Storage:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def update_file_text(self, **werte: Any) -> None:
        self.updates.append(werte)


@linux
@pytest.mark.usefixtures("plaetze")
async def test_grosse_textebene_belastet_den_ingestor_nicht(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Eine Textebene, die mehr Speicher braucht als die Grenze, endet im eigenen Prozess; im Ingestor selbst bleibt der
    Speicher der Python-Objekte klein (``tracemalloc``, misst nur diesen Prozess) und die Datei kommt aus dem Bild.
    """
    monkeypatch.setattr(settings, "text_layer_memory_limit_mb", GRENZE_MB)
    _ocr_ersetzt(monkeypatch, FakeRunner({("render", 5): [(1, b"")]}))
    pfad = tmp_path / "download.part"
    pfad.write_bytes(_pdf_mit_grossen_seiten(seiten=3))
    geladen = DownloadedFile(path=pfad, size=pfad.stat().st_size, sha256="0" * 64, head=b"%PDF-1.4")
    storage = _Storage()

    lief_schon = tracemalloc.is_tracing()
    if not lief_schon:
        tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        await TextExtractor(storage)._extract_and_store(
            SimpleNamespace(id="f1", body_id=None), geladen, "application/pdf", "vorlage.pdf"
        )
        spitze_mib = tracemalloc.get_traced_memory()[1] / 1024 / 1024
    finally:
        if not lief_schon:
            tracemalloc.stop()

    assert spitze_mib < 32, f"Ingestor belegte {spitze_mib:.0f} MiB"
    letzte = storage.updates[-1]
    assert (letzte["status"], letzte["method"]) == ("completed", "tesseract")
    assert letzte["text_content"] == "Seite 1\n\nSeite 2\n\nSeite 3\n\nSeite 4"

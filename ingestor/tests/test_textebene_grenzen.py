"""
Textebene von PDFs nur in einem eigenen Prozess mit Speicher-, Zeit- und Seitengrenze.

Der Ingestor liest die Textebene (pypdf) eines PDFs nie im eigenen Prozess, der alle Abgleiche trägt: pypdf hält die
entpackten Inhalte aller gelesenen Seiten, und eine kleine Datei kann so Hunderte MB belegen. Ein eigener Prozess
(``src/extraction/textebene.py``) liest sie mit Grenze des Adressraums (unter Linux) und Zeitgrenze, höchstens
``TEXT_LAYER_CONCURRENCY`` davon zugleich, und liefert höchstens ``MAX_TEXT_PAGES`` Seiten mit ``MAX_TEXT_CHARS``
Zeichen. Seine Ausgabe liest der Ingestor nur bis zu einer Grenze. Endet der Prozess an einer Grenze, erkennt der
Ingestor die Datei aus dem Bild, ohne die Textebene selbst zu lesen.

Die Tests mit echter Speichergrenze laufen nur unter Linux; die übrigen auch unter Windows.
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
from typing import Any

import pypdf
import pytest

from src.config import settings
from src.extraction import extractor, textebene
from src.extraction.extractor import TextExtractor

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Grenze des Adressraums nur unter Linux")

#: Adressraum des eigenen Prozesses im Linux-Test: reicht für Python, pypdf und eine gewöhnliche Seite, nicht für
#: eine Seite mit großem Inhaltsstrom
GRENZE_MB = 160
#: Größe eines großen Inhaltsstroms nach dem Entpacken in MiB (unter der Grenze von pypdf je Strom)
STROM_MIB = 70


def _inhaltsstrom(mib: int) -> bytes:
    """Inhaltsstrom (FlateDecode) mit einer Textzeile und ``mib`` MiB Leerraum."""
    packer = zlib.compressobj()
    teile = [packer.compress(b"BT /F1 12 Tf 72 720 Td (Beschlussvorlage) Tj ET\n")]
    block = b" " * (1024 * 1024)
    teile += [packer.compress(block) for _ in range(mib)]
    teile.append(packer.flush())
    return b"".join(teile)


def _pdf(inhalte: Sequence[tuple[bytes, bool]]) -> bytes:
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


def _textseiten(*texte: str) -> bytes:
    return _pdf([(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode(), False) for text in texte])


@pytest.fixture
def ohne_textebene_im_ingestor(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """pypdf darf die Datei in diesem Prozess nicht öffnen (der eigene Prozess ist davon nicht betroffen)."""
    aufrufe: list[int] = []

    def verboten(self: Any, *args: Any, **kwargs: Any) -> None:
        aufrufe.append(1)
        raise AssertionError("Textebene im Prozess des Ingestors gelesen")

    monkeypatch.setattr(pypdf.PdfReader, "__init__", verboten)
    return aufrufe


@pytest.fixture
def ocr(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Erkennung aus dem Bild ersetzt: merkt sich die übergebene Seitenzahl."""
    seitenzahlen: list[int | None] = []

    def erkennen(data: bytes, page_count: int | None = None) -> tuple[str, bool]:
        seitenzahlen.append(page_count)
        return "Text aus dem Bild", True

    monkeypatch.setattr(extractor, "_extract_text_with_ocr", erkennen)
    monkeypatch.setattr(settings, "mistral_api_key", "")
    return seitenzahlen


@pytest.fixture(autouse=True)
def plaetze(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plätze für Prozesse der Textebene je Test neu anlegen (nach ``TEXT_LAYER_CONCURRENCY`` des Tests)."""
    monkeypatch.setattr(extractor, "_text_layer_slots", None, raising=False)


# =============================================================================
# Textebene im eigenen Prozess
# =============================================================================


def test_textebene_nie_im_prozess_des_ingestors(ohne_textebene_im_ingestor: list[int], ocr: list[int | None]) -> None:
    daten = _textseiten("Antrag Radweg", "Begruendung")

    assert TextExtractor._extract_text(daten, "application/pdf", "antrag.pdf") == (
        "Antrag Radweg\n\nBegruendung",
        2,
        "pypdf",
    )
    assert ohne_textebene_im_ingestor == [] and ocr == []


def test_speicher_des_ingestors_haengt_nicht_an_der_textebene(ocr: list[int | None]) -> None:
    """Zwei Seiten mit großen Inhaltsströmen belegten bisher im Ingestor rund 30 MiB je Seite, bis zum Ende."""
    daten = _pdf([(_inhaltsstrom(24), True)] * 2)
    assert len(daten) < 100_000

    tracemalloc.start()
    try:
        ergebnis = TextExtractor._extract_text(daten, "application/pdf", "vorlage.pdf")
        spitze_mib = tracemalloc.get_traced_memory()[1] / 2**20
    finally:
        tracemalloc.stop()

    assert ergebnis == ("Beschlussvorlage\n\nBeschlussvorlage", 2, "pypdf")
    assert spitze_mib < 8, f"Ingestor belegte {spitze_mib:.0f} MiB"


def test_andere_dateien_ohne_eigenen_prozess(monkeypatch: pytest.MonkeyPatch) -> None:
    def verboten(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("eigener Prozess für eine Datei, die kein PDF ist")

    monkeypatch.setattr(extractor.subprocess, "run", verboten)

    daten = b"<html><body><p>Tagesordnung</p></body></html>"
    assert TextExtractor._extract_text(daten, "text/html", "to.html") == ("Tagesordnung", None, "text")


# =============================================================================
# Grenzen für Seiten und Zeichen, kein Teilergebnis an der Speichergrenze
# =============================================================================


def test_textebene_liefert_hoechstens_seiten_und_zeichen() -> None:
    assert (textebene.MAX_TEXT_PAGES, textebene.MAX_TEXT_CHARS) == (3000, 5_000_000)
    daten = _textseiten(*(f"Beschluss Nummer {nummer}" for nummer in range(1, 6)))

    text, seiten = textebene.read_text_layer(daten)
    assert (text.count("Beschluss"), seiten) == (5, 5)

    text, seiten = textebene.read_text_layer(daten, max_pages=3)
    assert (text, seiten) == ("Beschluss Nummer 1\n\nBeschluss Nummer 2\n\nBeschluss Nummer 3", 5)

    text, seiten = textebene.read_text_layer(daten, max_chars=25)
    assert (text, seiten) == ("Beschluss Nummer 1\n\nBesch", 5)


def test_speichermangel_endet_nicht_als_teilergebnis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein ``MemoryError`` ab Seite 2 darf nicht als Text der ersten Seite enden, als wäre er vollständig."""
    echt = pypdf.PageObject.extract_text
    gelesen: list[int] = []

    def extract_text(self: pypdf.PageObject, *args: Any, **kwargs: Any) -> str:
        gelesen.append(1)
        if len(gelesen) >= 2:
            raise MemoryError
        return echt(self, *args, **kwargs)

    monkeypatch.setattr(pypdf.PageObject, "extract_text", extract_text)
    with pytest.raises(MemoryError):
        textebene.read_text_layer(_textseiten("Antrag Radweg", "Begruendung"))


def test_unlesbare_datei_ergibt_keinen_text() -> None:
    assert textebene.read_text_layer(b"%PDF-1.4 kaputt") == ("", None)


# =============================================================================
# Zeitgrenze, Ausgabe, gleichzeitige Prozesse
# =============================================================================


def _skript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quelltext: str) -> None:
    """Ersetzt das Skript des eigenen Prozesses."""
    pfad = tmp_path / "ersatz.py"
    pfad.write_text(quelltext, encoding="utf-8")
    monkeypatch.setattr(extractor, "_TEXT_LAYER_SCRIPT", pfad)


def test_zeitgrenze_beendet_den_prozess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ohne_textebene_im_ingestor: list[int], ocr: list[int | None]
) -> None:
    _skript(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    monkeypatch.setattr(settings, "text_layer_timeout", 1.0)

    beginn = time.monotonic()
    ergebnis = TextExtractor._extract_text(_textseiten("Antrag"), "application/pdf", "antrag.pdf")

    assert time.monotonic() - beginn < 30
    assert ergebnis == ("Text aus dem Bild", None, "tesseract")
    assert ocr == [None] and ohne_textebene_im_ingestor == []


def test_ausgabe_des_prozesses_nur_bis_zur_grenze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ohne_textebene_im_ingestor: list[int], ocr: list[int | None]
) -> None:
    antwort = json.dumps({"text": "x" * 5000, "page_count": 1})
    _skript(tmp_path, monkeypatch, f"import sys\nsys.stdout.write({antwort!r})\n")
    monkeypatch.setattr(extractor, "_MAX_LAYER_OUTPUT_BYTES", 1000)

    ergebnis = TextExtractor._extract_text(_textseiten("Antrag"), "application/pdf", "antrag.pdf")

    assert ergebnis == ("Text aus dem Bild", None, "tesseract")
    assert ohne_textebene_im_ingestor == []


def _lauf(rueckgabe: int, ausgabe: bytes = b"", fehler: bytes = b"") -> Any:
    def run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        kwargs["stdout"].write(ausgabe)
        kwargs["stderr"].write(fehler)
        return subprocess.CompletedProcess(list(command), rueckgabe)

    return run


def _zeitgrenze(command: Sequence[str], **kwargs: Any) -> Any:
    raise subprocess.TimeoutExpired(list(command), kwargs["timeout"])


def _nicht_startbar(command: Sequence[str], **kwargs: Any) -> Any:
    raise OSError("kein Interpreter")


@pytest.mark.parametrize(
    "lauf",
    [
        pytest.param(_zeitgrenze, id="zeit"),
        pytest.param(_lauf(textebene.EXIT_MEMORY, fehler=b"MemoryError"), id="speicher"),
        pytest.param(_lauf(-9), id="beendet"),
        pytest.param(_lauf(1, fehler=b"Traceback"), id="fehler"),
        pytest.param(_lauf(0, b"kein json"), id="unlesbar"),
        pytest.param(_lauf(0, b'["text"]'), id="kein-objekt"),
        pytest.param(_nicht_startbar, id="nicht-startbar"),
    ],
)
def test_an_einer_grenze_erkennt_der_ingestor_aus_dem_bild(
    monkeypatch: pytest.MonkeyPatch, ohne_textebene_im_ingestor: list[int], ocr: list[int | None], lauf: Any
) -> None:
    """Endet der Prozess an einer Grenze, liest der Ingestor die Textebene nicht selbst nach."""
    monkeypatch.setattr(extractor.subprocess, "run", lauf)

    ergebnis = TextExtractor._extract_text(_textseiten("Antrag"), "application/pdf", "antrag.pdf")

    assert ergebnis == ("Text aus dem Bild", None, "tesseract")
    assert ocr == [None] and ohne_textebene_im_ingestor == []


@pytest.mark.parametrize("angabe", [10**12, -1, "3", True])
def test_unplausible_seitenzahl_gilt_als_unbekannt(monkeypatch: pytest.MonkeyPatch, angabe: Any) -> None:
    antwort = json.dumps({"text": "Antrag", "page_count": angabe}).encode()
    monkeypatch.setattr(extractor.subprocess, "run", _lauf(0, antwort))

    assert TextExtractor._extract_text(_textseiten("Antrag"), "application/pdf", "a.pdf") == ("Antrag", None, "pypdf")


def test_hoechstens_so_viele_prozesse_der_textebene_zugleich(monkeypatch: pytest.MonkeyPatch) -> None:
    """Über alle Extraktoren und Threads des Ingestors hinweg laufen höchstens ``TEXT_LAYER_CONCURRENCY`` Prozesse."""
    monkeypatch.setattr(settings, "text_layer_concurrency", 2)
    zaehler = threading.Lock()
    laufend: list[int] = [0]
    hoechstens: list[int] = [0]
    grenzen: list[tuple[float, list[str]]] = []

    def run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        with zaehler:
            laufend[0] += 1
            hoechstens[0] = max(hoechstens[0], laufend[0])
            grenzen.append((kwargs["timeout"], list(command)))
        time.sleep(0.2)
        with zaehler:
            laufend[0] -= 1
        kwargs["stdout"].write(json.dumps({"text": "Antrag", "page_count": 1}).encode())
        return subprocess.CompletedProcess(list(command), 0)

    monkeypatch.setattr(extractor.subprocess, "run", run)
    monkeypatch.setattr(settings, "text_layer_timeout", 90.0)
    monkeypatch.setattr(settings, "text_layer_memory_limit_mb", 300)
    daten = _textseiten("Antrag")
    ergebnisse: list[tuple[str, int | None, str]] = []

    def bearbeiten() -> None:
        ergebnisse.append(TextExtractor._extract_text(daten, "application/pdf", "antrag.pdf"))

    threads = [threading.Thread(target=bearbeiten) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert ergebnisse == [("Antrag", 1, "pypdf")] * 6
    assert hoechstens[0] == 2
    timeout, befehl = grenzen[0]
    assert timeout == 90.0
    assert befehl == [sys.executable, "-I", str(extractor._TEXT_LAYER_SCRIPT), "300"]


def test_vorgaben_der_grenzen() -> None:
    from src.config import Settings

    vorgaben = Settings()
    assert (vorgaben.text_layer_memory_limit_mb, vorgaben.text_layer_timeout, vorgaben.text_layer_concurrency) == (
        384,
        120.0,
        2,
    )


# =============================================================================
# Linux: echte Grenze des Adressraums
# =============================================================================


@linux
def test_speichergrenze_beendet_nur_den_eigenen_prozess(
    monkeypatch: pytest.MonkeyPatch, ohne_textebene_im_ingestor: list[int], ocr: list[int | None]
) -> None:
    monkeypatch.setattr(settings, "text_layer_memory_limit_mb", GRENZE_MB)

    # Kontrolle: Eine gewöhnliche Datei passt unter die Grenze
    kontrolle = TextExtractor._extract_text(_textseiten("Antrag Radweg"), "application/pdf", "antrag.pdf")
    assert kontrolle == ("Antrag Radweg", 1, "pypdf")

    # Erste Seite gewöhnlicher Text, danach Seiten mit großem Inhaltsstrom: kein Teilergebnis aus der ersten Seite
    gross = _pdf([(b"BT /F1 12 Tf 72 720 Td (Antrag Radweg) Tj ET", False)] + [(_inhaltsstrom(STROM_MIB), True)] * 3)
    tracemalloc.start()
    try:
        ergebnis = TextExtractor._extract_text(gross, "application/pdf", "vorlage.pdf")
        spitze_mib = tracemalloc.get_traced_memory()[1] / 2**20
    finally:
        tracemalloc.stop()

    assert ergebnis == ("Text aus dem Bild", None, "tesseract")
    assert ocr == [None] and ohne_textebene_im_ingestor == []
    assert spitze_mib < 8, f"Ingestor belegte {spitze_mib:.0f} MiB"


@linux
def test_eigener_prozess_meldet_sich_dem_speicherwaechter_zuerst() -> None:
    """Das Skript begrenzt seinen Adressraum und lässt dem Speicherwächter zuerst sich selbst beenden."""
    if not os.path.exists("/proc/self/oom_score_adj"):
        pytest.skip("ohne /proc")
    pruefung = (
        "import resource, runpy, sys; runpy.run_path(sys.argv[1])['_limit_memory'](64); "
        "print(open('/proc/self/oom_score_adj').read().strip(), resource.getrlimit(resource.RLIMIT_AS)[0])"
    )
    befehl = [sys.executable, "-I", "-c", pruefung, str(extractor._TEXT_LAYER_SCRIPT)]

    ausgabe = subprocess.run(befehl, capture_output=True, check=True, timeout=60).stdout.decode().split()

    assert ausgabe == ["1000", str(64 * 1024 * 1024)]

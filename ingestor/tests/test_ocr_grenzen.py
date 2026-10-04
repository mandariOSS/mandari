"""
Texterkennung mit Speicher- und Zeitgrenzen (Issue #817).

Ein künstlich großes PDF (A4, A0-Plan, 200 × 200 Zoll) zeigt, dass jede Seite einzeln und mit gedeckelter
Bildgröße gerendert wird; Unterprozesse an der Speichergrenze beenden nur die Seite, nicht den Worker. Die
Tests mit echtem ``pdftoppm``/``tesseract`` laufen nur, wenn die Werkzeuge installiert sind (CI, Image).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.extraction import extractor as extractor_modul
from src.extraction import ocr
from src.extraction.extractor import DownloadedFile, TextExtractor
from src.extraction.ocr import OcrLimits, OcrMemoryLimitError, ocr_pdf, page_dpi

A4 = (595.0, 842.0)
A0 = (2384.0, 3370.0)
#: Größte Seite nach PDF 1.4 (14 400 Punkte = 200 Zoll): bei 200 dpi 1,6 Mrd. Bildpunkte
RIESIG = (14400.0, 14400.0)

LIMITS = OcrLimits(dpi=200, max_pixels=8_000_000, memory_limit_mb=512, page_timeout=30, file_budget=600)


def pdf_bytes(pages: Sequence[tuple[float, float, str]]) -> bytes:
    """Minimales PDF mit Seiten der Größe (Breite, Höhe) in Punkten und optional einer Textzeile."""
    objects: list[bytes] = [b"", b""]  # 1 Katalog, 2 Seitenbaum (unten gefüllt)
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font = len(objects)
    kids: list[int] = []
    for width, height, text in pages:
        size = max(12.0, height / 30)
        stream = f"BT /F1 {size:.0f} Tf {width / 10:.0f} {height / 2:.0f} Td ({text}) Tj ET".encode() if text else b""
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content = len(objects)
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width:.0f} {height:.0f}] "
                f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
            ).encode()
        )
        kids.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@pytest.fixture
def grosses_pdf(tmp_path: Path) -> Path:
    """Gescannter Anlagenband ohne Textebene: A4, A0-Plan und eine Seite von 200 × 200 Zoll."""
    path = tmp_path / "anlagenband.pdf"
    path.write_bytes(pdf_bytes([(*A4, ""), (*A0, ""), (*RIESIG, "")]))
    return path


class FakeRunner:
    """Ersatz für die Unterprozesse: legt Bilder an, liefert Text je Seite, kann Fehler einspielen."""

    def __init__(self, fehler: dict[tuple[str, int], list[Any]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.limits: list[tuple[float, int]] = []
        self.fehler = fehler or {}

    def __call__(
        self, command: Sequence[str], timeout: float, memory_limit_mb: int, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        command = list(command)
        self.calls.append(command)
        self.limits.append((timeout, memory_limit_mb))
        assert env["OMP_THREAD_LIMIT"] == "1"
        if command[0] == "pdftoppm":
            page = int(command[command.index("-f") + 1])
            step = "render"
        else:
            page = int(Path(command[1]).stem.split("-")[1])
            step = "ocr"
        geplant = self.fehler.get((step, page))
        if geplant:
            fehler = geplant.pop(0)
            if isinstance(fehler, BaseException):
                raise fehler
            return subprocess.CompletedProcess(command, fehler[0], b"", fehler[1])
        if step == "render":
            Path(command[-1]).with_suffix(".pgm").write_bytes(b"P5\n1 1\n255\n\x00")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(command, 0, f"Seite {page}".encode(), b"")

    def renders(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "pdftoppm"]


def _dpi(command: list[str]) -> int:
    return int(command[command.index("-r") + 1])


def _pixel(size: tuple[float, float], dpi: int) -> float:
    return (size[0] / 72 * dpi) * (size[1] / 72 * dpi)


def test_aufloesung_je_seitengroesse_gedeckelt() -> None:
    assert page_dpi(*A4, LIMITS) == 200
    for size in (A0, RIESIG):
        dpi = page_dpi(*size, LIMITS)
        assert dpi < 200
        assert _pixel(size, dpi) <= LIMITS.max_pixels
    # Ohne Größe gilt die Grundauflösung (gerendert wird dann mit Grenze über die lange Seite)
    assert page_dpi(None, None, LIMITS) == 200


def test_grosses_pdf_wird_seitenweise_mit_gedeckelter_aufloesung_gerendert(grosses_pdf: Path) -> None:
    runner = FakeRunner()
    ergebnis = ocr_pdf(grosses_pdf, page_count=3, page_sizes=[A4, A0, RIESIG], limits=LIMITS, runner=runner)

    assert ergebnis.text == "Seite 1\n\nSeite 2\n\nSeite 3"
    renders = runner.renders()
    assert [(c[c.index("-f") + 1], c[c.index("-l") + 1]) for c in renders] == [("1", "1"), ("2", "2"), ("3", "3")]
    for command, size in zip(renders, (A4, A0, RIESIG), strict=True):
        assert "-gray" in command and "-singlefile" in command
        assert _pixel(size, _dpi(command)) <= LIMITS.max_pixels
    assert _dpi(renders[0]) == 200
    # Tesseract bekommt die Auflösung, jeder Unterprozess Speicher- und Zeitgrenze
    assert all("--dpi" in c for c in runner.calls if c[0] == "tesseract")
    assert set(runner.limits) == {(30, 512)}


def test_ohne_seitengroesse_skaliert_die_lange_seite(grosses_pdf: Path) -> None:
    runner = FakeRunner()
    ocr_pdf(grosses_pdf, page_count=1, page_sizes=None, limits=LIMITS, runner=runner)

    command = runner.renders()[0]
    assert "-r" not in command
    seite = int(command[command.index("-scale-to") + 1])
    assert seite * seite <= LIMITS.max_pixels


def test_speichergrenze_einer_seite_neuer_versuch_mit_halber_aufloesung(grosses_pdf: Path) -> None:
    runner = FakeRunner({("ocr", 2): [(-9, b"")]})
    ergebnis = ocr_pdf(grosses_pdf, page_count=3, page_sizes=[A4, A0, RIESIG], limits=LIMITS, runner=runner)

    seite_2 = [c for c in runner.renders() if c[c.index("-f") + 1] == "2"]
    assert len(seite_2) == 2
    assert _dpi(seite_2[1]) == _dpi(seite_2[0]) // 2
    assert ergebnis.text == "Seite 1\n\nSeite 2\n\nSeite 3"
    assert ergebnis.memory_limited_pages == 0


def test_seite_zweimal_an_der_speichergrenze_wird_uebersprungen(grosses_pdf: Path) -> None:
    runner = FakeRunner({("ocr", 3): [(-6, b"std::bad_alloc"), (1, b"Error in pixCreate: pix_malloc fail")]})
    ergebnis = ocr_pdf(grosses_pdf, page_count=3, page_sizes=[A4, A0, RIESIG], limits=LIMITS, runner=runner)

    assert ergebnis.text == "Seite 1\n\nSeite 2"
    assert ergebnis.memory_limited_pages == 1
    assert any("Speichergrenze" in n for n in ergebnis.notes)


def test_ohne_text_wegen_speichergrenze_meldet_eigenen_grund(grosses_pdf: Path) -> None:
    alle = {("ocr", seite): [(-9, b""), (-9, b"")] for seite in (1, 2, 3)}
    with pytest.raises(OcrMemoryLimitError, match="^Speichergrenze"):
        ocr_pdf(grosses_pdf, page_count=3, page_sizes=[A4, A0, RIESIG], limits=LIMITS, runner=FakeRunner(alle))


def test_zeitgrenze_einer_seite_und_zeitbudget_der_datei(grosses_pdf: Path) -> None:
    runner = FakeRunner({("ocr", 1): [subprocess.TimeoutExpired("tesseract", 30)]})
    ergebnis = ocr_pdf(grosses_pdf, page_count=3, page_sizes=[A4, A0, RIESIG], limits=LIMITS, runner=runner)
    assert ergebnis.timed_out_pages == 1
    assert ergebnis.text == "Seite 2\n\nSeite 3"

    zeit = iter([0.0, 0.0, 700.0])
    ergebnis = ocr_pdf(
        grosses_pdf,
        page_count=3,
        page_sizes=[A4, A0, RIESIG],
        limits=LIMITS,
        runner=FakeRunner(),
        clock=lambda: next(zeit),
    )
    assert ergebnis.budget_exhausted
    assert ergebnis.text == "Seite 1"


def test_pdf_ueber_extraktor_seitengroessen_aus_pypdf(grosses_pdf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(ocr, "tools_available", lambda: True)
    monkeypatch.setattr(ocr, "run_limited", runner)

    text, seiten, methode = extractor_modul._extract_text_from_pdf(grosses_pdf, "anlagenband.pdf")

    assert (seiten, methode) == (3, "tesseract")
    assert "Seite 3" in text
    riesig = runner.renders()[2]
    assert _pixel(RIESIG, _dpi(riesig)) <= LIMITS.max_pixels


class _Storage:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def update_file_text(self, **werte: Any) -> None:
        self.updates.append(werte)


async def test_speichergrenze_ergibt_failed_mit_grund(grosses_pdf: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alle = {("ocr", seite): [(-9, b""), (-9, b"")] for seite in (1, 2, 3)}
    monkeypatch.setattr(ocr, "tools_available", lambda: True)
    monkeypatch.setattr(ocr, "run_limited", FakeRunner(alle))
    storage = _Storage()
    kopie = grosses_pdf.with_name("kopie.pdf")
    shutil.copy(grosses_pdf, kopie)
    datei = SimpleNamespace(id="f1", body_id=None)
    geladen = DownloadedFile(path=kopie, size=kopie.stat().st_size, sha256="0" * 64, head=b"%PDF-1.4")

    assert await TextExtractor(storage)._extract_and_store(datei, geladen, "application/pdf", "plan.pdf") is False

    assert storage.updates[-1]["status"] == "failed"
    assert storage.updates[-1]["error"].startswith("Speichergrenze:")


# --- Echte Werkzeuge (CI, Image) ---------------------------------------------------------------------------

posix = pytest.mark.skipif(os.name != "posix", reason="Speichergrenze per ulimit nur unter Linux")
werkzeuge = pytest.mark.skipif(not ocr.tools_available(), reason="pdftoppm/tesseract nicht installiert")


@posix
def test_speichergrenze_des_unterprozesses_greift() -> None:
    befehl = [sys.executable, "-c", "bytearray(400 * 1024 * 1024)"]
    begrenzt = ocr.run_limited(befehl, 30, 200, ocr._environment())
    assert begrenzt.returncode != 0
    assert ocr._is_memory_failure(begrenzt) or b"MemoryError" in begrenzt.stderr
    assert ocr.run_limited(befehl, 30, 0, ocr._environment()).returncode == 0


def _pgm_groesse(path: Path) -> tuple[int, int]:
    teile = path.read_bytes()[:64].split()
    return int(teile[1]), int(teile[2])


@werkzeuge
def test_riesige_seite_wird_mit_gedeckelter_bildgroesse_gerendert(grosses_pdf: Path, tmp_path: Path) -> None:
    seiten = ocr.PageOcr(LIMITS)
    bild = seiten.render(grosses_pdf, 3, page_dpi(*RIESIG, LIMITS), tmp_path)
    breite, hoehe = _pgm_groesse(bild)
    assert breite * hoehe <= LIMITS.max_pixels
    assert bild.read_bytes()[:2] == b"P5"  # Graustufen


@werkzeuge
def test_texterkennung_auf_a0_plan_mit_echten_werkzeugen(tmp_path: Path) -> None:
    plan = tmp_path / "plan.pdf"
    plan.write_bytes(pdf_bytes([(*A0, "Bebauungsplan Nordviertel")]))

    ergebnis = ocr_pdf(plan, page_count=1, page_sizes=[A0], limits=LIMITS)

    assert ergebnis.pages_rendered == 1
    assert "Bebauungsplan" in ergebnis.text

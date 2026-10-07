# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Texterkennung eines Bildausschnitts mit Tesseract (Issue #915).

Tesseract läuft wie in ``mandari_dokumente.ocr`` als Unterprozess mit einem Thread (``OMP_THREAD_LIMIT=1``), mit
Zeitgrenze und unter Linux mit einer Grenze für den Adressraum (``ulimit -v`` über ``/bin/sh``). Das Bild geht als
PNG über die Standardeingabe hinein, der Text über die Standardausgabe heraus: Es entsteht keine Datei.

Erlaubte Zeichen (``zeichen``, z. B. nur Ziffern, Punkt und „TOP“ im TOP-Feld) gehen als
``-c tessedit_char_whitelist=…`` mit; leer = alle Zeichen.

Einstellungen: ``LIVE_TESSERACT_CMD`` (Standard ``tesseract``), ``LIVE_TESSDATA_DIR`` (leer = Vorgabe von Tesseract),
``LIVE_OCR_MEMORY_LIMIT_MB`` (Standard 256), ``LIVE_OCR_TIMEOUT_SECONDS`` (Standard 20).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Final

from django.conf import settings
from PIL import Image

#: Texterkennung eines Ausschnitts: (Bild, psm, erlaubte Zeichen oder leer) → Text; in Tests ersetzbar
Erkenner = Callable[[Image.Image, int, str], str]

SPRACHE: Final = "deu"


class OcrError(RuntimeError):
    """Tesseract ist gescheitert (Exit-Code, Zeitgrenze); die Meldung ist ein fester Text."""


def _programm() -> str:
    return str(getattr(settings, "LIVE_TESSERACT_CMD", "") or "tesseract")


def verfuegbar() -> bool:
    """Gibt es das Tesseract-Programm?"""
    programm = _programm()
    return shutil.which(programm) is not None or Path(programm).is_file()


def _tessdata() -> list[str]:
    tessdata = str(getattr(settings, "LIVE_TESSDATA_DIR", "") or "")
    return ["--tessdata-dir", tessdata] if tessdata else []


def sprache_verfuegbar() -> bool:
    """Gibt es Tesseract mit den Sprachdaten ``deu`` (``--list-langs``)?"""
    if not verfuegbar():
        return False
    try:
        ergebnis = subprocess.run(  # noqa: S603 – fester Befehl, Programm aus den Einstellungen
            [_programm(), "--list-langs", *_tessdata()], capture_output=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    ausgabe = (ergebnis.stdout or b"").decode("utf-8", errors="replace")
    return SPRACHE in {zeile.strip() for zeile in ausgabe.splitlines()}


def _befehl(psm: int, zeichen: str = "") -> list[str]:
    befehl = [_programm(), "stdin", "stdout", "-l", SPRACHE, "--psm", str(psm), *_tessdata()]
    if zeichen:
        befehl += ["-c", f"tessedit_char_whitelist={zeichen}"]
    speicher = int(getattr(settings, "LIVE_OCR_MEMORY_LIMIT_MB", 256))
    if speicher > 0 and os.name == "posix" and Path("/bin/sh").exists():
        return ["/bin/sh", "-c", 'ulimit -v "$0" && exec "$@"', str(speicher * 1024), *befehl]
    return befehl


def tesseract(bild: Image.Image, psm: int, zeichen: str = "") -> str:
    """Text eines Ausschnitts (nur ``zeichen``, falls angegeben); wirft ``OcrError``."""
    puffer = io.BytesIO()
    bild.save(puffer, format="PNG")
    umgebung = dict(os.environ)
    # Ein Thread genügt für einen kleinen Ausschnitt; sonst startet Tesseract je Kern einen
    umgebung["OMP_THREAD_LIMIT"] = "1"
    try:
        ergebnis = subprocess.run(  # noqa: S603 – fester Befehl, Programm aus den Einstellungen
            _befehl(psm, zeichen),
            input=puffer.getvalue(),
            capture_output=True,
            timeout=float(getattr(settings, "LIVE_OCR_TIMEOUT_SECONDS", 20)),
            env=umgebung,
            check=False,
        )
    except subprocess.TimeoutExpired as fehler:
        raise OcrError("Zeitgrenze der Texterkennung") from fehler
    except OSError as fehler:
        raise OcrError("Tesseract nicht startbar") from fehler
    if ergebnis.returncode != 0:
        raise OcrError(f"Tesseract mit Exit-Code {ergebnis.returncode}")
    return (ergebnis.stdout or b"").decode("utf-8", errors="replace")

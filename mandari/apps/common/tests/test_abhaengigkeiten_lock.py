# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Python-Abhängigkeiten der Django-Anwendung: eine Quelle (``pyproject.toml``), eine Lock-Datei
(``uv.lock``), aus der Image, CI, pip-audit und SBOM lesen (Issue #682).

Vorher standen die Abhängigkeiten in ``requirements.txt`` (Untergrenzen) und ``requirements.lock``.
Dependabot hob nur die Untergrenzen an, das Lockfile blieb alt, und jeder seiner PRs scheiterte.
Die Tests halten fest, was die Umstellung zusichert – ohne Netz; ob ``uv.lock`` im Ganzen zu
``pyproject.toml`` passt, prüft die CI mit ``uv lock --check``.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO = Path(__file__).resolve().parents[4]
PYPROJECT = REPO / "mandari" / "pyproject.toml"
UV_LOCK = REPO / "mandari" / "uv.lock"
EXPORT = REPO / "scripts" / "export_requirements.sh"
VERSIONSPRUEFUNG = REPO / "scripts" / "check_version_consistency.py"

# Lokales Paket aus ../shared: steht in uv.lock, wird aber getrennt installiert
LOKAL = {"mandari-oparl"}


def _direkte() -> list[Requirement]:
    projekt = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    return [Requirement(eintrag) for eintrag in projekt["dependencies"]]


def _gesperrt() -> dict[str, set[str]]:
    pakete = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]
    versionen: dict[str, set[str]] = {}
    for paket in pakete:
        if "version" in paket:
            versionen.setdefault(canonicalize_name(paket["name"]), set()).add(paket["version"])
    return versionen


def test_alte_requirements_dateien_sind_weg() -> None:
    # Eine zweite Liste neben pyproject.toml liefe wieder auseinander (und Dependabot läse sie mit).
    for name in ("requirements.txt", "requirements.lock"):
        assert not (REPO / "mandari" / name).exists(), (
            f"mandari/{name} ist zurück – Abhängigkeiten gehören in mandari/pyproject.toml, danach `uv lock`"
        )


def test_jede_direkte_abhaengigkeit_ist_gesperrt_und_erfuellt_ihre_grenze() -> None:
    gesperrt = _gesperrt()
    probleme = []
    for anforderung in _direkte():
        versionen = gesperrt.get(canonicalize_name(anforderung.name))
        if not versionen:
            probleme.append(f"{anforderung.name}: fehlt in uv.lock")
        elif not all(anforderung.specifier.contains(Version(v), prereleases=True) for v in versionen):
            probleme.append(
                f"{anforderung.name}: uv.lock hat {sorted(versionen)}, verlangt ist {anforderung.specifier}"
            )
    assert not probleme, "uv.lock passt nicht zu pyproject.toml (`uv lock` im Ordner mandari):\n" + "\n".join(probleme)


def test_kein_pymupdf_in_den_abhaengigkeiten() -> None:
    """Fitnessfunktion aus docs/adr/20260930-pdf-seitenanalyse-bibliothek.md: PyMuPDF (AGPL/kommerziell) bleibt draußen."""
    verboten = {"pymupdf", "pymupdfb", "fitz"}
    assert not verboten & set(_gesperrt()), "PyMuPDF ist gesperrt – siehe ADR zur PDF-Seitenanalyse"
    assert not verboten & {canonicalize_name(a.name) for a in _direkte()}


def test_projekt_ist_kein_paket_und_nutzt_das_lokale_shared_paket() -> None:
    uv = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["uv"]
    assert uv["package"] is False, "mandari ist eine Anwendung; uv darf sie nicht als Paket bauen"
    quelle = uv["sources"]["mandari-oparl"]
    assert quelle["path"] == "../shared", "mandari-oparl muss aus dem Repo kommen, nicht aus einer Paketquelle"


@pytest.fixture(scope="module")
def versionspruefung() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_version_consistency", VERSIONSPRUEFUNG)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def test_versionspruefung_liest_die_projektversion_aus_uv_lock(versionspruefung: ModuleType) -> None:
    assert versionspruefung.aus_uv_lock() == versionspruefung.aus_pyproject()


def test_versionspruefung_meldet_abweichende_lock_datei(
    versionspruefung: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Version in pyproject.toml angehoben, ``uv lock`` vergessen: Der Image-Build bräche sonst erst später ab."""
    aktuell = versionspruefung.aus_pyproject()
    veraltet = tmp_path / "uv.lock"
    veraltet.write_text(
        UV_LOCK.read_text(encoding="utf-8").replace(
            f'name = "mandari"\nversion = "{aktuell}"', 'name = "mandari"\nversion = "0.0.1"'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(versionspruefung, "UV_LOCK", veraltet)
    monkeypatch.setattr(sys, "argv", ["check_version_consistency.py"])

    assert versionspruefung.main() == 1
    assert "mandari/uv.lock sagt 0.0.1" in capsys.readouterr().out


def _sh() -> str | None:
    if sys.platform == "win32":
        # Git Bash; das sh.exe aus System32 gibt es nicht, bash.exe dort wäre WSL
        kandidat = Path(r"C:\Program Files\Git\usr\bin\sh.exe")
        return str(kandidat) if kandidat.exists() else None
    return shutil.which("sh")


@pytest.mark.skipif(shutil.which("uv") is None or _sh() is None, reason="uv oder sh nicht vorhanden")
def test_export_liefert_nur_laufzeitpakete_mit_fester_version(tmp_path: Path) -> None:
    """Was CI, pip-audit und SBOM installieren bzw. prüfen: alle direkten Abhängigkeiten, keine Werkzeuge."""
    ziel = tmp_path / "requirements.lock"
    sh = _sh()
    assert sh is not None
    subprocess.run([sh, str(EXPORT), str(ziel)], check=True, capture_output=True, timeout=120)  # noqa: S603

    zeilen = [z for z in ziel.read_text(encoding="utf-8").splitlines() if z and not z.lstrip().startswith("#")]
    assert zeilen, "Export ist leer"
    namen = set()
    for zeile in zeilen:
        treffer = re.match(r"^([A-Za-z0-9._-]+)==[^\s;]+(\s*;.*)?$", zeile)
        assert treffer, f"keine feste Version: {zeile}"
        namen.add(canonicalize_name(treffer.group(1)))

    erwartet = {canonicalize_name(a.name) for a in _direkte()} - LOKAL
    assert erwartet <= namen, f"fehlen im Export: {sorted(erwartet - namen)}"
    assert not namen & LOKAL, "das lokale Paket aus ../shared gehört nicht in den Export"
    assert not namen & {"pytest", "mypy", "ruff", "djlint"}, "Entwicklungswerkzeuge gehören nicht ins Image"

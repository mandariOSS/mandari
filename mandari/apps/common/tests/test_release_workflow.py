# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Release-Workflow: Website-Tags und Prüfung am Ende (Issue #698).

``.github/workflows/release.yml`` gibt dem Image ``website`` dieselben Tags wie mandari und ingestor, als Kopie von
``website:main``. Ins Paket ``website`` schreibt auch mandariOSS/marketing-website eigene Commit-Tags
(``main-<website-commit>``); eine gleiche Kurzkennung in beiden Repositorys darf ein solches Tag nicht überschreiben.
Beweglich sind nur ``latest`` und ``dev``.

Der Schritt läuft hier unverändert aus der Workflow-Datei gegen ein nachgebautes ``docker`` (Registry als Dateien).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

WURZEL = Path(__file__).resolve().parents[4]
RELEASE_WORKFLOW = WURZEL / ".github" / "workflows" / "release.yml"
WEBSITE = "ghcr.io/mandarioss/website"

pytestmark = pytest.mark.skipif(not RELEASE_WORKFLOW.exists(), reason="Workflow nur im Repository, nicht im Image")

# Registry als Verzeichnis: registry/website_main enthält das Manifest von website:main
FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> docker.log
case "$1 $2 $3" in
  "buildx imagetools inspect")
    ref="${*: -1}"
    name="${ref##*/}"
    datei="registry/${name//:/_}"
    if [ -f "$datei" ]; then cat "$datei"; exit 0; fi
    echo "ERROR: $ref: not found" >&2
    exit 1
    ;;
esac
exit 0
"""


def _bash() -> str | None:
    if os.name == "nt":
        # Git Bash; das bash.exe aus System32 wäre WSL
        kandidat = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "usr" / "bin" / "bash.exe"
        return str(kandidat) if kandidat.exists() else None
    return shutil.which("bash")


BASH = _bash()


def _schritte() -> list[dict[str, Any]]:
    daten: dict[Any, Any] = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    schritte: list[dict[str, Any]] = daten["jobs"]["build"]["steps"]
    return schritte


def _schritt(schritt_id: str) -> dict[str, Any]:
    for schritt in _schritte():
        if schritt.get("id") == schritt_id:
            return schritt
    raise AssertionError(f"Schritt {schritt_id} fehlt im Job build")


@dataclass
class Lauf:
    rc: int
    aufrufe: list[str]
    ausgabe: str

    @property
    def gesetzt(self) -> list[str]:
        """Tags des Aufrufs ``imagetools create`` (leer, wenn es keinen gab)."""
        for zeile in self.aufrufe:
            if zeile.startswith("buildx imagetools create"):
                teile = zeile.split()
                return [teile[i + 1] for i, teil in enumerate(teile) if teil == "--tag"]
        return []


def _website_schritt(tmp_path: Path, tags: list[str], registry: dict[str, str]) -> Lauf:
    if BASH is None:
        pytest.skip("bash nicht vorhanden")
    (tmp_path / "registry").mkdir()
    for tag, inhalt in registry.items():
        (tmp_path / "registry" / f"website_{tag}").write_text(inhalt, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_bytes(FAKE_DOCKER.encode())
    docker.chmod(0o755)
    (tmp_path / "docker.log").touch()
    skript = tmp_path / "schritt.sh"
    skript.write_bytes(_schritt("website")["run"].encode())

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "WEBSITE_SOURCE": f"{WEBSITE}:main",
            "WEBSITE_TAGS": ",".join(f"{WEBSITE}:{tag}" for tag in tags),
        }
    )
    ergebnis = subprocess.run(  # noqa: S603 — fester Aufruf im Test
        [BASH, "--noprofile", "--norc", "-eo", "pipefail", "schritt.sh"],  # wie "shell: bash" in Actions
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    return Lauf(
        rc=ergebnis.returncode,
        aufrufe=(tmp_path / "docker.log").read_text(encoding="utf-8").splitlines(),
        ausgabe=ergebnis.stdout + ergebnis.stderr,
    )


def test_commit_tag_der_website_wird_nicht_ueberschrieben(tmp_path: Path) -> None:
    """``dev-abc1234`` stammt schon aus marketing-website (anderer Inhalt): stehen lassen, ``dev`` trotzdem setzen."""
    lauf = _website_schritt(
        tmp_path,
        ["dev", "dev-abc1234"],
        {"main": "manifest-main", "dev": "manifest-alt", "dev-abc1234": "manifest-website-commit"},
    )

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.gesetzt == [f"{WEBSITE}:dev"], lauf.aufrufe
    assert f"::warning title=Website-Tag nicht überschrieben::{WEBSITE}:dev-abc1234" in lauf.ausgabe, lauf.ausgabe


def test_neue_tags_und_bewegliche_tags_werden_gesetzt(tmp_path: Path) -> None:
    """Release: ``v0.12.0`` gibt es noch nicht, ``latest`` zeigt auf einen alten Stand – beide setzen."""
    lauf = _website_schritt(tmp_path, ["v0.12.0", "latest"], {"main": "manifest-main", "latest": "manifest-februar"})

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.gesetzt == [f"{WEBSITE}:v0.12.0", f"{WEBSITE}:latest"], lauf.aufrufe
    assert "::warning" not in lauf.ausgabe


def test_von_hand_nachgetragenes_release_tag_bleibt(tmp_path: Path) -> None:
    """``website:v0.11.0`` wurde aus dem Website-Stand zum Release nachgetragen; ein erneuter Lauf ändert es nicht."""
    lauf = _website_schritt(tmp_path, ["v0.11.0", "latest"], {"main": "manifest-main", "v0.11.0": "manifest-release"})

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.gesetzt == [f"{WEBSITE}:latest"], lauf.aufrufe


def test_erneuter_lauf_mit_gleichem_inhalt_setzt_das_tag_erneut(tmp_path: Path) -> None:
    lauf = _website_schritt(tmp_path, ["dev", "dev-abc1234"], {"main": "manifest-main", "dev-abc1234": "manifest-main"})

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.gesetzt == [f"{WEBSITE}:dev", f"{WEBSITE}:dev-abc1234"], lauf.aufrufe
    assert "::warning" not in lauf.ausgabe


def test_ohne_zu_setzende_tags_kein_aufruf(tmp_path: Path) -> None:
    lauf = _website_schritt(tmp_path, ["main-abc1234"], {"main": "manifest-main", "main-abc1234": "anderes"})

    assert lauf.rc == 0, lauf.ausgabe
    assert not any(z.startswith("buildx imagetools create") for z in lauf.aufrufe), lauf.aufrufe


def test_ohne_quelle_bricht_der_schritt_ab(tmp_path: Path) -> None:
    lauf = _website_schritt(tmp_path, ["dev"], {})

    assert lauf.rc != 0, lauf.ausgabe
    assert not any(z.startswith("buildx imagetools create") for z in lauf.aufrufe), lauf.aufrufe


def test_scheitert_der_website_schritt_laufen_sbom_sichtbarkeit_und_pruefung_trotzdem() -> None:
    """Sonst fehlte gerade dann die Übersicht, welche Tags abrufbar sind."""
    schritte = _schritte()
    position = next(i for i, schritt in enumerate(schritte) if schritt.get("id") == "website")
    danach = schritte[position + 1 :]
    assert danach, "nach dem Website-Schritt folgen SBOM, Sichtbarkeit und Prüfung"
    for schritt in danach:
        bedingung = str(schritt.get("if", ""))
        assert "!cancelled()" in bedingung and "steps.tags.outcome == 'success'" in bedingung, schritt["name"]
    assert "scripts/check_image_tags.py" in str(danach[-1].get("run", "")), "die Prüfung ist der letzte Schritt"


def test_pruefung_bekommt_die_tags_ueber_die_umgebung() -> None:
    """Bei ``workflow_dispatch`` stammt das Tag aus einer freien Eingabe – nicht in den Skripttext einsetzen."""
    pruefung = _schritte()[-1]
    assert pruefung["env"]["RUN_TAGS"] == "${{ steps.tags.outputs.run_tags }}"
    assert "${{" not in pruefung["run"], pruefung["run"]
    assert '"$RUN_TAGS"' in pruefung["run"]

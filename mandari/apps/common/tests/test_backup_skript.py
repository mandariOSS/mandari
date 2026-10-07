# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``backup.sh``: Die Prüfung nach einer Wiederherstellung (``verify_installation``) nennt nur echte Container.

Die Liste der Container ist eine Shell-Liste über mehrere Zeilen. Ein verrutschtes ``\\n`` statt eines
Zeilenumbruchs wurde dort zu einem eigenen Eintrag „n“, und nach jeder Wiederherstellung stand eine rote Zeile
„n ✗ missing“ da (Issue #915). ``bash -n`` und der CI-Job „Sichern und wiederherstellen“ fangen das nicht ab.
Die Funktion läuft hier gegen ein nachgebautes ``docker``, das jeden geprüften Container protokolliert.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parents[4]
SKRIPT = WURZEL / "backup.sh"
COMPOSE = WURZEL / "docker-compose.yml"

FAKE_DOCKER = """#!/usr/bin/env bash
# docker inspect --format=… <container>: Container protokollieren, laufend und gesund melden
for letztes in "$@"; do :; done
echo "$letztes" >> "$DOCKER_LOG"
case "$*" in
  *Health*) echo "healthy" ;;
  *) echo "running" ;;
esac
"""


def _bash() -> str | None:
    if os.name == "nt":
        # Git Bash; das bash.exe aus System32 wäre WSL
        kandidat = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "usr" / "bin" / "bash.exe"
        return str(kandidat) if kandidat.exists() else None
    return shutil.which("bash")


def _funktion(name: str) -> str:
    """Quelltext einer Shell-Funktion aus ``backup.sh`` (bis zur schließenden Klammer am Zeilenanfang)."""
    text = SKRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
    treffer = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.MULTILINE | re.DOTALL)
    assert treffer is not None, f"{name} fehlt in backup.sh"
    return treffer.group(0)


def _compose_container(projekt: str) -> set[str]:
    namen = re.findall(r"container_name:\s*\$\{COMPOSE_PROJECT_NAME:-mandari\}(\S*)", COMPOSE.read_text("utf-8"))
    return {f"{projekt}{rest}" for rest in namen}


def test_pruefung_nach_wiederherstellung_nennt_nur_container_aus_compose(tmp_path: Path) -> None:
    bash = _bash()
    if bash is None:
        pytest.skip("bash nicht vorhanden")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_bytes(FAKE_DOCKER.encode())
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()
    skript = tmp_path / "pruefung.sh"
    skript.write_bytes(
        (
            "COMPOSE_PROJECT_NAME=muster\nGREEN=''; YELLOW=''; RED=''; NC=''\nlog() { :; }\n"
            f"{_funktion('verify_installation')}verify_installation\n"
        ).encode()
    )
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DOCKER_LOG": str(log)}

    ergebnis = subprocess.run(  # noqa: S603 — fester Aufruf im Test
        [bash, str(skript)], env=env, capture_output=True, timeout=60, check=False
    )

    ausgabe = (ergebnis.stdout + ergebnis.stderr).decode("utf-8", errors="replace")
    assert ergebnis.returncode == 0, ausgabe
    geprueft = log.read_text(encoding="utf-8").split()
    # Je Container zwei Abfragen (Zustand, Gesundheit); jeder Eintrag ist ein Container aus docker-compose.yml
    assert set(geprueft) <= _compose_container("muster"), geprueft
    assert {"muster-worker", "muster-worker-heavy", "muster-worker-live"} <= set(geprueft)
    assert "missing" not in ausgabe, ausgabe
    zeilen = [z for z in ausgabe.splitlines() if z.strip()]
    assert len(zeilen) == len(set(geprueft)), ausgabe

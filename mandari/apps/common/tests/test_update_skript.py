# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``update.sh``: Der Ingestor steht während aller Migrationen (wie ``deploy/scripts/deploy.sh``), der
Protokoll-Orchestrator wechselt danach auf das neue Image (Issue #479).

Das Skript läuft gegen ein nachgebautes ``docker`` (protokolliert nur die Aufrufe), damit die CI
die Reihenfolge ohne Docker prüft: erst Worker anhalten, dann migrieren, Worker erst nach den
Post-Deploy-Migrationen mit neuem Image starten – und bei Abbruch trotzdem wieder starten.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SKRIPT = Path(__file__).resolve().parents[4] / "update.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
case "$1" in
  inspect)
    case "$2" in
      *Health*) if [ -n "${FAKE_UNHEALTHY:-}" ]; then echo unhealthy; else echo healthy; fi ;;
      *State.Status*) echo running ;;
      *Config.Image*) echo "ghcr.io/mandarioss/mandari:alt" ;;
    esac
    ;;
  compose)
    if [ "$2" = config ] && [ "$3" = --services ]; then
      printf '%s\\n' ${FAKE_SERVICES:-postgres redis elasticsearch mandari minutes-orchestrator website ingestor caddy}
    fi
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
pytestmark = pytest.mark.skipif(BASH is None, reason="bash nicht vorhanden")


def _lauf(
    tmp_path: Path, *, env_zusatz: dict[str, str] | None = None, args: tuple[str, ...] = ()
) -> tuple[int, list[str]]:
    arbeit = tmp_path / "mandari"
    arbeit.mkdir()
    # LF erzwingen (Checkout unter Windows kann CRLF liefern)
    (arbeit / "update.sh").write_bytes(SKRIPT.read_bytes().replace(b"\r\n", b"\n"))
    (arbeit / ".env").write_text("IMAGE_TAG=v1.0.0\nDOMAIN=localhost\n", encoding="utf-8")
    (arbeit / ".env.pre-update").write_text("IMAGE_TAG=v0.9.0\nDOMAIN=localhost\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_bytes(FAKE_DOCKER.encode())
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()

    env = {k: v for k, v in os.environ.items() if k not in {"WORKER_SERVICES", "COMPOSE_PROJECT_NAME"}}
    env.update({"PATH": f"{bin_dir}{os.pathsep}{env['PATH']}", "DOCKER_LOG": str(log), **(env_zusatz or {})})
    assert BASH is not None
    ergebnis = subprocess.run(  # noqa: S603 — fester Aufruf im Test
        [BASH, "update.sh", *(args or ("--tag", "v1.1.0", "--no-backup", "--no-cleanup"))],
        cwd=arbeit,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    return ergebnis.returncode, log.read_text(encoding="utf-8").splitlines()


def _index(zeilen: list[str], teil: str) -> int:
    treffer = [i for i, zeile in enumerate(zeilen) if teil in zeile]
    assert treffer, f"Aufruf fehlt: {teil}\n" + "\n".join(zeilen)
    return treffer[0]


def test_ingestor_steht_waehrend_aller_migrationen(tmp_path: Path) -> None:
    rc, aufrufe = _lauf(tmp_path)

    assert rc == 0, "\n".join(aufrufe)
    angehalten = _index(aufrufe, "compose stop ingestor")
    vor_migration = _index(aufrufe, "manage.py safemigrate")
    nach_migration = _index(aufrufe, "exec mandari python manage.py migrate --noinput")
    gestartet = _index(aufrufe, "compose up -d --no-deps ingestor")
    assert angehalten < vor_migration < nach_migration < gestartet
    assert sum("compose up -d --no-deps ingestor" in z for z in aufrufe) == 1


def test_worker_aus_env_nur_wenn_definiert(tmp_path: Path) -> None:
    rc, aufrufe = _lauf(tmp_path, env_zusatz={"WORKER_SERVICES": "ingestor ocr-worker minutes-orchestrator"})

    assert rc == 0, "\n".join(aufrufe)
    # ocr-worker ist in dieser Installation nicht definiert und wird übersprungen
    assert _index(aufrufe, "compose stop ingestor minutes-orchestrator") < _index(aufrufe, "manage.py safemigrate")
    assert not any("ocr-worker" in z for z in aufrufe)
    assert _index(aufrufe, "exec mandari python manage.py migrate --noinput") < _index(
        aufrufe, "compose up -d --no-deps ingestor minutes-orchestrator"
    )


def test_abbruch_startet_worker_wieder(tmp_path: Path) -> None:
    """Scheitert das Umschalten, rollt das Skript zurück, bricht ab – und startet den Ingestor wieder."""
    rc, aufrufe = _lauf(tmp_path, env_zusatz={"FAKE_UNHEALTHY": "1"})

    assert rc != 0
    assert _index(aufrufe, "compose stop ingestor") < _index(aufrufe, "compose up -d --no-deps ingestor")
    assert not any("exec mandari python manage.py migrate --noinput" in z for z in aufrufe)


def test_orchestrator_steht_waehrend_der_migrationen_und_wechselt_danach(tmp_path: Path) -> None:
    rc, aufrufe = _lauf(tmp_path)

    assert rc == 0, "\n".join(aufrufe)
    assert _index(aufrufe, "compose stop ingestor minutes-orchestrator") < _index(aufrufe, "manage.py safemigrate")
    assert _index(aufrufe, "exec mandari python manage.py migrate --noinput") < _index(
        aufrufe, "compose up -d --no-deps ingestor minutes-orchestrator"
    )
    assert sum("minutes-orchestrator" in z and "compose up" in z for z in aufrufe) == 1, "genau ein Start"


def test_orchestrator_wechselt_auch_ohne_eintrag_in_worker_services(tmp_path: Path) -> None:
    """Eigenes WORKER_SERVICES ohne Orchestrator: Er läuft weiter und wechselt nach den Migrationen."""
    rc, aufrufe = _lauf(tmp_path, env_zusatz={"WORKER_SERVICES": "ingestor"})

    assert rc == 0, "\n".join(aufrufe)
    assert not any("stop" in z and "minutes-orchestrator" in z for z in aufrufe)
    assert _index(aufrufe, "exec mandari python manage.py migrate --noinput") < _index(
        aufrufe, "compose up -d --no-deps minutes-orchestrator"
    )


def test_ohne_orchestrator_dienst_kein_aufruf(tmp_path: Path) -> None:
    rc, aufrufe = _lauf(
        tmp_path, env_zusatz={"FAKE_SERVICES": "postgres redis elasticsearch mandari website ingestor caddy"}
    )

    assert rc == 0, "\n".join(aufrufe)
    assert not any("minutes-orchestrator" in z for z in aufrufe)
    assert _index(aufrufe, "compose stop ingestor") < _index(aufrufe, "manage.py safemigrate")


def test_rueckfall_setzt_auch_den_orchestrator_zurueck(tmp_path: Path) -> None:
    rc, aufrufe = _lauf(tmp_path, args=("--rollback",))

    assert rc == 0, "\n".join(aufrufe)
    assert any("compose up -d --no-deps ingestor minutes-orchestrator" in z for z in aufrufe), "\n".join(aufrufe)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``deploy/scripts/deploy.sh``: Nach dem Umschalten prüft das Skript neben den Seiten auch die Worker.

Ein Ingestor-Image, das beim Start mit einem Importfehler abbricht, fiel der reinen Seitenprüfung nicht
auf. Jetzt gilt: Jede Beendigung eines Worker-Containers mit Exit-Code ungleich 0 im Prüffenster führt
zum Rückfall wie eine fehlgeschlagene Seitenprüfung. Exit 0 ist planmäßig (die Worker enden nach jedem
Durchlauf und werden neu gestartet).

Das Skript läuft gegen ein nachgebautes ``docker`` (protokolliert die Aufrufe, liefert vorgegebene
Ereignisse und Zustände), damit die CI den Ablauf ohne Docker prüft.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SKRIPT = Path(__file__).resolve().parents[4] / "deploy" / "scripts" / "deploy.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
case "$1" in
  compose)
    shift
    while [ "$1" = "-f" ]; do shift 2; done
    case "$1" in
      ps)
        dienst="${@: -1}"
        if [ "$dienst" = mandari ]; then echo app-id
        elif [ -z "${FAKE_OHNE_WORKER:-}" ]; then echo "id-$dienst"
        fi
        ;;
      exec)
        [ "$3" = postgres ] && echo DUMP
        ;;
      config)
        [ "$2" = --services ] && printf '%s\\n' ${FAKE_SERVICES:-mandari ingestor postgres}
        ;;
    esac
    ;;
  exec)
    if [[ "$*" == *verify_deploy* ]]; then
      if [ -n "${FAKE_SEITEN_FEHLER:-}" ]; then echo "ERGEBNIS: 1 Prüfung(en) fehlgeschlagen"
      else echo "ERGEBNIS: alle Prüfungen bestanden"
      fi
    fi
    ;;
  events)
    if [ -n "${FAKE_EVENTS:-}" ]; then printf '%b\\n' "$FAKE_EVENTS"; fi
    ;;
  inspect)
    if [[ "$*" == *Health* ]]; then
      # Healthcheck-Zustand; leer = Container ohne Healthcheck
      echo "${FAKE_HEALTH:-}"
    else
      echo "/worker-1 ${FAKE_STATUS:-running} ${FAKE_EXIT:-0} ${FAKE_RESTARTS:-3}"
    fi
    ;;
esac
exit 0
"""


def _sh() -> str | None:
    if os.name == "nt":
        # Git Bash; das bash.exe aus System32 wäre WSL
        kandidat = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "usr" / "bin" / "sh.exe"
        return str(kandidat) if kandidat.exists() else None
    # Auf dem Server läuft das Skript mit sh (dash), also auch hier
    return shutil.which("sh")


SH = _sh()
pytestmark = pytest.mark.skipif(SH is None, reason="sh nicht vorhanden")


def _lauf(tmp_path: Path, *args: str, **fake: str) -> tuple[int, str, list[str], Path]:
    arbeit = tmp_path / "mandari"
    arbeit.mkdir()
    skript = tmp_path / "deploy.sh"
    # LF erzwingen (Checkout unter Windows kann CRLF liefern)
    skript.write_bytes(SKRIPT.read_bytes().replace(b"\r\n", b"\n"))
    (arbeit / ".env").write_text("IMAGE_TAG=dev-alt\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_bytes(FAKE_DOCKER.encode())
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    log.touch()

    env = {k: v for k, v in os.environ.items() if not k.startswith(("WORKER_", "VERIFY_", "NOTIFY_", "FAKE_"))}
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "DOCKER_LOG": str(log),
            "MANDARI_DIR": str(arbeit),
            "WORKER_SERVICES": "ingestor ocr-worker",
            "WORKER_CHECK_SECONDS": "1",
        }
    )
    env.update(fake)
    assert SH is not None
    ergebnis = subprocess.run(  # noqa: S603 — fester Aufruf im Test
        [SH, "../deploy.sh", *args],
        cwd=arbeit,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    ausgabe = ergebnis.stdout + ergebnis.stderr
    return ergebnis.returncode, ausgabe, log.read_text(encoding="utf-8").splitlines(), arbeit


def _tag(arbeit: Path) -> str:
    return (arbeit / ".env").read_text(encoding="utf-8").strip().removeprefix("IMAGE_TAG=")


def _deploy_log(arbeit: Path) -> str:
    return (arbeit / "deploy-log.tsv").read_text(encoding="utf-8")


def test_worker_mit_exit_0_sind_kein_fehler(tmp_path: Path) -> None:
    rc, ausgabe, aufrufe, arbeit = _lauf(
        tmp_path, "apply", "dev-neu", FAKE_EVENTS="worker-1 0\\nworker-1 0\\nworker-2 0"
    )

    assert rc == 0, ausgabe
    assert _tag(arbeit) == "dev-neu"
    assert "\tok\t" in _deploy_log(arbeit)
    assert "planmaessig" in ausgabe
    # Beobachtet werden genau die Worker-Container, nur Beendigungen
    events = next(z for z in aufrufe if z.startswith("events"))
    assert "--filter event=die" in events
    assert "--filter container=id-ingestor" in events
    assert "--filter container=id-ocr-worker" in events
    assert "--since" in events


def test_worker_mit_exit_ungleich_0_fuehrt_zum_rueckfall(tmp_path: Path) -> None:
    """Beispiel: Image bricht beim Start mit ModuleNotFoundError ab (Exit 1) und startet immer wieder."""
    rc, ausgabe, aufrufe, arbeit = _lauf(tmp_path, "apply", "dev-neu", FAKE_EVENTS="worker-1 1\\nworker-1 1")

    assert rc == 1, ausgabe
    assert _tag(arbeit) == "dev-alt", "Rückfall auf den alten Stand"
    assert "rollback-automatisch" in _deploy_log(arbeit)
    assert "FAIL worker-1 beendet mit Exit 1" in ausgabe
    assert "die Worker-Pruefung" in ausgabe
    # Nach dem Rückfall laufen die Worker wieder (mit dem alten Image)
    assert sum("up -d --no-deps ingestor ocr-worker" in z for z in aufrufe) == 2


def test_endzustand_mit_exit_ungleich_0_fuehrt_zum_rueckfall(tmp_path: Path) -> None:
    """Ohne aufgezeichnetes Ereignis zählt der Endzustand: neu startend mit Exit 1."""
    rc, ausgabe, _, arbeit = _lauf(tmp_path, "apply", "dev-neu", FAKE_STATUS="restarting", FAKE_EXIT="1")

    assert rc == 1, ausgabe
    assert _tag(arbeit) == "dev-alt"
    assert "FAIL worker-1: restarting, Exit 1" in ausgabe


def test_beendet_mit_exit_0_im_endzustand_ist_in_ordnung(tmp_path: Path) -> None:
    rc, ausgabe, _, arbeit = _lauf(tmp_path, "apply", "dev-neu", FAKE_STATUS="exited", FAKE_EXIT="0")

    assert rc == 0, ausgabe
    assert _tag(arbeit) == "dev-neu"


def test_worker_ohne_container_fuehrt_zum_rueckfall(tmp_path: Path) -> None:
    rc, ausgabe, _, arbeit = _lauf(tmp_path, "apply", "dev-neu", FAKE_OHNE_WORKER="1")

    assert rc == 1, ausgabe
    assert _tag(arbeit) == "dev-alt"
    assert "FAIL ingestor: kein Container" in ausgabe


def test_seitenpruefung_und_workerpruefung_werden_beide_genannt(tmp_path: Path) -> None:
    rc, ausgabe, _, arbeit = _lauf(tmp_path, "apply", "dev-neu", FAKE_SEITEN_FEHLER="1", FAKE_EVENTS="worker-1 1")

    assert rc == 1, ausgabe
    assert _tag(arbeit) == "dev-alt"
    assert "die Anwendungspruefung und die Worker-Pruefung" in ausgabe


def test_verify_prueft_auch_die_worker(tmp_path: Path) -> None:
    rc, ausgabe, _, _ = _lauf(tmp_path, "verify", FAKE_EVENTS="worker-1 137")
    assert rc == 1, ausgabe
    assert "FAIL worker-1 beendet mit Exit 137" in ausgabe

    (tmp_path / "zwei").mkdir()
    rc, ausgabe, _, _ = _lauf(tmp_path / "zwei", "verify", FAKE_EVENTS="worker-1 0")
    assert rc == 0, ausgabe
    assert "Pruefung bestanden" in ausgabe


def test_workerpruefung_abschaltbar(tmp_path: Path) -> None:
    rc, ausgabe, aufrufe, arbeit = _lauf(
        tmp_path, "apply", "dev-neu", WORKER_CHECK_SECONDS="0", FAKE_EVENTS="worker-1 1", FAKE_STATUS="exited"
    )

    assert rc == 0, ausgabe
    assert _tag(arbeit) == "dev-neu"
    assert not any(z.startswith(("events", "inspect")) for z in aufrufe)


def _index(aufrufe: list[str], teil: str) -> int:
    treffer = [i for i, zeile in enumerate(aufrufe) if teil in zeile]
    assert treffer, f"Aufruf fehlt: {teil}\n" + "\n".join(aufrufe)
    return treffer[0]


def test_worker_starten_nach_der_migration_vor_der_anwendung(tmp_path: Path) -> None:
    """Issue #509: Reihenfolge Migration → Worker → Web."""
    rc, ausgabe, aufrufe, _ = _lauf(tmp_path, "apply", "dev-neu", WORKER_SERVICES="worker ingestor")

    assert rc == 0, ausgabe
    angehalten = _index(aufrufe, "stop worker ingestor")
    migration = _index(aufrufe, "safemigrate")
    worker = _index(aufrufe, "up -d --no-deps worker ingestor")
    anwendung = _index(aufrufe, "up -d --no-deps --wait mandari")
    assert angehalten < migration < worker < anwendung


def test_vorgabe_nimmt_den_worker_nur_wenn_die_compose_datei_ihn_kennt(tmp_path: Path) -> None:
    rc, ausgabe, aufrufe, _ = _lauf(
        tmp_path, "apply", "dev-neu", WORKER_SERVICES="", FAKE_SERVICES="postgres mandari worker ingestor"
    )
    assert rc == 0, ausgabe
    assert any(z.endswith("stop worker ingestor") for z in aufrufe), "\n".join(aufrufe)

    (tmp_path / "alt").mkdir()
    rc, ausgabe, aufrufe, _ = _lauf(tmp_path / "alt", "apply", "dev-neu", WORKER_SERVICES="")
    assert rc == 0, ausgabe
    assert any(z.endswith("stop ingestor") for z in aufrufe), "ältere Compose-Datei ohne Dienst worker"
    assert not any("worker" in z.split() for z in aufrufe)


def test_vorgabe_nimmt_auch_den_worker_fuer_texterkennung(tmp_path: Path) -> None:
    """worker-heavy (Warteschlangen ocr, ai) steht wie worker während der Migration und startet vor der Anwendung."""
    rc, ausgabe, aufrufe, _ = _lauf(
        tmp_path, "apply", "dev-neu", WORKER_SERVICES="", FAKE_SERVICES="postgres mandari worker worker-heavy ingestor"
    )

    assert rc == 0, ausgabe
    angehalten = _index(aufrufe, "stop worker worker-heavy ingestor")
    migration = _index(aufrufe, "safemigrate")
    worker = _index(aufrufe, "up -d --no-deps worker worker-heavy ingestor")
    anwendung = _index(aufrufe, "up -d --no-deps --wait mandari")
    assert angehalten < migration < worker < anwendung


# -- Gesundheitsprüfung per Heartbeat (Issue #574) -------------------------------------------------


def test_worker_mit_healthcheck_muss_healthy_werden(tmp_path: Path) -> None:
    """events_worker erneuert die Heartbeat-Datei nur, solange jede Rolle arbeitet; „unhealthy“ heißt Rückfall."""
    rc, ausgabe, _, arbeit = _lauf(tmp_path, "apply", "dev-neu", WORKER_SERVICES="worker", FAKE_HEALTH="unhealthy")

    assert rc == 1, ausgabe
    assert _tag(arbeit) == "dev-alt"
    assert "FAIL worker: unhealthy (Heartbeat" in ausgabe

    (tmp_path / "gesund").mkdir()
    rc, ausgabe, _, arbeit = _lauf(
        tmp_path / "gesund", "apply", "dev-neu", WORKER_SERVICES="worker", FAKE_HEALTH="healthy"
    )
    assert rc == 0, ausgabe
    assert _tag(arbeit) == "dev-neu"
    assert "OK   worker: healthy (Heartbeat)" in ausgabe


def test_worker_der_nicht_gesund_wird_fuehrt_zum_rueckfall(tmp_path: Path) -> None:
    rc, ausgabe, _, arbeit = _lauf(
        tmp_path, "apply", "dev-neu", WORKER_SERVICES="worker", FAKE_HEALTH="starting", WORKER_HEALTH_SECONDS="0"
    )

    assert rc == 1, ausgabe
    assert _tag(arbeit) == "dev-alt"
    assert "FAIL worker: starting (Heartbeat, nach 0 s)" in ausgabe


def test_container_ohne_healthcheck_zaehlen_nur_mit_ihrem_zustand(tmp_path: Path) -> None:
    rc, ausgabe, _, _ = _lauf(tmp_path, "apply", "dev-neu", FAKE_HEALTH="")

    assert rc == 0, ausgabe
    assert "(Heartbeat" not in ausgabe

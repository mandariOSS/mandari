# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``update.sh``: Der Ingestor steht während aller Migrationen (wie ``deploy/scripts/deploy.sh``), der
Protokoll-Orchestrator wechselt danach auf das neue Image (Issue #479).

Das Skript läuft gegen ein nachgebautes ``docker`` (protokolliert nur die Aufrufe), damit die CI
die Reihenfolge ohne Docker prüft: erst Worker anhalten, dann migrieren, Worker erst nach den
Post-Deploy-Migrationen mit neuem Image starten – und bei Abbruch trotzdem wieder starten.

Außerdem (Issue #698): Fehlt für die Zielversion ein Image, bricht das Skript ab, bevor es etwas verändert.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
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
  manifest)
    # FAKE_FEHLT: Images, die es in der Registry nicht gibt (durch Leerzeichen getrennt)
    case " ${FAKE_FEHLT:-} " in
      *" $3 "*) echo "manifest unknown" >&2; exit 1 ;;
    esac
    ;;
  compose)
    if [ "$2" = config ] && [ "$3" = --services ]; then
      printf '%s\\n' ${FAKE_SERVICES:-postgres redis elasticsearch mandari minutes-orchestrator website ingestor caddy}
      if [ -n "${FAKE_WEITERE_DIENSTE:-}" ]; then seq -f 'dienst-%g' 1 "$FAKE_WEITERE_DIENSTE"; fi
    fi
    if [ "$2" = config ] && [ "$3" = --images ]; then
      # Wie Compose: Infrastruktur-Images, Anwendungs-Images mit IMAGE_TAG (mandari zweimal: Orchestrator)
      printf '%s\\n' postgres:16-alpine "ghcr.io/mandarioss/mandari:${IMAGE_TAG:-latest}" \\
        "ghcr.io/mandarioss/website:${IMAGE_TAG:-latest}" "ghcr.io/mandarioss/ingestor:${IMAGE_TAG:-latest}" \\
        "ghcr.io/mandarioss/mandari:${IMAGE_TAG:-latest}"
    fi
    if [ "$2" = pull ] && [ -n "${FAKE_PULL_SCHEITERT:-}" ]; then exit 1; fi
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


@dataclass
class Lauf:
    rc: int
    aufrufe: list[str]
    ausgabe: str
    env: str


def _lauf(
    tmp_path: Path, *, env_zusatz: dict[str, str] | None = None, args: tuple[str, ...] = ()
) -> tuple[int, list[str]]:
    lauf = _lauf_voll(tmp_path, env_zusatz=env_zusatz, args=args)
    return lauf.rc, lauf.aufrufe


def _lauf_voll(tmp_path: Path, *, env_zusatz: dict[str, str] | None = None, args: tuple[str, ...] = ()) -> Lauf:
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

    env = {k: v for k, v in os.environ.items() if k not in {"WORKER_SERVICES", "COMPOSE_PROJECT_NAME", "IMAGE_TAG"}}
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
    return Lauf(
        rc=ergebnis.returncode,
        aufrufe=log.read_text(encoding="utf-8").splitlines(),
        ausgabe=ergebnis.stdout + ergebnis.stderr,
        env=(arbeit / ".env").read_text(encoding="utf-8"),
    )


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


def test_worker_werden_auch_bei_langer_dienstliste_erkannt(tmp_path: Path) -> None:
    """
    Die Dienstliste ist größer als der Pipe-Puffer, die Worker stehen vorn: Endete die Suche beim ersten
    Treffer (``grep -q``), bekäme der Schreiber „Broken pipe“ und unter ``pipefail`` gälte der Dienst als
    nicht definiert – er liefe während der Migrationen weiter (Issue #695). Bei kurzer Liste hing das vom
    Zeitverhalten ab; so tritt es unter Linux verlässlich auf.
    """
    rc, aufrufe = _lauf(tmp_path, env_zusatz={"FAKE_WEITERE_DIENSTE": "20000"})

    assert rc == 0, "\n".join(aufrufe[:40])
    assert _index(aufrufe, "compose stop ingestor minutes-orchestrator") < _index(aufrufe, "manage.py safemigrate")
    assert _index(aufrufe, "exec mandari python manage.py migrate --noinput") < _index(
        aufrufe, "compose up -d --no-deps ingestor minutes-orchestrator"
    )


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


def test_images_der_zielversion_werden_vor_dem_herunterladen_geprueft(tmp_path: Path) -> None:
    rc, aufrufe = _lauf(tmp_path)

    assert rc == 0, "\n".join(aufrufe)
    for image in ("mandari", "ingestor", "website"):
        assert _index(aufrufe, f"manifest inspect ghcr.io/mandarioss/{image}:v1.1.0") < _index(aufrufe, "compose pull")
    assert not any("postgres:16-alpine" in z for z in aufrufe), "nur Anwendungs-Images mit dem Ziel-Tag"


def test_fehlendes_image_bricht_ab_bevor_etwas_veraendert_wird(tmp_path: Path) -> None:
    """Früher stand die neue Version schon in der .env, wenn ``docker compose pull`` am fehlenden Image scheiterte."""
    lauf = _lauf_voll(tmp_path, env_zusatz={"FAKE_FEHLT": "ghcr.io/mandarioss/website:v1.1.0"})

    assert lauf.rc != 0
    assert "IMAGE_TAG=v1.0.0" in lauf.env, "die .env bleibt unverändert"
    assert "website:v1.1.0" in lauf.ausgabe and "fehlt" in lauf.ausgabe, lauf.ausgabe
    assert "v0.11.0" in lauf.ausgabe and "/releases" in lauf.ausgabe, "die Meldung nennt gültige Werte"
    assert "nichts verändert" in lauf.ausgabe, lauf.ausgabe
    assert not any(teil in z for z in lauf.aufrufe for teil in ("compose pull", "compose stop", "safemigrate"))


def test_dry_run_meldet_fehlendes_image(tmp_path: Path) -> None:
    lauf = _lauf_voll(
        tmp_path, env_zusatz={"FAKE_FEHLT": "ghcr.io/mandarioss/website:v1.1.0"}, args=("--dry-run", "--tag", "v1.1.0")
    )

    assert lauf.rc != 0
    assert "website:v1.1.0" in lauf.ausgabe and "fehlt" in lauf.ausgabe, lauf.ausgabe
    assert "IMAGE_TAG=v1.0.0" in lauf.env


def test_scheitert_das_herunterladen_kommt_die_alte_env_zurueck(tmp_path: Path) -> None:
    lauf = _lauf_voll(tmp_path, env_zusatz={"FAKE_PULL_SCHEITERT": "1"})

    assert lauf.rc != 0
    assert "IMAGE_TAG=v1.0.0" in lauf.env, lauf.env
    assert "nichts umgeschaltet" in lauf.ausgabe, lauf.ausgabe
    assert not any(teil in z for z in lauf.aufrufe for teil in ("compose stop", "safemigrate"))


MIT_WORKER = "postgres redis elasticsearch mandari minutes-orchestrator website ingestor caddy worker"


def test_worker_steht_waehrend_der_migrationen_und_startet_vor_der_anwendung(tmp_path: Path) -> None:
    """Issue #509: Migration → Worker → Web; der Worker steht wie die übrigen während der Migrationen."""
    rc, aufrufe = _lauf(tmp_path, env_zusatz={"FAKE_SERVICES": MIT_WORKER})

    assert rc == 0, "\n".join(aufrufe)
    angehalten = _index(aufrufe, "compose stop ingestor minutes-orchestrator worker")
    migration = _index(aufrufe, "manage.py safemigrate")
    worker = _index(aufrufe, "compose up -d --no-deps worker")
    anwendung = _index(aufrufe, "compose up -d --no-deps mandari")
    assert angehalten < migration < worker < anwendung
    assert any("inspect" in z and "mandari-worker" in z for z in aufrufe), "Verifikation prüft den Worker"


def test_ohne_worker_dienst_kein_vorstart(tmp_path: Path) -> None:
    rc, aufrufe = _lauf(tmp_path)

    assert rc == 0, "\n".join(aufrufe)
    assert not any("worker" in z.split() for z in aufrufe if "compose" in z)
    assert not any("mandari-worker" in z for z in aufrufe)


def test_abbruch_setzt_den_vorgestarteten_worker_mit_zurueck(tmp_path: Path) -> None:
    """Scheitert das Umschalten, startet der trap den Worker mit der zurückgesetzten .env neu."""
    rc, aufrufe = _lauf(tmp_path, env_zusatz={"FAKE_SERVICES": MIT_WORKER, "FAKE_UNHEALTHY": "1"})

    assert rc != 0
    vorstart = _index(aufrufe, "compose up -d --no-deps worker")
    neustarts = [i for i, z in enumerate(aufrufe) if "compose up -d --no-deps" in z and z.endswith(" worker")]
    assert neustarts[-1] > vorstart, "\n".join(aufrufe)
    assert "ingestor minutes-orchestrator worker" in aufrufe[neustarts[-1]]


def test_rueckfall_setzt_auch_den_worker_zurueck(tmp_path: Path) -> None:
    rc, aufrufe = _lauf(tmp_path, args=("--rollback",), env_zusatz={"FAKE_SERVICES": MIT_WORKER})

    assert rc == 0, "\n".join(aufrufe)
    assert any("compose up -d --no-deps ingestor minutes-orchestrator worker" in z for z in aufrufe)

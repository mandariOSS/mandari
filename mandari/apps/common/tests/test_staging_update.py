# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``deploy/scripts/staging_update.sh`` (Issue #737): Staging zieht den neuesten grünen dev-Stand nach.

Das Skript fragt die GitHub-API nach dem neuesten Commit mit erfolgreichem Release-Lauf und erfolgreicher
CI (in der Merge-Queue oder direkt auf dem Zweig; seit Issue #935 ohne Lauf nach dem Push), vergleicht mit dem laufenden ``IMAGE_TAG`` und ruft dann
``deploy.sh plan`` und ``deploy.sh apply`` auf. Hier läuft es gegen ein nachgebautes ``curl`` (liefert
vorgegebene API-Antworten) und ein nachgebautes ``deploy.sh`` (protokolliert die Aufrufe und schaltet das
Tag in der ``.env`` um), damit die CI den Ablauf ohne Netz und ohne Docker prüft.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]
SKRIPT = REPO / "deploy" / "scripts" / "staging_update.sh"
SYSTEMD = REPO / "deploy" / "systemd"

NEU = "3b1f9c2" + "d" * 33
MITTE = "2c3d4e5" + "e" * 33
ALT = "1a2b3c4" + "f" * 33

FAKE_CURL = """#!/usr/bin/env bash
ziel=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) ziel="$2"; shift 2 ;;
    -H|-m|--retry|-K) shift 2 ;;
    http*) url="$1"; shift ;;
    *) shift ;;
  esac
done
echo "$url" >> "$CURL_LOG"
if [ -n "${FAKE_STDIN_LOG:-}" ]; then cat >> "$FAKE_STDIN_LOG"; fi
case "$url" in
  *"/actions/workflows/release.yml/runs"*) datei=release ;;
  *"/actions/workflows/pr-check.yml/runs"*"event=merge_group"*) datei=ci_queue ;;
  *"/actions/workflows/pr-check.yml/runs"*) datei=ci_zweig ;;
  *"/compare/"*) datei=compare ;;
  *) exit 22 ;;
esac
[ -f "$FAKE_API_DIR/$datei.json" ] || exit 22
cp "$FAKE_API_DIR/$datei.json" "$ziel"
"""

FAKE_DEPLOY = """#!/bin/sh
echo "$1 ${2:-} dir=$MANDARI_DIR app=${APP_SERVICE:-}" >> "$DEPLOY_AUFRUFE"
case "$1" in
  plan)
    echo "plan-ausgabe fuer $2"
    exit "${FAKE_PLAN_EXIT:-0}"
    ;;
  apply)
    echo "ERGEBNIS: alle Pruefungen bestanden"
    if [ "${FAKE_APPLY_EXIT:-0}" = 0 ]; then
      sed "s/^IMAGE_TAG=.*/IMAGE_TAG=$2/" "$MANDARI_DIR/.env" > "$MANDARI_DIR/.env.neu"
      mv "$MANDARI_DIR/.env.neu" "$MANDARI_DIR/.env"
    fi
    exit "${FAKE_APPLY_EXIT:-0}"
    ;;
esac
"""


def _sh() -> str | None:
    if os.name == "nt":
        # Git Bash; das bash.exe aus System32 wäre WSL
        kandidat = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git" / "usr" / "bin" / "sh.exe"
        return str(kandidat) if kandidat.exists() else None
    # Auf dem Server läuft das Skript mit sh (dash), also auch hier
    return shutil.which("sh")


SH = _sh()
pytestmark = pytest.mark.skipif(
    SH is None or shutil.which("jq") is None, reason="sh oder jq nicht vorhanden (in der CI beides da)"
)


def _laeufe(
    *shas: str, zweig: str = "dev", ergebnis: str = "success", ereignis: str = "workflow_dispatch"
) -> dict[str, Any]:
    return {
        "total_count": len(shas),
        "workflow_runs": [
            {"head_sha": s, "head_branch": zweig, "conclusion": ergebnis, "event": ereignis} for s in shas
        ],
    }


def _queue(*shas: str, zweig: str = "dev") -> dict[str, Any]:
    return {
        "total_count": len(shas),
        "workflow_runs": [
            {"head_sha": s, "head_branch": f"gh-readonly-queue/{zweig}/pr-{i}-{s}", "conclusion": "success"}
            for i, s in enumerate(shas, start=700)
        ],
    }


@dataclass
class Lauf:
    rc: int
    ausgabe: str
    urls: list[str]
    deploys: list[str]
    staging: Path

    @property
    def tag(self) -> str:
        zeilen = (self.staging / ".env").read_text(encoding="utf-8").splitlines()
        return next(z for z in zeilen if z.startswith("IMAGE_TAG=")).removeprefix("IMAGE_TAG=")

    @property
    def zustand(self) -> Path:
        return self.staging / "staging-update"


class Umgebung:
    """Ein Staging-Verzeichnis mit Fake-API; mehrere Läufe hintereinander teilen den Zustand."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        tag: str = f"dev-{ALT[:7]}",
        staging_zeile: str | None = "MANDARI_UMGEBUNG=staging",
        deploy_env: str | None = "APP_SERVICE=mandari-stage\nMANDARI_DIR=/woanders\n",
    ) -> None:
        self.tmp = tmp_path
        self.staging = tmp_path / "staging"
        self.staging.mkdir()
        zeilen = [f"IMAGE_TAG={tag}", "IMAGE_REGISTRY=ghcr.io/mandarioss"]
        if staging_zeile is not None:
            zeilen.append(staging_zeile)
        (self.staging / ".env").write_text("\n".join(zeilen) + "\n", encoding="utf-8", newline="\n")
        if deploy_env is not None:
            (self.staging / "deploy.env").write_text(deploy_env, encoding="utf-8", newline="\n")

        skripte = tmp_path / "skripte"
        skripte.mkdir()
        self.skript = skripte / "staging_update.sh"
        # LF erzwingen (Checkout unter Windows kann CRLF liefern)
        self.skript.write_bytes(SKRIPT.read_bytes().replace(b"\r\n", b"\n"))
        (skripte / "deploy.sh").write_bytes(FAKE_DEPLOY.encode())

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_bytes(FAKE_CURL.encode())
        curl.chmod(0o755)
        self.api = tmp_path / "api"
        self.api.mkdir()
        self.curl_log = tmp_path / "curl.log"
        self.deploy_log = tmp_path / "deploy.log"

    def antworten(
        self,
        *,
        release: dict[str, Any] | None = None,
        ci_zweig: dict[str, Any] | None = None,
        ci_queue: dict[str, Any] | None = None,
        vergleich: str | None = "ahead",
    ) -> None:
        for name, daten in (
            ("release", release if release is not None else _laeufe()),
            ("ci_zweig", ci_zweig if ci_zweig is not None else _laeufe()),
            ("ci_queue", ci_queue if ci_queue is not None else _queue()),
            ("compare", {"status": vergleich} if vergleich is not None else None),
        ):
            datei = self.api / f"{name}.json"
            if daten is None:
                datei.unlink(missing_ok=True)
            else:
                datei.write_text(json.dumps(daten), encoding="utf-8", newline="\n")

    def lauf(self, *args: str, **env_extra: str) -> Lauf:
        for datei in (self.curl_log, self.deploy_log):
            datei.write_text("", encoding="utf-8")
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("FAKE_", "GITHUB_", "DEPLOY_", "STATE_", "MANDARI_", "APP_", "CI_", "RELEASE_"))
        }
        env.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{env['PATH']}",
                "MANDARI_DIR": str(self.staging),
                "CURL_LOG": str(self.curl_log),
                "DEPLOY_AUFRUFE": str(self.deploy_log),
                "FAKE_API_DIR": str(self.api),
            }
        )
        env.update(env_extra)
        assert SH is not None
        ergebnis = subprocess.run(  # noqa: S603 — fester Aufruf im Test
            [SH, str(self.skript), *args],
            cwd=self.tmp,
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
            ausgabe=ergebnis.stdout + ergebnis.stderr,
            urls=self.curl_log.read_text(encoding="utf-8").splitlines(),
            deploys=self.deploy_log.read_text(encoding="utf-8").splitlines(),
            staging=self.staging,
        )


def test_neuer_gruener_stand_wird_mit_plan_und_apply_deployt(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU, ALT), ci_zweig=_laeufe(NEU, ALT))

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    ziel = f"dev-{NEU[:7]}"
    assert [z.split(" dir=")[0] for z in lauf.deploys] == [f"plan {ziel}", f"apply {ziel}"]
    assert lauf.tag == ziel
    # Prüfprotokoll mit Commit, Ausgabe beider Schritte und Ergebnis
    protokolle = list((lauf.zustand / "protokolle").glob(f"*-{ziel}.log"))
    assert len(protokolle) == 1
    text = protokolle[0].read_text(encoding="utf-8")
    assert f"commit/{NEU}" in text
    assert f"plan-ausgabe fuer {ziel}" in text and "ERGEBNIS: alle Pruefungen bestanden" in text
    assert "# Ergebnis: ok" in text
    status = (lauf.zustand / "status").read_text(encoding="utf-8").split("\t")
    assert status[1:4] == [f"dev-{ALT[:7]}", ziel, "ok"]
    assert "Staging laeuft auf" in (lauf.zustand / "staging-update.log").read_text(encoding="utf-8")


def test_zweiter_lauf_ohne_neuen_stand_tut_nichts(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU, ALT), ci_zweig=_laeufe(NEU, ALT))
    assert u.lauf().rc == 0

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.deploys == []
    assert "neueste gepruefte Stand" in lauf.ausgabe
    # Ohne Änderung keine zusätzliche Abfrage (Vergleich) – drei Abfragen je Lauf
    assert len(lauf.urls) == 3


def test_release_ohne_gruene_ci_zaehlt_nicht(tmp_path: Path) -> None:
    """Das neueste Commit hat Images, aber die CI läuft noch (oder ist rot): das ältere grüne gilt."""
    u = Umgebung(tmp_path, tag=f"dev-{ALT[:7]}")
    u.antworten(release=_laeufe(NEU, MITTE, ALT), ci_zweig=_laeufe(MITTE, ALT))

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.tag == f"dev-{MITTE[:7]}"


def test_gruene_ci_ohne_release_zaehlt_nicht(tmp_path: Path) -> None:
    u = Umgebung(tmp_path, tag=f"dev-{ALT[:7]}")
    u.antworten(release=_laeufe(ALT), ci_zweig=_laeufe(NEU, ALT))

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.deploys == []


def test_ci_aus_der_merge_queue_zaehlt_nur_fuer_den_eigenen_zweig(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    # NEU wurde in der Queue von main geprüft, MITTE in der Queue von dev
    queue = _queue(MITTE)
    queue["workflow_runs"] = [*_queue(NEU, zweig="main")["workflow_runs"], *queue["workflow_runs"]]
    u.antworten(release=_laeufe(NEU, MITTE, ALT), ci_zweig=_laeufe(ALT), ci_queue=queue)

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.tag == f"dev-{MITTE[:7]}"
    assert any("event=merge_group" in url for url in lauf.urls)


def test_merge_queue_allein_genuegt_ohne_lauf_nach_dem_push(tmp_path: Path) -> None:
    """Issue #935: Ein Push auf dev startet keine CI mehr; die Queue hat genau dieses Commit geprüft."""
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU, ALT, ereignis="push"), ci_zweig=_laeufe(), ci_queue=_queue(NEU))

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.tag == f"dev-{NEU[:7]}"
    # Läufe auf dem Zweig werden ohne Ereignisfilter abgefragt (von Hand, Nachtlauf, ältere push-Läufe)
    zweig = [url for url in lauf.urls if "pr-check.yml/runs" in url and "merge_group" not in url]
    assert len(zweig) == 1 and "event=" not in zweig[0], zweig


def test_lauf_auf_dem_zweig_zaehlt_ein_pull_request_davon_nicht(tmp_path: Path) -> None:
    # Ein Pull Request dev -> main prüft die Zusammenführung mit main, nicht das Commit auf dev
    u = Umgebung(tmp_path)
    zweig = _laeufe(NEU, ereignis="pull_request")
    zweig["workflow_runs"] += _laeufe(MITTE, ereignis="workflow_dispatch")["workflow_runs"]
    u.antworten(release=_laeufe(NEU, MITTE, ALT), ci_zweig=zweig)

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.tag == f"dev-{MITTE[:7]}"


def test_laeufe_anderer_zweige_und_ungueltige_kennungen_zaehlen_nicht(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    release = _laeufe("nicht-hex;rm -rf /", NEU[:12], zweig="dev")
    release["workflow_runs"] = [
        *release["workflow_runs"],
        *_laeufe(NEU, zweig="feature")["workflow_runs"],
        *_laeufe(ALT, ergebnis="failure")["workflow_runs"],
    ]
    u.antworten(release=release, ci_zweig=_laeufe(NEU, ALT, "nicht-hex;rm -rf /", NEU[:12]))

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.deploys == []
    assert "Kein Stand" in lauf.ausgabe


def test_ohne_kennzeichnung_als_staging_bricht_es_ab(tmp_path: Path) -> None:
    """Schutz vor Fehlkonfiguration: Ein Verzeichnis ohne MANDARI_UMGEBUNG=staging wird nie angefasst."""
    u = Umgebung(tmp_path, staging_zeile=None)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))

    lauf = u.lauf()

    assert lauf.rc == 1
    assert "weist sich nicht als Staging aus" in lauf.ausgabe
    assert lauf.deploys == [] and lauf.urls == []
    assert lauf.tag == f"dev-{ALT[:7]}"

    (tmp_path / "prod").mkdir()
    u = Umgebung(tmp_path / "prod", staging_zeile="MANDARI_UMGEBUNG=produktion")
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))
    assert u.lauf().rc == 1


def test_ohne_mandari_dir_bricht_es_ab(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))

    lauf = u.lauf(MANDARI_DIR="")

    assert lauf.rc == 1
    assert "MANDARI_DIR fehlt" in lauf.ausgabe
    assert lauf.deploys == []


def test_deploy_env_wird_geladen_das_verzeichnis_bleibt_das_gepruefte(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    for aufruf in lauf.deploys:
        assert "app=mandari-stage" in aufruf, aufruf
        assert f"dir={u.staging}" in aufruf, aufruf
        assert "/woanders" not in aufruf


def test_gescheiterter_apply_wird_nicht_wiederholt(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))

    lauf = u.lauf(FAKE_APPLY_EXIT="1")

    assert lauf.rc == 1
    assert lauf.tag == f"dev-{ALT[:7]}"
    assert (lauf.zustand / "fehlgeschlagen").read_text(encoding="utf-8").split() == [f"dev-{NEU[:7]}"]
    assert "# Ergebnis: gescheitert" in next((lauf.zustand / "protokolle").glob("*.log")).read_text(encoding="utf-8")

    nochmal = u.lauf()
    assert nochmal.rc == 0, nochmal.ausgabe
    assert nochmal.deploys == []
    assert "schon einmal gescheitert" in nochmal.ausgabe

    # Ein neuerer Stand wird wieder versucht
    u.antworten(release=_laeufe("4" * 40, NEU), ci_zweig=_laeufe("4" * 40, NEU))
    weiter = u.lauf()
    assert weiter.rc == 0, weiter.ausgabe
    assert weiter.tag == "dev-4444444"


def test_gescheiterte_vorbereitung_hoechstens_dreimal(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))

    for versuch in range(3):
        lauf = u.lauf(FAKE_PLAN_EXIT="1")
        assert lauf.rc == 1, lauf.ausgabe
        assert [z.split()[0] for z in lauf.deploys] == ["plan"], "kein apply nach gescheitertem plan"
        assert f"Versuch {versuch + 1} von 3" in lauf.ausgabe

    lauf = u.lauf(FAKE_PLAN_EXIT="1")
    assert lauf.rc == 0
    assert lauf.deploys == []
    assert lauf.tag == f"dev-{ALT[:7]}"


def test_neuerer_laufender_stand_wird_nicht_ueberschrieben(tmp_path: Path) -> None:
    u = Umgebung(tmp_path, tag=f"dev-{NEU[:7]}")
    u.antworten(release=_laeufe(MITTE), ci_zweig=_laeufe(MITTE), vergleich="behind")

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.deploys == []
    assert "ist neuer als" in lauf.ausgabe
    assert any(f"/compare/{NEU[:7]}...{MITTE}" in url for url in lauf.urls)


def test_ohne_vergleich_gilt_der_gruene_stand(tmp_path: Path) -> None:
    """Laufendes Tag ohne Commit-Bezug (z. B. eine Version) oder Vergleich nicht möglich: der grüne Stand gilt."""
    u = Umgebung(tmp_path, tag="v0.11.0")
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU), vergleich=None)

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert lauf.tag == f"dev-{NEU[:7]}"
    assert not any("/compare/" in url for url in lauf.urls)


def test_pause_und_pruefmodus_aendern_nichts(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))

    pruefen = u.lauf("pruefen")
    assert pruefen.rc == 0, pruefen.ausgabe
    assert pruefen.deploys == []
    assert f"wuerde dev-{ALT[:7]} -> dev-{NEU[:7]} deployen" in pruefen.ausgabe

    (u.staging / "staging-update" / "pause").touch()
    pausiert = u.lauf()
    assert pausiert.rc == 0, pausiert.ausgabe
    assert pausiert.deploys == []
    assert "Pausiert" in pausiert.ausgabe
    assert pausiert.tag == f"dev-{ALT[:7]}"


def test_api_nicht_erreichbar_ist_ein_fehler_ohne_deploy(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))
    (u.api / "ci_queue.json").unlink()

    lauf = u.lauf()

    assert lauf.rc == 1
    assert "GitHub-API nicht erreichbar" in lauf.ausgabe
    assert lauf.deploys == []

    (u.api / "ci_queue.json").write_text("<html>Fehlerseite</html>", encoding="utf-8")
    lauf = u.lauf()
    assert lauf.rc == 1
    assert "nicht lesbar" in lauf.ausgabe
    assert lauf.deploys == []


def test_aktive_sperre_verhindert_einen_zweiten_lauf(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))
    sperre = u.staging / "staging-update" / "lauf.sperre"
    sperre.mkdir(parents=True)
    pid_datei = tmp_path / "pid"
    assert SH is not None
    # Die PID muss aus derselben Shell-Umgebung stammen, die das Skript mit kill -0 prüft
    halter = subprocess.Popen([SH, "-c", 'echo $$ > "$1"; exec sleep 60', "sh", str(pid_datei)])  # noqa: S603
    try:
        for _ in range(100):
            if pid_datei.exists() and pid_datei.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.1)
        (sperre / "pid").write_text(pid_datei.read_text(encoding="utf-8"), encoding="utf-8")

        lauf = u.lauf()

        assert lauf.rc == 0, lauf.ausgabe
        assert "anderer Lauf ist aktiv" in lauf.ausgabe
        assert lauf.deploys == [] and lauf.urls == []
        assert sperre.exists(), "fremde Sperre bleibt stehen"
    finally:
        halter.kill()
        halter.wait()


def test_verwaiste_sperre_wird_uebernommen_und_freigegeben(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))
    sperre = u.staging / "staging-update" / "lauf.sperre"
    sperre.mkdir(parents=True)
    (sperre / "pid").write_text("4999999\n", encoding="utf-8")

    lauf = u.lauf()

    assert lauf.rc == 0, lauf.ausgabe
    assert "Verwaiste Sperre" in lauf.ausgabe
    assert lauf.tag == f"dev-{NEU[:7]}"
    assert not sperre.exists(), "Sperre nach dem Lauf freigegeben"


def test_token_geht_nicht_ueber_die_befehlszeile(tmp_path: Path) -> None:
    u = Umgebung(tmp_path)
    u.antworten(release=_laeufe(NEU), ci_zweig=_laeufe(NEU))
    stdin_log = tmp_path / "stdin.log"

    lauf = u.lauf("pruefen", GITHUB_TOKEN="geheim-123", FAKE_STDIN_LOG=str(stdin_log))

    assert lauf.rc == 0, lauf.ausgabe
    assert "Authorization: Bearer geheim-123" in stdin_log.read_text(encoding="utf-8")
    assert "geheim-123" not in "".join(lauf.urls) + lauf.ausgabe


# --- systemd-Vorlagen ------------------------------------------------------------------------


class _Unit(configparser.ConfigParser):
    """systemd-Unit: Schlüssel mit Groß- und Kleinschreibung, ``${…}`` ohne Interpolation."""

    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _unit(name: str) -> configparser.ConfigParser:
    parser = _Unit(strict=False, interpolation=None)
    parser.read_string((SYSTEMD / name).read_text(encoding="utf-8"))
    return parser


def _sekunden(wert: str) -> int:
    treffer = re.fullmatch(r"(\d+)(s|min|h)?", wert.strip())
    assert treffer, wert
    return int(treffer.group(1)) * {"s": 1, "min": 60, "h": 3600, None: 1}[treffer.group(2)]


def test_systemd_dienst_ruft_das_skript_einmalig_auf() -> None:
    dienst = _unit("mandari-staging-update.service")["Service"]
    assert dienst["Type"] == "oneshot", "oneshot: der Timer startet keinen zweiten Lauf, solange einer läuft"
    assert dienst["EnvironmentFile"].startswith("/etc/")
    assert dienst["ExecStart"] == "/bin/sh ${UPDATE_SKRIPT}"
    vorlage = (SYSTEMD / "staging-update.env.example").read_text(encoding="utf-8")
    for variable in ("MANDARI_DIR=", "UPDATE_SKRIPT=", "DEPLOY_ENV="):
        assert re.search(rf"^{variable}", vorlage, re.M), variable
    assert re.search(r"^UPDATE_SKRIPT=.*/staging_update\.sh$", vorlage, re.M)


def test_systemd_timer_haelt_die_zusage_von_30_minuten() -> None:
    timer = _unit("mandari-staging-update.timer")
    assert timer["Timer"]["Unit"] == "mandari-staging-update.service"
    abstand = _sekunden(timer["Timer"]["OnUnitInactiveSec"]) + _sekunden(timer["Timer"]["RandomizedDelaySec"])
    timeout = _sekunden(_unit("mandari-staging-update.service")["Service"]["TimeoutStartSec"])
    # Höchstens zehn Minuten bis zur nächsten Abfrage; der Rest der 30 Minuten bleibt für den Deploy
    assert abstand <= 10 * 60
    assert timeout <= 45 * 60
    assert timer["Install"]["WantedBy"] == "timers.target"

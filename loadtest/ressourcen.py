# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ressourcenverbrauch während eines Lasttests mitschreiben (Issue #228; Linux, z. B. in der CI).

Schreibt alle ``--intervall`` Sekunden je Komponente und Messgröße eine Zeile als CSV
(``zeit_s,komponente,messgroesse,wert``), bis das Programm SIGTERM oder SIGINT erhält.
``loadtest/auswerten.py`` fasst die Reihe zu Mittel und Maximum zusammen.

- ``--prozess name=muster``: alle Prozesse, deren Befehlszeile ``muster`` enthält (über ``/proc``),
  summiert – CPU in Prozent eines Kerns und Speicher (RSS) in MB. So zählen alle Daphne-Prozesse
  zusammen als „Anwendung“.
- ``--container name=container``: ``docker stats`` eines Containers – CPU in Prozent eines Kerns und
  Speicher in MB.
- ``--postgres container:benutzer:datenbank``: offene Verbindungen zur Datenbank (``pg_stat_activity``).

    python loadtest/ressourcen.py --ausgabe loadtest/results/klein_ressourcen.csv \\
        --prozess anwendung=daphne --prozess lastgeber=locust \\
        --container postgres=lasttest-postgres --postgres lasttest-postgres:lasttest:lasttest

Nur Standardbibliothek. Die Umrechnung der ``docker stats``-Angaben ist eine reine Funktion und wird in
``mandari/apps/common/tests/test_lasttest_auswertung.py`` geprüft.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

#: Einheiten von ``docker stats`` (MemUsage) in MB
_EINHEITEN = {
    "b": 1 / 1_000_000,
    "kb": 1 / 1000,
    "kib": 1024 / 1_000_000,
    "mb": 1.0,
    "mib": 1024 * 1024 / 1_000_000,
    "gb": 1000.0,
    "gib": 1024**3 / 1_000_000,
}


def speicher_mb(angabe: str) -> float:
    """``"123.4MiB / 15.6GiB"`` oder ``"1.2GiB"`` aus ``docker stats`` in MB (nur der belegte Teil)."""
    treffer = re.match(r"\s*([0-9.]+)\s*([a-zA-Z]+)", angabe)
    if not treffer:
        return 0.0
    return float(treffer.group(1)) * _EINHEITEN.get(treffer.group(2).lower(), 0.0)


def cpu_prozent(angabe: str) -> float:
    """``"12.34%"`` aus ``docker stats``."""
    try:
        return float(angabe.strip().rstrip("%"))
    except ValueError:
        return 0.0


# =============================================================================
# Prozesse über /proc
# =============================================================================

_TAKT = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_SEITE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _prozesse(muster: str) -> list[int]:
    eigene = os.getpid()
    gefunden: list[int] = []
    for eintrag in Path("/proc").iterdir():
        if not eintrag.name.isdigit() or int(eintrag.name) == eigene:
            continue
        try:
            befehl = (eintrag / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if muster in befehl:
            gefunden.append(int(eintrag.name))
    return gefunden


def _cpu_ticks(pid: int) -> int | None:
    try:
        felder = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        # Nach dem Befehlsnamen: Feld 12 und 13 (utime, stime) in Takten
        return int(felder[11]) + int(felder[12])
    except (OSError, IndexError, ValueError):
        return None


def _rss_mb(pid: int) -> float:
    try:
        return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * _SEITE / 1_000_000
    except (OSError, IndexError, ValueError):
        return 0.0


class Prozessgruppe:
    """CPU (Delta der Takte zwischen zwei Messungen) und RSS aller Prozesse mit einem Muster."""

    def __init__(self, name: str, muster: str) -> None:
        self.name = name
        self.muster = muster
        self.vorher: dict[int, int] = {}

    def messen(self, intervall: float) -> list[tuple[str, str, float]]:
        cpu = 0.0
        speicher = 0.0
        jetzt: dict[int, int] = {}
        for pid in _prozesse(self.muster):
            ticks = _cpu_ticks(pid)
            if ticks is None:
                continue
            jetzt[pid] = ticks
            if pid in self.vorher:
                cpu += (ticks - self.vorher[pid]) / _TAKT / intervall * 100
            speicher += _rss_mb(pid)
        self.vorher = jetzt
        return [(self.name, "cpu_prozent", round(cpu, 1)), (self.name, "speicher_mb", round(speicher, 1))]


# =============================================================================
# Container
# =============================================================================


def _container(zuordnung: dict[str, str]) -> list[tuple[str, str, float]]:
    if not zuordnung:
        return []
    try:
        ausgabe = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}",
                *zuordnung.values(),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    namen = {container: name for name, container in zuordnung.items()}
    werte: list[tuple[str, str, float]] = []
    for zeile in ausgabe.splitlines():
        teile = zeile.split("\t")
        if len(teile) != 3 or teile[0] not in namen:
            continue
        name = namen[teile[0]]
        werte += [(name, "cpu_prozent", cpu_prozent(teile[1])), (name, "speicher_mb", round(speicher_mb(teile[2]), 1))]
    return werte


def _verbindungen(angabe: str) -> list[tuple[str, str, float]]:
    container, benutzer, datenbank = angabe.split(":", 2)
    abfrage = "select count(*) from pg_stat_activity where datname = current_database()"
    try:
        ausgabe = subprocess.run(
            ["docker", "exec", container, "psql", "-U", benutzer, "-d", datenbank, "-tAc", abfrage],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout.strip()
        return [("postgres", "verbindungen", float(ausgabe))]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _paare(angaben: list[str]) -> dict[str, str]:
    paare: dict[str, str] = {}
    for angabe in angaben:
        name, _, wert = angabe.partition("=")
        if not wert:
            raise SystemExit(f"Erwartet name=wert, erhalten: {angabe}")
        paare[name] = wert
    return paare


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ausgabe", type=Path, required=True)
    parser.add_argument("--intervall", type=float, default=5.0)
    parser.add_argument("--prozess", action="append", default=[])
    parser.add_argument("--container", action="append", default=[])
    parser.add_argument("--postgres", default="")
    args = parser.parse_args(argv)

    gruppen = [Prozessgruppe(name, muster) for name, muster in _paare(args.prozess).items()]
    container = _paare(args.container)
    laeuft = [True]

    def beenden(*_: Any) -> None:
        laeuft[0] = False

    signal.signal(signal.SIGTERM, beenden)
    signal.signal(signal.SIGINT, beenden)

    beginn = time.monotonic()
    args.ausgabe.parent.mkdir(parents=True, exist_ok=True)
    with args.ausgabe.open("w", encoding="utf-8", newline="") as datei:
        schreiber = csv.writer(datei)
        schreiber.writerow(["zeit_s", "komponente", "messgroesse", "wert"])
        for gruppe in gruppen:
            gruppe.messen(args.intervall)  # Ausgangsstand der Takte
        while laeuft[0]:
            time.sleep(args.intervall)
            zeit = round(time.monotonic() - beginn, 1)
            werte = [w for gruppe in gruppen for w in gruppe.messen(args.intervall)]
            werte += _container(container)
            if args.postgres:
                werte += _verbindungen(args.postgres)
            for komponente, messgroesse, wert in werte:
                schreiber.writerow([zeit, komponente, messgroesse, wert])
            datei.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

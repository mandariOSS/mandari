# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für doppelten Code (Issue #1113): Kopierte Blöcke dürfen je Sprache nur weniger werden.

jscpd vergleicht Python, Vorlagen und TypeScript Token für Token und meldet Blöcke, die an zwei Stellen
gleich sind (Klone). Gezählt werden je Format die Klone und die doppelten Zeilen. Was gelesen wird, ab
welcher Größe ein Block zählt und was ausgenommen ist (Tests, Migrationen, Fremdcode), steht mit
Begründung in ``scripts/duplicate_code_ratchet.toml``.

Die Baseline ``scripts/duplicate_code_baseline.json`` hält je Format den erlaubten Höchststand fest. Das
Skript schlägt fehl, wenn ein Wert steigt. Sinkt einer, zieht ``--update`` die Baseline nach (im selben
Pull Request committen); ``--update`` senkt nur. Eine höhere Grenze geht nur über eine Änderung der
Baseline von Hand, die im Review sichtbar ist.

Aufruf aus dem Repo-Root (jscpd in der Version aus ``mandari/pyproject.toml`` [dev]):

    python scripts/check_duplicate_code_ratchet.py [--update] [--verbose] [--json] [--report DATEI]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "scripts" / "duplicate_code_ratchet.toml"
BASELINE = ROOT / "scripts" / "duplicate_code_baseline.json"

#: Bezeichnungen der Formate von jscpd für den Bericht.
FORMATS = {"python": "Python", "markup": "Vorlagen (HTML)", "typescript": "TypeScript", "javascript": "JavaScript"}
#: Kennzahlen der Baseline und ihr Name in der Statistik von jscpd.
METRICS = {"klone": "clones", "zeilen": "duplicatedLines"}


@dataclass(frozen=True)
class Clone:
    """Ein Klon: Format, Umfang und die beiden Stellen (Datei relativ zum Repo-Root, erste und letzte Zeile)."""

    format: str
    lines: int
    tokens: int
    first: str
    second: str


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG).open("rb") as handle:
        return tomllib.load(handle)


def jscpd_binary() -> str:
    """jscpd liegt nach ``pip install`` neben dem Python-Interpreter, sonst im Suchpfad."""
    for name in ("jscpd.exe", "jscpd"):
        candidate = Path(sys.executable).parent / name
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("jscpd")
    if found is None:
        raise SystemExit('jscpd fehlt: pip install "jscpd==<Version aus mandari/pyproject.toml>"')
    return found


def command(binary: str, config: dict[str, Any], output: Path) -> list[str]:
    """
    Aufruf von jscpd nach der Konfiguration; der JSON-Bericht landet in ``output``.

    Die Muster der Ausnahmen werden je gelesenem Pfad verankert (``mandari/**/tests/**``): jscpd vergleicht
    ein freies ``**/tests/**`` mit dem ganzen Pfad der Datei und nähme sonst alles aus, sobald das Repository
    selbst unter einem Verzeichnis ``tests`` liegt.
    """
    scope = config["umfang"]
    ignored = [f"{path}/{pattern}" for path in scope["pfade"] for pattern in config["ausgenommen"]["dateien"]]
    return [
        binary,
        *scope["pfade"],
        "--absolute",
        "--no-colors",
        "--fail-on-empty",
        "--format",
        ",".join(scope["formate"]),
        "--min-tokens",
        str(scope["mindest_token"]),
        "--min-lines",
        str(scope["mindest_zeilen"]),
        "--ignore",
        ",".join(ignored),
        "--reporters",
        "json",
        "--output",
        str(output),
    ]


def _relative(name: str, root: Path) -> str:
    """Pfad aus dem Bericht relativ zum Repo-Root; jscpd schreibt unter Windows ``//?/C:/…``."""
    path = name.replace("\\", "/")
    if path.startswith("//?/"):
        path = path[4:]
    base = root.resolve().as_posix()
    if path.lower().startswith(base.lower() + "/"):
        return path[len(base) + 1 :]
    return path


def parse_report(
    data: dict[str, Any], config: dict[str, Any], root: Path
) -> tuple[dict[str, dict[str, int]], list[Clone]]:
    """(Kennzahlen je Format, Klone – die größten zuerst) aus dem JSON-Bericht von jscpd."""
    statistics = data["statistics"]["formats"]
    current = {
        fmt: {key: int(statistics.get(fmt, {}).get(source, 0)) for key, source in METRICS.items()}
        for fmt in sorted(config["umfang"]["formate"])
    }
    clones = []
    for entry in data["duplicates"]:
        first, second = entry["firstFile"], entry["secondFile"]
        clones.append(
            Clone(
                format=entry["format"],
                lines=int(entry["lines"]),
                tokens=int(entry["tokens"]),
                first=f"{_relative(first['name'], root)}:{first['start']}-{first['end']}",
                second=f"{_relative(second['name'], root)}:{second['start']}-{second['end']}",
            )
        )
    return current, sorted(clones, key=lambda c: (-c.lines, -c.tokens, c.first, c.second))


def measure(
    root: Path | None = None, config: dict[str, Any] | None = None
) -> tuple[dict[str, dict[str, int]], list[Clone]]:
    root = root or ROOT
    config = config or load_config()
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(  # noqa: S603 – fester Befehl aus der Konfiguration, keine Shell
            command(jscpd_binary(), config, Path(directory)), cwd=root, capture_output=True, text=True, check=False
        )
        report_file = Path(directory) / "jscpd-report.json"
        if result.returncode != 0 or not report_file.is_file():
            raise SystemExit(f"jscpd ist gescheitert (Code {result.returncode}):\n{result.stdout}\n{result.stderr}")
        data = json.loads(report_file.read_text(encoding="utf-8"))
    return parse_report(data, config, root)


def compare(current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]) -> tuple[list[str], list[str]]:
    """(Verstöße, gesunkene Werte). Ein Format ohne Eintrag in der Baseline hat die Grenze 0."""
    violations: list[str] = []
    lowered: list[str] = []
    for fmt in sorted(set(current) | set(baseline)):
        for key in METRICS:
            value = current.get(fmt, {}).get(key, 0)
            limit = baseline.get(fmt, {}).get(key, 0)
            if value > limit:
                violations.append(f"{fmt}: {value} {key}, erlaubt {limit}")
            elif value < limit:
                lowered.append(f"{fmt}: {value} {key} statt {limit}")
    return violations, lowered


def lowered_baseline(
    current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """Neue Baseline: je Format und Kennzahl der kleinere Wert; nichts kommt hinzu, nichts steigt."""
    return {
        fmt: {key: min(limit, current.get(fmt, {}).get(key, 0)) for key, limit in values.items()}
        for fmt, values in baseline.items()
    }


def load_baseline(path: Path | None = None) -> dict[str, dict[str, int]]:
    """Erlaubter Höchststand je Format; ohne Datei ist nichts erlaubt."""
    path = path or BASELINE
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(fmt): {str(k): int(v) for k, v in values.items()} for fmt, values in data["formate"].items()}


def write_baseline(formats: dict[str, dict[str, int]], path: Path | None = None) -> None:
    data = {
        "beschreibung": (
            "Doppelter Code nach jscpd (Klone und doppelte Zeilen je Format) nach "
            "scripts/duplicate_code_ratchet.toml; dürfen nur sinken "
            "(scripts/check_duplicate_code_ratchet.py, Issue #1113)."
        ),
        "formate": dict(sorted(formats.items())),
    }
    (path or BASELINE).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def report(
    current: dict[str, dict[str, int]], clones: list[Clone], baseline: dict[str, dict[str, int]], top: int = 10
) -> str:
    """Bericht als Markdown: Kennzahlen je Format, die größten Klone, alle Klone."""
    total = {key: sum(values[key] for values in current.values()) for key in METRICS}
    out = [
        "## Doppelter Code (jscpd)",
        "",
        f"**{total['klone']} Klone, {total['zeilen']} doppelte Zeilen.**",
        "",
        "| Format | Klone | Grenze | Doppelte Zeilen | Grenze |",
        "|---|---:|---:|---:|---:|",
    ]
    for fmt, values in current.items():
        limit = baseline.get(fmt, {})
        out.append(
            f"| {FORMATS.get(fmt, fmt)} | {values['klone']} | {limit.get('klone', 0)} "
            f"| {values['zeilen']} | {limit.get('zeilen', 0)} |"
        )
    out += [
        "",
        f"### Die {min(top, len(clones))} größten Klone",
        "",
        "| Zeilen | Format | Stelle 1 | Stelle 2 |",
        "|---:|---|---|---|",
    ]
    out += [f"| {c.lines} | {FORMATS.get(c.format, c.format)} | `{c.first}` | `{c.second}` |" for c in clones[:top]]
    out += ["", "<details><summary>Alle Klone</summary>", "", "```"]
    out += [f"{c.lines} Zeilen ({c.format}): {c.first} <-> {c.second}" for c in clones]
    out += ["```", "", "</details>", ""]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Ratchet für doppelten Code (jscpd)")
    parser.add_argument("--update", action="store_true", help="Baseline auf den niedrigeren Stand senken")
    parser.add_argument("--verbose", action="store_true", help="alle Klone ausgeben")
    parser.add_argument("--json", action="store_true", help="aktuellen Stand als JSON ausgeben")
    parser.add_argument("--report", metavar="DATEI", help="Bericht als Markdown in diese Datei schreiben")
    args = parser.parse_args(argv)

    current, clones = measure()
    if args.json:
        print(json.dumps({"formate": current, "klone": [asdict(c) for c in clones]}, indent=2, ensure_ascii=False))
        return 0

    baseline = load_baseline()
    violations, lowered = compare(current, baseline)
    if args.update and lowered:
        write_baseline(lowered_baseline(current, baseline))
        print(f"Baseline nachgezogen: {BASELINE.relative_to(ROOT).as_posix()}")
        baseline = load_baseline()
        lowered = []
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report(current, clones, baseline), encoding="utf-8")
    if args.verbose:
        for c in clones:
            print(f"{c.lines} Zeilen ({c.format}): {c.first} <-> {c.second}")

    for fmt, values in current.items():
        limit = baseline.get(fmt, {})
        print(
            f"{fmt:12s} {values['klone']:4d} Klone (Baseline {limit.get('klone', 0)}), "
            f"{values['zeilen']:5d} doppelte Zeilen (Baseline {limit.get('zeilen', 0)})"
        )
    if violations:
        print(
            "\nMehr doppelter Code als erlaubt. Bitte den kopierten Block zusammenführen (Funktion, "
            "Basisklasse, Include oder Komponente), statt die Baseline zu erhöhen:"
        )
        print("\n".join(f"- {line}" for line in violations))
        print("Alle Klone, die größten zuerst: python scripts/check_duplicate_code_ratchet.py --verbose")
        return 1
    if lowered:
        print("\nHinweis: Werte gesunken, Baseline nachziehen: python scripts/check_duplicate_code_ratchet.py --update")
        print("\n".join(f"- {line}" for line in lowered))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für die Schichtregeln: Ausnahmen von den import-linter-Verträgen dürfen nur weniger werden.

Die Verträge stehen in ``mandari/pyproject.toml`` (``[tool.importlinter]``, siehe
``docs/adr/20260929-schichtenmodell.md``); ob der Code sie einhält, prüft ``lint-imports`` aus
``mandari/``. Dieses Skript wacht über die Ausnahmelisten (``ignore_imports``) und über die
Vollständigkeit der Schichten. Es schlägt fehl, wenn

- einer der drei Verträge fehlt oder eine andere Art hat,
- eine Ausnahme dazukommt, die nicht in der Baseline steht (``scripts/import_linter_baseline.json``),
- eine Ausnahme Platzhalter nutzt (einzige erlaubte: die Freistellung der Tests) oder doppelt steht,
- ein Paket keiner Schicht zugeordnet ist (neue App, neues Wurzelpaket), womit es ungeprüft bliebe,
- die Baseline Ausnahmen enthält, die es nicht mehr gibt: mit ``--update`` nachziehen und im selben
  Pull Request committen, damit der Abbau festgeschrieben ist.

``--update`` streicht nur, es fügt nie Ausnahmen hinzu.

Aufruf aus dem Repo-Root:  python scripts/check_import_linter_ratchet.py [--update]
"""

from __future__ import annotations

import json
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "mandari"
PYPROJECT = PROJECT / "pyproject.toml"
BASELINE = ROOT / "scripts" / "import_linter_baseline.json"

#: Pflichtverträge (Kennung → Art), ADR Schichtenmodell.
CONTRACTS = {"schichten": "layers", "module-unabhaengig": "independence", "plattform-fachfrei": "forbidden"}
#: Einziger erlaubter Eintrag mit Platzhaltern: Tests prüfen modulübergreifend und sind ausgenommen.
TEST_EXEMPTION = "**.tests.** -> **"
#: Pakete unter mandari/, die bewusst außerhalb der Schichten liegen.
OUTSIDE_LAYERS = {"mandari", "tests_e2e"}


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or PYPROJECT
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    linter = config.get("tool", {}).get("importlinter")
    if not isinstance(linter, dict):
        raise SystemExit(f"{path}: Abschnitt [tool.importlinter] fehlt")
    return linter


def contract_problems(linter: dict[str, Any]) -> list[str]:
    found = {c.get("id"): c.get("type") for c in linter.get("contracts", [])}
    problems = []
    for contract_id, kind in CONTRACTS.items():
        if contract_id not in found:
            problems.append(f"Vertrag „{contract_id}“ fehlt")
        elif found[contract_id] != kind:
            problems.append(f"Vertrag „{contract_id}“ muss vom Typ {kind} sein, ist {found[contract_id]}")
    return problems


def exceptions(linter: dict[str, Any]) -> tuple[dict[str, list[str]], list[str]]:
    """Ausnahmen je Pflichtvertrag (ohne Test-Freistellung) und Verstöße gegen die Form."""
    result: dict[str, list[str]] = {}
    problems: list[str] = []
    for contract in linter.get("contracts", []):
        contract_id = contract.get("id")
        if contract_id not in CONTRACTS:
            continue
        entries = [str(entry).strip() for entry in contract.get("ignore_imports", [])]
        for entry, count in Counter(entries).items():
            if count > 1:
                problems.append(f"{contract_id}: Ausnahme doppelt: {entry}")
        for entry in entries:
            if entry != TEST_EXEMPTION and "*" in entry:
                problems.append(f"{contract_id}: Platzhalter sind nicht erlaubt (je Import ein Eintrag): {entry}")
        result[str(contract_id)] = sorted({entry for entry in entries if entry != TEST_EXEMPTION})
    return result, problems


def _layer_modules(linter: dict[str, Any]) -> set[str]:
    modules: set[str] = set()
    for contract in linter.get("contracts", []):
        if contract.get("id") != "schichten":
            continue
        for layer in contract.get("layers", []):
            for part in str(layer).replace("|", ":").split(":"):
                modules.add(part.strip().strip("()").strip())
    return modules


def coverage_problems(linter: dict[str, Any], project: Path | None = None) -> list[str]:
    """Jedes Wurzelpaket und jede App unter apps/ muss einer Schicht zugeordnet sein."""
    project = project or PROJECT
    roots = set(linter.get("root_packages", []))
    layered = _layer_modules(linter)
    problems = []
    for package in sorted(p.parent.name for p in project.glob("*/__init__.py")):
        if package in OUTSIDE_LAYERS:
            continue
        if package not in roots:
            problems.append(f"Paket „{package}“ fehlt in root_packages")
        if package != "apps" and package not in layered:
            problems.append(f"Paket „{package}“ ist keiner Schicht zugeordnet")
    for app in sorted(p.parent.name for p in (project / "apps").glob("*/__init__.py")):
        if f"apps.{app}" not in layered:
            problems.append(f"App „apps.{app}“ ist keiner Schicht zugeordnet")
    return problems


def compare(current: dict[str, list[str]], baseline: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """Neue Ausnahmen (verboten) und weggefallene (Baseline nachziehen)."""
    added: list[str] = []
    removed: list[str] = []
    for contract_id in CONTRACTS:
        now = set(current.get(contract_id, []))
        before = set(baseline.get(contract_id, []))
        added += [f"{contract_id}: {entry}" for entry in sorted(now - before)]
        removed += [f"{contract_id}: {entry}" for entry in sorted(before - now)]
    return added, removed


def main(argv: list[str]) -> int:
    update = "--update" in argv
    linter = load_config()
    problems = contract_problems(linter) + coverage_problems(linter)
    current, form_problems = exceptions(linter)
    problems += form_problems
    if not BASELINE.exists():
        print(f"Baseline {BASELINE.relative_to(ROOT)} fehlt.")
        return 1
    baseline: dict[str, list[str]] = json.loads(BASELINE.read_text(encoding="utf-8"))
    added, removed = compare(current, baseline)

    for contract_id in CONTRACTS:
        print(
            f"{contract_id:20s} {len(current.get(contract_id, [])):4d} Ausnahmen (Baseline {len(baseline.get(contract_id, []))})"
        )
    if problems:
        print("\nSchichtregeln unvollständig oder Ausnahmeliste unzulässig:")
        print("\n".join(f"  - {problem}" for problem in problems))
    if added:
        print("\nNeue Ausnahmen sind nicht erlaubt (Abhängigkeit über Ereignis, Befehl oder Fassade lösen):")
        print("\n".join(f"  + {entry}" for entry in added))
    if problems or added:
        return 1
    if removed:
        if update:
            BASELINE.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"\nBaseline nachgezogen: {len(removed)} Ausnahmen gestrichen.")
            return 0
        print("\nAusnahmen abgebaut, Baseline noch nicht nachgezogen (mit --update, im selben PR committen):")
        print("\n".join(f"  - {entry}" for entry in removed))
        return 1
    print("\nOK: keine neuen Ausnahmen.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

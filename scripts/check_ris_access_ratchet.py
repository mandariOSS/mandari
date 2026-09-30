# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für Direktzugriffe auf den RIS-Bestand (Issue #522).

Fachmodule lesen RIS-Daten über die Lese-Fassade ``hub/ris/selectors.py``
(``docs/adr/20260929-kanonisches-modell.md``). Dieses Skript zählt je Datei die Zugriffe auf
``OParl*.objects`` (auch ``_default_manager``/``_base_manager`` und umbenannte Importe) außerhalb
von ``hub`` und ``insight_core``. Tests, Migrationen und die E2E-Tests zählen nicht mit: Sie legen
Testdaten an und koppeln nichts im Betrieb.

Die Baseline ``scripts/ris_access_baseline.json`` hält je Datei den erlaubten Höchststand fest.
Das Skript schlägt fehl, wenn

- eine Datei mehr Zugriffe hat als in der Baseline oder eine neue Datei Zugriffe bekommt,
- Zugriffe abgebaut sind, die Baseline aber noch den alten Stand hat: mit ``--update`` nachziehen
  und im selben Pull Request committen, damit der niedrigere Stand festgeschrieben ist.

``--update`` senkt nur: Es übernimmt kleinere Werte und streicht Dateien ohne Zugriffe, nimmt aber
nie eine Datei auf und erhöht nie einen Wert. Wandert Code samt Zugriff in eine andere Datei, geht das
nur über eine Änderung der Baseline von Hand, die im Review sichtbar ist; die Summe darf dabei nicht
steigen. ``--json`` gibt den aktuellen Stand aus.

Aufruf aus dem Repo-Root:  python scripts/check_ris_access_ratchet.py [--update] [--json]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "mandari"
BASELINE = ROOT / "scripts" / "ris_access_baseline.json"

#: Pakete, die den RIS-Bestand besitzen bzw. die Fassade bereitstellen (dürfen direkt zugreifen).
OWNERS = ("hub", "insight_core")
#: Verzeichnisse, die nie mitzählen.
SKIPPED_DIRS = {"migrations", "tests", "tests_e2e", "node_modules", "static", "staticfiles", "media", "__pycache__"}
MODEL = re.compile(r"^OParl[A-Z][A-Za-z]*$")
MANAGERS = {"objects", "_default_manager", "_base_manager"}


def is_counted(relative: Path) -> bool:
    """Zählt diese Datei (Pfad relativ zu ``mandari/``) mit?"""
    parts = relative.parts
    if not parts or parts[0] in OWNERS:
        return False
    if any(part in SKIPPED_DIRS or part.startswith(".") for part in parts[:-1]):
        return False
    name = parts[-1]
    return not (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py")


def count_accesses(source: str) -> int:
    """Zugriffe auf Manager von RIS-Modellen in einem Modul."""
    tree = ast.parse(source)
    aliases = {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("insight_core")
        for alias in node.names
        if alias.asname and MODEL.match(alias.name)
    }
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in MANAGERS:
            continue
        target = node.value
        name = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ""
        if MODEL.match(name) or name in aliases:
            count += 1
    return count


def measure(project: Path | None = None) -> dict[str, int]:
    """Zugriffe je Datei (nur Dateien mit mindestens einem Zugriff), sortiert nach Pfad."""
    project = project or PROJECT
    result: dict[str, int] = {}
    for path in sorted(project.rglob("*.py")):
        relative = path.relative_to(project)
        if not is_counted(relative):
            continue
        count = count_accesses(path.read_text(encoding="utf-8"))
        if count:
            result[relative.as_posix()] = count
    return result


def compare(current: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    """(Verstöße, abgebaute Zugriffe ohne nachgezogene Baseline)."""
    violations: list[str] = []
    lowered: list[str] = []
    for name, count in current.items():
        allowed = baseline.get(name)
        if allowed is None:
            violations.append(f"{name}: {count} neue Zugriffe (Datei steht nicht in der Baseline)")
        elif count > allowed:
            violations.append(f"{name}: {count} Zugriffe, erlaubt {allowed}")
    for name, allowed in baseline.items():
        count = current.get(name, 0)
        if count < allowed:
            lowered.append(f"{name}: {count} statt {allowed}")
    return violations, lowered


def lowered_baseline(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """Neue Baseline: je Datei der kleinere Wert, Dateien ohne Zugriffe entfallen, nichts kommt hinzu."""
    return {name: min(allowed, current.get(name, 0)) for name, allowed in baseline.items() if current.get(name, 0)}


def load_baseline(path: Path | None = None) -> dict[str, int]:
    data = json.loads((path or BASELINE).read_text(encoding="utf-8"))
    return {str(name): int(count) for name, count in data["files"].items()}


def write_baseline(files: dict[str, int], path: Path | None = None) -> None:
    data = {
        "beschreibung": (
            "Direktzugriffe auf OParl*.objects außerhalb von hub und insight_core je Datei; "
            "dürfen nur sinken (scripts/check_ris_access_ratchet.py, Issue #522)."
        ),
        "files": dict(sorted(files.items())),
    }
    (path or BASELINE).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    current = measure()
    if "--json" in argv:
        print(json.dumps({"summe": sum(current.values()), "files": current}, indent=2, ensure_ascii=False))
        return 0
    baseline = load_baseline()
    violations, lowered = compare(current, baseline)
    if "--update" in argv:
        write_baseline(lowered_baseline(current, baseline))
        print(f"Baseline nachgezogen: {BASELINE.relative_to(ROOT).as_posix()}")
        baseline = load_baseline()
        lowered = []
    total, allowed_total = sum(current.values()), sum(baseline.values())
    print(f"Direktzugriffe auf OParl*.objects außerhalb von hub/insight_core: {total} (Baseline {allowed_total})")
    if violations:
        print("\nNeue Direktzugriffe – bitte die Lese-Fassade hub/ris/selectors.py nutzen (bei Bedarf ergänzen):")
        print("\n".join(f"- {line}" for line in violations))
        return 1
    if lowered:
        print("\nZugriffe abgebaut, Baseline noch auf altem Stand. Bitte festschreiben:")
        print("  python scripts/check_ris_access_ratchet.py --update  (und die Baseline mitcommitten)")
        print("\n".join(f"- {line}" for line in lowered))
        return 1
    print("OK: keine neuen Direktzugriffe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

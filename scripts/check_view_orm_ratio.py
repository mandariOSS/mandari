# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für ORM-Zugriffe in Views (Issue #160, Service-Layer).

Zählt je Paket unter ``mandari/apps/work/<paket>/`` die ORM-Aufrufe in View-Modulen
(``views.py``, ``views_*.py``, ``views/*.py``) und die Funktionen über 100 Zeilen. Die Werte
stehen in ``scripts/view_orm_baseline.json`` und dürfen nur sinken; ``--update`` schreibt die
Baseline nach, ``--json`` gibt die aktuellen Werte aus.

    python scripts/check_view_orm_ratio.py [--update] [--json]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "mandari" / "apps" / "work"
BASELINE = ROOT / "scripts" / "view_orm_baseline.json"
MAX_FUNCTION_LINES = 100

# Aufrufe, die direkt auf das ORM gehen (Queryset-Aufbau, Schreiben, Löschen)
ORM_RE = re.compile(
    r"\.objects\b|\.(?:filter|exclude|get|create|update|delete|save|get_or_create|update_or_create|"
    r"bulk_create|bulk_update|select_related|prefetch_related|annotate|aggregate|values|values_list|"
    r"order_by|exists|count|first|last|distinct|only|defer)\("
)
COMMENT_RE = re.compile(r"^\s*#")


def view_files(package: Path) -> list[Path]:
    files = [p for p in package.glob("views*.py")]
    if (package / "views").is_dir():
        files += sorted((package / "views").glob("*.py"))
    return sorted(files)


def orm_calls(path: Path) -> int:
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if COMMENT_RE.match(line):
            continue
        count += len(ORM_RE.findall(line))
    return count


def long_functions(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.end_lineno:
            length = node.end_lineno - node.lineno + 1
            if length > MAX_FUNCTION_LINES:
                found.append((f"{path.relative_to(ROOT).as_posix()}:{node.name}", length))
    return found


def measure() -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for package in sorted(p for p in WORK.iterdir() if p.is_dir() and not p.name.startswith("_")):
        files = view_files(package)
        if not files:
            continue
        calls = sum(orm_calls(f) for f in files)
        longs = [fn for f in files for fn in long_functions(f)]
        result[package.name] = {"orm_calls": calls, "long_functions": len(longs)}
    return result


def main() -> int:
    current = measure()
    if "--json" in sys.argv:
        print(json.dumps(current, indent=2, ensure_ascii=False))
        return 0
    if "--update" in sys.argv or not BASELINE.exists():
        BASELINE.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Baseline geschrieben: {BASELINE.relative_to(ROOT).as_posix()}")
        return 0
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    failed = False
    improved = False
    print(f"{'Paket':<16}{'ORM in Views':>14}{'Baseline':>10}{'Fn>100':>9}{'Baseline':>10}")
    for name, values in current.items():
        base = baseline.get(name, values)
        for key in ("orm_calls", "long_functions"):
            if values[key] > base[key]:
                failed = True
            if values[key] < base[key]:
                improved = True
        flag = (
            "ZU HOCH"
            if values["orm_calls"] > base["orm_calls"] or values["long_functions"] > base["long_functions"]
            else "OK"
        )
        print(
            f"{name:<16}{values['orm_calls']:>14}{base['orm_calls']:>10}{values['long_functions']:>9}{base['long_functions']:>10}  {flag}"
        )
    if failed:
        print("\nService-Layer-Ratchet verletzt: ORM-Zugriffe oder lange Funktionen in Views haben zugenommen.")
        return 1
    if improved:
        print("\nHinweis: Werte gesunken, Baseline mit --update nachziehen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

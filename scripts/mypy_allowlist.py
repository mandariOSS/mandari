# SPDX-License-Identifier: AGPL-3.0-or-later
"""
mypy-Gate mit schrumpfender Allowlist.

mypy läuft strikt (django-stubs). Module, die heute noch Fehler haben, stehen in
scripts/mypy_allowlist.txt und werden vorerst toleriert. Regeln:

- Ein Modul außerhalb der Allowlist mit Fehlern lässt den Lauf fehlschlagen (neuer Code muss typisiert sein).
- Ein Modul auf der Allowlist, das inzwischen fehlerfrei ist, wird mit `--update` gestrichen.
- Die Allowlist darf nur schrumpfen; `--update` fügt nie Module hinzu.

Aufruf aus dem Repo-Root:  python scripts/mypy_allowlist.py [--update] [--verbose]
"""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "mandari"
ALLOWLIST = ROOT / "scripts" / "mypy_allowlist.txt"
TARGETS = ["apps", "insight_core", "insight_sync", "insight_search", "insight_ai", "oparl_api", "mandari"]


def run_mypy() -> Counter[str]:
    cmd = [
        sys.executable,
        "-m",
        "mypy",
        *TARGETS,
        "--ignore-missing-imports",
        "--no-error-summary",
        "--no-color-output",
    ]
    proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    errors: Counter[str] = Counter()
    for line in proc.stdout.splitlines():
        head, sep, rest = line.partition(": error:")
        if not sep:
            continue
        path = head.rsplit(":", 1)[0].replace("\\", "/")
        if not path.endswith(".py"):
            continue
        module = path[:-3].replace("/", ".").removesuffix(".__init__")
        errors[module] += 1
    return errors


def main(argv: list[str]) -> int:
    update = "--update" in argv
    verbose = "--verbose" in argv
    errors = run_mypy()
    allowed = set(ALLOWLIST.read_text(encoding="utf-8").split()) if ALLOWLIST.exists() else set()
    if not ALLOWLIST.exists() or "--init" in argv:
        ALLOWLIST.write_text("\n".join(sorted(errors)) + "\n", encoding="utf-8")
        print(f"Allowlist angelegt: {len(errors)} Module mit {sum(errors.values())} Fehlern")
        return 0

    new_failures = {m: n for m, n in errors.items() if m not in allowed}
    now_clean = sorted(m for m in allowed if m not in errors)
    if verbose:
        for m, n in sorted(errors.items(), key=lambda kv: -kv[1])[:20]:
            print(f"  {n:5d}  {m}")
    print(f"mypy: {len(errors)} Module mit Fehlern ({sum(errors.values())} Fehler), Allowlist {len(allowed)} Module")
    if new_failures:
        print("\nTypfehler außerhalb der Allowlist (neuer oder angefasster Code muss typisiert sein):")
        for m, n in sorted(new_failures.items()):
            print(f"  {m}: {n} Fehler")
        return 1
    if now_clean:
        if update:
            ALLOWLIST.write_text("\n".join(sorted(allowed - set(now_clean))) + "\n", encoding="utf-8")
            print(f"Allowlist verkleinert um {len(now_clean)} Module.")
        else:
            print(
                f"Hinweis: {len(now_clean)} Module sind inzwischen fehlerfrei; mit --update aus der Allowlist streichen."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

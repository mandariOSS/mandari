# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüft, dass ``requirements.lock`` zu ``requirements.txt`` passt.

Hintergrund: Installiert wird überall das Lockfile — im ``Dockerfile``, in allen
CI-Jobs und bei ``pip-audit``. ``requirements.txt`` hält nur die Untergrenzen.
Dependabot hebt aber genau diese Grenzen an, ohne das Lockfile neu zu erzeugen.
Ein solcher PR sieht nach einem Update aus, ändert aber nichts am Laufenden und
lässt beide Dateien auseinanderlaufen (Issue #241).

Geprüft wird bewusst **nicht** durch Neuerzeugen des Lockfiles: ``uv pip compile``
löst ``>=`` gegen die Live-Paketquelle auf und lieferte bei jeder neuen
Veröffentlichung ein anderes Ergebnis — das Gate wäre von fremden Releases
abhängig. Stattdessen offline und deterministisch:

1. Jede direkte Anforderung aus ``requirements.txt`` kommt im Lockfile vor.
2. Die dort festgeschriebene Version erfüllt die Bedingung aus ``requirements.txt``.

    python scripts/check_requirements_lock.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REQUIREMENTS = Path(__file__).resolve().parent.parent / "mandari" / "requirements.txt"
LOCKFILE = REQUIREMENTS.with_name("requirements.lock")

PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;]+)")


def direct_requirements(path: Path) -> list[Requirement]:
    """Direkte Anforderungen; Kommentare, Optionen und Verweise werden übergangen."""
    found = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            found.append(Requirement(line))
        except Exception as exc:  # pragma: no cover - defekte Zeile soll auffallen
            sys.exit(f"{path.name}: Zeile nicht lesbar: {raw.strip()!r} ({exc})")
    return found


def pinned_versions(path: Path) -> dict[str, str]:
    pins = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        match = PIN.match(raw.strip())
        if match:
            pins[canonicalize_name(match.group("name"))] = match.group("version")
    return pins


def main() -> int:
    requirements = direct_requirements(REQUIREMENTS)
    pins = pinned_versions(LOCKFILE)

    problems: list[str] = []
    for requirement in requirements:
        name = canonicalize_name(requirement.name)
        pinned = pins.get(name)
        if pinned is None:
            problems.append(f"{requirement.name}: steht in requirements.txt, fehlt im Lockfile")
            continue
        if not requirement.specifier.contains(Version(pinned), prereleases=True):
            problems.append(
                f"{requirement.name}: Lockfile hat {pinned}, requirements.txt verlangt {requirement.specifier}"
            )

    if problems:
        print("requirements.lock passt nicht zu requirements.txt:\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nLockfile neu erzeugen:\n"
            "  cd mandari && uv pip compile requirements.txt -o requirements.lock --python-version 3.12"
        )
        return 1

    print(f"OK: {len(requirements)} direkte Anforderungen, alle im Lockfile und innerhalb ihrer Bedingung.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

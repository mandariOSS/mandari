# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüft, dass die Versionsangaben im Repo zusammenpassen.

Vor dem Release v0.10.0 standen drei verschiedene Nummern an drei Stellen:
Git-Tag ``v0.9.0-beta``, ``mandari/pyproject.toml`` 2.0.0 und
``mandari/package.json`` 1.0.0. Keine davon sagte, was tatsächlich lief. Beim
Schließen einer Sicherheitslücke ist das nicht nur unsauber: Im Advisory steht
dann eine Commit-Kennung statt einer Version, und Selbst-Hoster können nicht
erkennen, ob ihr Stand betroffen ist.

    python scripts/check_version_consistency.py
    python scripts/check_version_consistency.py --tag v0.10.0

Ohne ``--tag`` wird geprüft, dass die Dateien übereinstimmen. Mit ``--tag``
zusätzlich, dass der Release-Tag dieselbe Version nennt.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
PYPROJECT = WURZEL / "mandari" / "pyproject.toml"
PACKAGE_JSON = WURZEL / "mandari" / "package.json"


def aus_pyproject() -> str:
    for zeile in PYPROJECT.read_text(encoding="utf-8").splitlines():
        treffer = re.match(r'^version\s*=\s*"([^"]+)"', zeile.strip())
        if treffer:
            return treffer.group(1)
    sys.exit(f"{PYPROJECT.name}: keine version-Zeile gefunden")


def aus_package_json() -> str:
    daten = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    version = daten.get("version")
    if not version:
        sys.exit(f"{PACKAGE_JSON.name}: kein version-Feld gefunden")
    return str(version)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Release-Tag, z. B. v0.10.0")
    args = parser.parse_args()

    py = aus_pyproject()
    npm = aus_package_json()

    probleme: list[str] = []
    if py != npm:
        probleme.append(f"mandari/pyproject.toml sagt {py}, mandari/package.json sagt {npm}")

    if args.tag:
        # Release-Tags tragen ein führendes "v", die Dateien nicht.
        tag_version = args.tag.lstrip("vV")
        if tag_version != py:
            probleme.append(f"Tag {args.tag} passt nicht zu mandari/pyproject.toml ({py})")
        if tag_version != npm:
            probleme.append(f"Tag {args.tag} passt nicht zu mandari/package.json ({npm})")

    if probleme:
        print("Versionsangaben passen nicht zusammen:\n")
        for problem in probleme:
            print(f"  - {problem}")
        print(
            "\nBeim Anheben der Version beide Dateien ändern und den Tag danach setzen.\n"
            "Die Version steht in mandari/pyproject.toml und mandari/package.json."
        )
        return 1

    print(f"OK: Version {py} in pyproject.toml und package.json" + (f", Tag {args.tag} passt." if args.tag else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

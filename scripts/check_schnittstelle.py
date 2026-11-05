# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfungen der eingecheckten Schnittstellenbeschreibung, ohne Django (Issue #1205).

    python scripts/check_schnittstelle.py [--basis <git-ref>]

1. **Befehlsparität:** Jeder Befehl im Register (``mandari/hub/contracts/schemas/<name>/v<n>.json`` mit
   ``"x-kind": "command"``) hat einen Endpunkt mit ``x-mandari-command`` in ``docs/schnittstelle/openapi.json``
   oder steht in der Ausnahmeliste ``mandari/hub/api/openapi/befehle_ohne_endpunkt.json``. Ein Eintrag der
   Liste, der einen Endpunkt hat oder kein Befehl mehr ist, muss heraus.
2. **Ratchet (mit ``--basis``):** Die Ausnahmeliste darf gegenüber dem Stand der Basis (etwa ``origin/dev``)
   nur kürzer werden. Gibt es die Liste dort noch nicht, entfällt der Vergleich.

Dass die eingecheckte Beschreibung zum Code passt, prüft ``hub/api/tests/test_schnittstelle.py``
(``manage.py schnittstelle_beschreibung --pruefen``); brechende Änderungen gegenüber der Basis erkennt die CI
mit oasdiff.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DESCRIPTION = ROOT / "docs" / "schnittstelle" / "openapi.json"
EXCEPTIONS = ROOT / "mandari" / "hub" / "api" / "openapi" / "befehle_ohne_endpunkt.json"
SCHEMAS = ROOT / "mandari" / "hub" / "contracts" / "schemas"
COMMAND_EXTENSION = "x-mandari-command"
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def registered_commands(schemas: Path = SCHEMAS) -> set[str]:
    """Namen der Befehle im Register (alle Versionen eines Befehls zählen als einer)."""
    found = set()
    for path in schemas.glob("*/v*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("x-kind") == "command":
            found.add(path.parent.name)
    return found


def commands_with_endpoint(document: dict[str, Any]) -> set[str]:
    found = set()
    for item in document.get("paths", {}).values():
        for method in METHODS:
            name = item.get(method, {}).get(COMMAND_EXTENSION)
            if name:
                found.add(str(name))
    return found


def exceptions(text: str) -> list[str]:
    data = json.loads(text)
    entries = data.get("befehle")
    if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
        raise SystemExit(f"{EXCEPTIONS.relative_to(ROOT)}: 'befehle' muss eine Liste von Namen sein")
    return entries


def parity_problems(commands: set[str], with_endpoint: set[str], listed: list[str]) -> list[str]:
    problems = []
    if len(set(listed)) != len(listed):
        problems.append("Ausnahmeliste: Einträge doppelt")
    if listed != sorted(listed):
        problems.append("Ausnahmeliste: bitte alphabetisch sortieren")
    for name in sorted(commands - with_endpoint - set(listed)):
        problems.append(
            f"Befehl {name} hat keinen Endpunkt in der Beschreibung ({COMMAND_EXTENSION}). Endpunkt anbieten; "
            "neue Ausnahmen sind nicht erlaubt."
        )
    for name in sorted(set(listed) & with_endpoint):
        problems.append(f"Befehl {name} hat jetzt einen Endpunkt: aus der Ausnahmeliste streichen.")
    for name in sorted(set(listed) - commands):
        problems.append(f"Ausnahmeliste nennt {name}, das ist kein Befehl im Register: streichen.")
    for name in sorted(with_endpoint - commands):
        problems.append(f"Endpunkt nennt den Befehl {name}, den es im Register nicht gibt.")
    return problems


def ratchet_problems(listed: list[str], base: list[str] | None) -> list[str]:
    if base is None:
        return []
    return [
        f"Ausnahmeliste: {name} ist neu. Die Liste darf nur kürzer werden; der Befehl braucht einen Endpunkt."
        for name in sorted(set(listed) - set(base))
    ]


def base_exceptions(ref: str) -> list[str] | None:
    """Ausnahmeliste im Stand ``ref``; ``None``, wenn es sie dort nicht gibt."""
    relative = EXCEPTIONS.relative_to(ROOT).as_posix()
    result = subprocess.run(  # noqa: S603 – fester Befehl, ref aus der CI
        ["git", "show", f"{ref}:{relative}"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        print(f"Hinweis: {relative} gibt es in {ref} nicht, der Vergleich entfällt.")
        return None
    return exceptions(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument("--basis", help="Git-Stand, gegen den die Ausnahmeliste nur kürzer werden darf")
    args = parser.parse_args(argv)

    document = json.loads(DESCRIPTION.read_text(encoding="utf-8"))
    listed = exceptions(EXCEPTIONS.read_text(encoding="utf-8"))
    problems = parity_problems(registered_commands(), commands_with_endpoint(document), listed)
    if args.basis:
        problems += ratchet_problems(listed, base_exceptions(args.basis))
    for problem in problems:
        print(f"FEHLER: {problem}")
    if problems:
        return 1
    print(f"Befehlsparität: {len(commands_with_endpoint(document))} mit Endpunkt, {len(listed)} Ausnahmen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

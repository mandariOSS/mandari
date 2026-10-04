# SPDX-License-Identifier: AGPL-3.0-or-later
"""
CI-Gate der Ereignisverträge (Issue #519, ``docs/adr/20260929-ereignisvertraege.md``, Abschnitt Prüfung).

1. **Schema je Ereignis im Code.** Jeder Ereignistyp, den der Code nennt, hat ein Schema im Register:
   Zeichenketten, die wie ein Ereignisname aussehen (Namensregeln aus ``hub/contracts/naming.py``: bekannter
   Bereich, Vergangenheitsform), in ``mandari/``, ``ingestor/src/`` und ``shared/`` ohne Tests und
   Migrationen. Das trifft Erzeuger wie Abonnenten. Die Version prüft das Skript, wo sie im Quelltext
   steht: ``publish("<typ>", version=<n>)`` mit festen Werten und die Modulkonstante ``VERSION`` eines
   Moduls, das Ereignistypen nennt (so führen ``hub/ris`` und der Ingestor ihre Schemaversion). Befehle:
   ``command_handler("<typ>", <n>)``. Was erst zur Laufzeit feststeht, prüft ``publish()`` in den Tests
   gegen das Register (``EVENTS_VALIDATE_CONTRACTS``).
2. **Nur additive Änderungen** gegenüber dem Stand der Zielverzweigung (``--base <ref>``): Schemas und
   Hülle, Regeln in ``hub/contracts/compatibility.py``. Eine brechende Änderung braucht eine neue Version
   (``v<n+1>.json`` neben der alten).
3. **Katalog aktuell.** ``docs/EREIGNISKATALOG.md`` entspricht dem, was ``hub/contracts/catalog.py`` aus dem
   Register erzeugt.

Aufruf aus dem Repo-Root (braucht ``jsonschema`` aus den Django-Abhängigkeiten, kein Django-Setup):

    python scripts/check_event_contracts.py                       # Prüfungen 1 und 3
    python scripts/check_event_contracts.py --base origin/dev     # zusätzlich Prüfung 2
    python scripts/check_event_contracts.py --write-catalog       # Katalog neu schreiben
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "mandari"
CONTRACTS = PROJECT / "hub" / "contracts"
CATALOG = ROOT / "docs" / "EREIGNISKATALOG.md"
#: Quellbäume, in denen Ereignisse erzeugt oder abonniert werden.
SOURCES = (PROJECT, ROOT / "ingestor" / "src", ROOT / "shared")
#: Verzeichnisse, die nie zählen: Tests nennen bewusst auch unbekannte Typen.
SKIPPED_DIRS = frozenset(
    {"tests", "tests_e2e", "migrations", "node_modules", "static", "staticfiles", "media", "__pycache__", ".venv"}
)
#: Ablage der Verträge im Repository (für den Vergleich mit einem anderen Stand).
CONTRACT_PATHS = ("mandari/hub/contracts/schemas", "mandari/hub/contracts/envelope")


@dataclass(frozen=True)
class Reference:
    """Ein Ereignis- oder Befehlsname im Quelltext; ``version`` nur, wenn sie dort feststeht."""

    path: str
    line: int
    name: str
    kind: str
    version: int | None


# --- 1. Schema je Ereignis im Code -----------------------------------------------------------------


def python_files(roots: Iterable[Path] = SOURCES) -> Iterator[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root).parts
            if SKIPPED_DIRS & set(relative[:-1]) or path.name.startswith("test_") or path.name == "conftest.py":
                continue
            yield path


def _int_constant(node: ast.AST | None, module_version: int | None) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id == "VERSION":
        return module_version
    return None


def _module_version(tree: ast.Module) -> int | None:
    """Modulkonstante ``VERSION = <n>`` bzw. ``VERSION: Final = <n>``."""
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        value = node.value if isinstance(node, ast.Assign | ast.AnnAssign) else None
        if any(isinstance(t, ast.Name) and t.id == "VERSION" for t in targets):
            return _int_constant(value, None)
    return None


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def references(source: str, path: str) -> list[Reference]:
    """Ereignis- und Befehlsnamen eines Moduls, mit Version, wo sie im Quelltext feststeht."""
    from hub.contracts.naming import COMMAND, DOMAINS, EVENT, NAME_PATTERN, check_name, domain_of

    def contract_like(value: str) -> bool:
        # Andere publish()-Aufrufe (etwa Redis-Kanäle wie "mandari:sync:trigger") sind keine Ereignisse.
        return re.fullmatch(NAME_PATTERN, value) is not None and domain_of(value) in DOMAINS

    tree = ast.parse(source)
    module_version = _module_version(tree)
    found: list[Reference] = []
    calls: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str) and contract_like(first.value)):
            continue
        name = _call_name(node)
        if name == "publish":
            version_node = next((k.value for k in node.keywords if k.arg == "version"), None)
            found.append(Reference(path, first.lineno, first.value, EVENT, _int_constant(version_node, module_version)))
            calls.add(id(first))
        elif name == "command_handler":
            given = (
                node.args[1]
                if len(node.args) > 1
                else next((k.value for k in node.keywords if k.arg == "version"), None)
            )
            version = 1 if given is None else _int_constant(given, module_version)
            found.append(Reference(path, first.lineno, first.value, COMMAND, version))
            calls.add(id(first))
    for node in ast.walk(tree):
        if id(node) in calls or not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not check_name(node.value, EVENT):
            found.append(Reference(path, node.lineno, node.value, EVENT, module_version))
    return sorted(found, key=lambda ref: (ref.line, ref.name))


def reference_problems(refs: Iterable[Reference], registry: Any) -> list[str]:
    """Namen ohne Schema, mit falscher Art oder ohne die genannte Version."""
    from hub.contracts import UnknownContractError

    problems: list[str] = []
    for ref in refs:
        where = f"{ref.path}:{ref.line}"
        art = "Ereignis" if ref.kind == "event" else "Befehl"
        try:
            versions = registry.versions(ref.name)
        except UnknownContractError:
            problems.append(f"{where}: {art} {ref.name} hat kein Schema im Register")
            continue
        if registry.latest(ref.name).kind != ref.kind:
            problems.append(f"{where}: {ref.name} ist im Register kein {art}")
        elif ref.version is not None and ref.version not in versions:
            problems.append(f"{where}: {ref.name} v{ref.version} hat kein Schema (vorhanden: {versions})")
    return problems


def code_references(roots: Iterable[Path] = SOURCES) -> list[Reference]:
    refs: list[Reference] = []
    for path in python_files(roots):
        refs += references(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
    return refs


# --- 2. Nur additive Änderungen ---------------------------------------------------------------------


def _key(relative: str) -> str | None:
    """``…/schemas/<typ>/v<n>.json`` → ``<typ>/v<n>``, ``…/envelope/v<n>.json`` → ``envelope/v<n>``."""
    parts = relative.split("/")
    if len(parts) < 2 or not parts[-1].endswith(".json"):
        return None
    return f"{parts[-2]}/{parts[-1].removesuffix('.json')}"


def contracts_on_disk(root: Path = ROOT) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for base in CONTRACT_PATHS:
        for path in sorted((root / base).rglob("*.json")):
            key = _key(path.relative_to(root).as_posix())
            if key:
                found[key] = json.loads(path.read_text(encoding="utf-8"))
    return found


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout


def contracts_at(ref: str) -> dict[str, Any]:
    """Schemas und Hülle im Stand ``ref`` (Commit, Zweig); ``CalledProcessError``, wenn es ihn nicht gibt."""
    found: dict[str, Any] = {}
    for relative in _git("ls-tree", "-r", "--name-only", ref, "--", *CONTRACT_PATHS).splitlines():
        key = _key(relative)
        if key:
            found[key] = json.loads(_git("show", f"{ref}:{relative}"))
    return found


# --- 3. Katalog -------------------------------------------------------------------------------------


def rendered_catalog() -> str:
    from hub.contracts import envelope_schema, get_registry
    from hub.contracts.catalog import render_catalog

    return render_catalog(get_registry(), envelope_schema())


def catalog_current(path: Path = CATALOG) -> bool:
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8").replace("\r\n", "\n") == rendered_catalog()


# --- Ablauf -----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if str(PROJECT) not in sys.path:
        sys.path.insert(0, str(PROJECT))
    parser = argparse.ArgumentParser(description="Ereignisverträge prüfen (Issue #519).")
    parser.add_argument("--base", help="Stand der Zielverzweigung für den Vergleich auf additive Änderungen")
    parser.add_argument("--write-catalog", action="store_true", help="docs/EREIGNISKATALOG.md neu schreiben")
    args = parser.parse_args(argv)

    from hub.contracts import ContractError, get_registry
    from hub.contracts.compatibility import contract_changes

    try:
        registry = get_registry()
    except ContractError as exc:
        print("Register ungültig:")
        for problem in exc.problems:
            print(f"  ✗ {problem}")
        return 1

    if args.write_catalog:
        CATALOG.write_text(rendered_catalog(), encoding="utf-8", newline="\n")
        print(f"Katalog geschrieben: {CATALOG.relative_to(ROOT).as_posix()} ({len(registry)} Verträge)")
        return 0

    problems: list[str] = []
    refs = code_references()
    problems += reference_problems(refs, registry)
    print(f"1. Schema je Ereignis im Code: {len(refs)} Nennungen geprüft")

    if args.base:
        try:
            old = contracts_at(args.base)
        except subprocess.CalledProcessError:
            print(f"Stand {args.base} nicht gefunden (vorher git fetch?).")
            return 2
        changes = contract_changes(old, contracts_on_disk())
        problems += [f"brechende Änderung {change}" for change in changes]
        print(f"2. Nur additive Änderungen gegenüber {args.base}: {len(old)} Schemas verglichen")
    else:
        print("2. Vergleich auf additive Änderungen übersprungen (kein --base)")

    if not catalog_current():
        problems.append(
            "docs/EREIGNISKATALOG.md ist nicht aktuell: python scripts/check_event_contracts.py --write-catalog"
        )
    print("3. Ereigniskatalog geprüft")

    if problems:
        print(f"\n{len(problems)} Befund(e):")
        for problem in problems:
            print(f"  ✗ {problem}")
        print(
            "\nBrechende Änderungen brauchen eine neue Version (v<n+1>.json neben der alten), "
            "siehe docs/adr/20260929-ereignisvertraege.md."
        )
        return 1
    print("\nEreignisverträge in Ordnung.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

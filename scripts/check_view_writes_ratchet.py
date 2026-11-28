# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für ORM-Schreibzugriffe in Views (Befehlsschicht, Issue #1588, Kennzahl K9).

Views lesen nur ihr Formular und rufen Befehle auf; geschrieben wird beim Eigentümer
(``docs/adr/20261128-api-first-ohne-ausnahme.md``). Dieses Skript zählt je View-Modul die Aufrufe, die über das ORM
schreiben, statisch am Syntaxbaum und ohne Django zu laden. Die Baseline ``scripts/view_writes_baseline.json`` hält
je Datei den erlaubten Höchststand fest. Das Skript schlägt fehl, wenn

- eine Datei mehr Schreibzugriffe hat als in der Baseline oder eine neue Datei welche bekommt,
- Schreibzugriffe abgebaut sind, die Baseline aber noch den alten Stand hat: mit ``--update`` nachziehen und im
  selben Pull Request committen, damit der niedrigere Stand festgeschrieben ist.

``--update`` senkt nur: Es übernimmt kleinere Werte und streicht Dateien ohne Schreibzugriffe, nimmt aber nie eine
Datei auf und erhöht nie einen Wert. ``--json`` gibt den aktuellen Stand aus.

**View-Module** (Pfade relativ zu ``mandari/``): ``apps/<app>/views*.py``, ``apps/<app>/<paket>/views*.py``,
``insight_core/views*.py`` und alles in Paketen ``views/`` an diesen Stellen. Tests und Migrationen zählen nie.

**Zählregeln** (wie K1 in ``docs/DATENDREHSCHEIBE.md``, Abschnitt 5.3, aber ohne Blick auf den Eigentümer):

- ``create``, ``get_or_create``, ``update_or_create``, ``save`` und ``delete`` (auch asynchron) zählen an jedem
  Empfänger, sofern der Aufruf keine Positionsargumente hat: Das ORM nimmt dort nur Schlüsselwörter, Positionen
  haben ``cache.delete(schluessel)``, ``speicher.delete(name)`` oder ``mappe.save(antwort)``. ``form.save()`` eines
  Model-Formulars zählt, ``form.save(commit=False)`` nicht (es schreibt erst das folgende ``save()``).
- ``bulk_create`` und ``bulk_update`` zählen an jedem Empfänger.
- ``update`` zählt nur an einer Kette mit Manager oder Queryset (``Modell.objects…update(…)``,
  ``….filter(…).update(…)``) oder an einer Variable, die dieselbe Funktion aus einer solchen Kette bildet;
  ``kontext.update(…)`` an einem Wörterbuch zählt nicht.
- Nie: Aufrufe an einem importierten Modul oder einer Funktion in Kleinbuchstaben (``dienst.create(…)`` ist ein
  Dienstaufruf), ``request.session.save()``, Mehrfachbeziehungen (``add``, ``remove``, ``set``, ``clear``) und rohes
  SQL. Schreibzugriffe in Diensten, die eine View aufruft, zählt K9 nicht; sie verschwinden, wenn die View auf einen
  Befehl umzieht, dessen Handler den Dienst nutzt.

Aufruf aus dem Repo-Root:  python scripts/check_view_writes_ratchet.py [--update] [--json]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "mandari"
BASELINE = ROOT / "scripts" / "view_writes_baseline.json"

#: Verzeichnisse, die nie mitzählen.
SKIPPED_DIRS = frozenset({"migrations", "tests", "tests_e2e", "node_modules", "static", "staticfiles", "__pycache__"})
MANAGERS = frozenset({"objects", "_default_manager", "_base_manager"})
#: Methoden, die aus einem Manager oder Queryset ein Queryset machen
QUERYSET_METHODS = frozenset(
    {
        "all",
        "filter",
        "exclude",
        "select_related",
        "prefetch_related",
        "select_for_update",
        "order_by",
        "annotate",
        "alias",
        "distinct",
        "only",
        "defer",
        "using",
        "none",
    }
)
#: Schreibende Methoden ohne Positionsargumente im ORM (siehe Moduldokumentation)
_KEYWORD_ONLY = ("create", "get_or_create", "update_or_create", "save", "delete")
KEYWORD_ONLY = frozenset(_KEYWORD_ONLY) | {f"a{name}" for name in _KEYWORD_ONLY}
BULK = frozenset({"bulk_create", "bulk_update", "abulk_create", "abulk_update"})
UPDATE = frozenset({"update", "aupdate"})
#: Empfänger, die nie ins ORM schreiben: die Sitzung der Anfrage
NOT_ORM = frozenset({"session"})
_VIEW_MODULE = re.compile(r"^views(_[a-z0-9_]+)?\.py$")


def is_view_module(relative: PurePosixPath) -> bool:
    """Ist die Datei (Pfad relativ zu ``mandari/``) ein View-Modul? Siehe Moduldokumentation."""
    parts = relative.parts
    if not parts or relative.suffix != ".py":
        return False
    if any(part in SKIPPED_DIRS or part.startswith(".") for part in parts[:-1]):
        return False
    name = parts[-1]
    if name.startswith("test_") or name == "conftest.py":
        return False
    if parts[0] == "insight_core":
        anchors = [1]
    elif parts[0] == "apps" and len(parts) >= 3:
        anchors = [2, 3]
    else:
        return False
    for anchor in anchors:
        if len(parts) == anchor + 1 and _VIEW_MODULE.match(name):
            return True
        if len(parts) > anchor + 1 and parts[anchor] == "views":
            return True
    return False


def _scopes(tree: ast.Module) -> Iterator[list[ast.AST]]:
    """Knoten je Funktion (innere Funktionen eigens) und der Rest des Moduls."""

    def walk(node: ast.AST, bucket: list[ast.AST]) -> Iterator[list[ast.AST]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                inner: list[ast.AST] = []
                yield from walk(child, inner)
                yield inner
            else:
                bucket.append(child)
                yield from walk(child, bucket)

    module: list[ast.AST] = []
    yield from walk(tree, module)
    yield module


def _lowercase_imports(tree: ast.Module) -> set[str]:
    """Lokale Namen importierter Module und Funktionen (Kleinbuchstaben am Anfang, also keine Modellklassen)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {alias.asname or alias.name for alias in node.names}
    return {name for name in names if name[:1].islower()}


def _is_queryset_chain(node: ast.expr, querysets: set[str]) -> bool:
    """Hängt der Ausdruck an einem Manager, einem Queryset-Aufruf oder einer Variable aus einer solchen Kette?"""
    current: ast.expr = node
    while True:
        if isinstance(current, ast.Attribute):
            if current.attr in MANAGERS:
                return True
            current = current.value
        elif isinstance(current, ast.Call):
            if isinstance(current.func, ast.Attribute) and current.func.attr in QUERYSET_METHODS:
                return True
            current = current.func
        elif isinstance(current, ast.Subscript | ast.Await | ast.Starred):
            current = current.value
        elif isinstance(current, ast.Name):
            return current.id in querysets
        else:
            return False


def _querysets(scope: list[ast.AST]) -> set[str]:
    """Variablen, die der Geltungsbereich aus einer Queryset-Kette bildet (``qs = Modell.objects.filter(…)``)."""
    found: set[str] = set()
    for node in scope:
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if _is_queryset_chain(node.value, found):
                found |= {target.id for target in targets if isinstance(target, ast.Name)}
    return found


def _counts(call: ast.Call, querysets: set[str], modules: set[str]) -> bool:
    if not isinstance(call.func, ast.Attribute):
        return False
    method, receiver = call.func.attr, call.func.value
    if isinstance(receiver, ast.Name) and receiver.id in modules:
        return False
    if isinstance(receiver, ast.Attribute) and receiver.attr in NOT_ORM:
        return False
    if method in BULK:
        return True
    if method in KEYWORD_ONLY:
        return not call.args and not _without_commit(call)
    if method in UPDATE:
        return _is_queryset_chain(receiver, querysets)
    return False


def _without_commit(call: ast.Call) -> bool:
    """``form.save(commit=False)`` legt nur das Objekt an und schreibt nicht."""
    return any(
        keyword.arg == "commit" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        for keyword in call.keywords
    )


def count_writes(source: str) -> int:
    """ORM-Schreibzugriffe in einem Modul (siehe Moduldokumentation)."""
    tree = ast.parse(source)
    modules = _lowercase_imports(tree)
    total = 0
    for scope in _scopes(tree):
        querysets = _querysets(scope)
        total += sum(1 for node in scope if isinstance(node, ast.Call) and _counts(node, querysets, modules))
    return total


def measure(project: Path | None = None) -> dict[str, int]:
    """Schreibzugriffe je View-Modul (nur Module mit mindestens einem), sortiert nach Pfad."""
    project = project or PROJECT
    result: dict[str, int] = {}
    for path in sorted(project.rglob("*.py")):
        relative = PurePosixPath(path.relative_to(project).as_posix())
        if not is_view_module(relative):
            continue
        count = count_writes(path.read_text(encoding="utf-8"))
        if count:
            result[relative.as_posix()] = count
    return result


def compare(current: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    """(Verstöße, abgebaute Schreibzugriffe ohne nachgezogene Baseline)."""
    violations: list[str] = []
    lowered: list[str] = []
    for name, count in current.items():
        allowed = baseline.get(name)
        if allowed is None:
            violations.append(f"{name}: {count} neue Schreibzugriffe (Datei steht nicht in der Baseline)")
        elif count > allowed:
            violations.append(f"{name}: {count} Schreibzugriffe, erlaubt {allowed}")
    for name, allowed in baseline.items():
        count = current.get(name, 0)
        if count < allowed:
            lowered.append(f"{name}: {count} statt {allowed}")
    return violations, lowered


def lowered_baseline(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """Neue Baseline: je Datei der kleinere Wert, Dateien ohne Schreibzugriffe entfallen, nichts kommt hinzu."""
    return {name: min(allowed, current.get(name, 0)) for name, allowed in baseline.items() if current.get(name, 0)}


def load_baseline(path: Path | None = None) -> dict[str, int]:
    data = json.loads((path or BASELINE).read_text(encoding="utf-8"))
    return {str(name): int(count) for name, count in data["files"].items()}


def write_baseline(files: dict[str, int], path: Path | None = None) -> None:
    data = {
        "beschreibung": (
            "ORM-Schreibzugriffe in Views je Datei; dürfen nur sinken. Neue Änderungen entstehen als Befehl beim "
            "Eigentümer (scripts/check_view_writes_ratchet.py, Issue #1588, Kennzahl K9)."
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
    print(f"ORM-Schreibzugriffe in Views: {total} (Baseline {allowed_total})")
    if violations:
        print(
            "\nNeue Schreibzugriffe in Views. Änderungen gehören in einen Befehl beim Eigentümer; die View ruft ihn"
            " mit ausfuehren() auf (Anleitung: docs/DATENDREHSCHEIBE.md, Abschnitt 6):"
        )
        print("\n".join(f"- {line}" for line in violations))
        return 1
    if lowered:
        print("\nSchreibzugriffe abgebaut, Baseline noch auf altem Stand. Bitte festschreiben:")
        print("  python scripts/check_view_writes_ratchet.py --update  (und die Baseline mitcommitten)")
        print("\n".join(f"- {line}" for line in lowered))
        return 1
    print("OK: keine neuen Schreibzugriffe in Views.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

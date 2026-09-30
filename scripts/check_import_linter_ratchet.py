# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ratchet für die Schichtregeln: Die import-linter-Verträge dürfen nur strenger werden.

Die Verträge stehen in ``mandari/pyproject.toml`` (``[tool.importlinter]``, siehe
``docs/adr/20260929-schichtenmodell.md``); ob der Code sie einhält, prüft ``lint-imports`` aus
``mandari/``. Dieses Skript wacht über die Verträge selbst. Die Baseline
(``scripts/import_linter_baseline.json``) hält den Stand fest: die Einstellungen (``root_packages``
usw.), den Inhalt der drei Pflichtverträge (Schichten, Module, Quell- und verbotene Module sowie
alle Optionen) und ihre Ausnahmelisten (``ignore_imports``). Das Skript schlägt fehl, wenn

- einer der drei Verträge fehlt oder eine andere Art hat,
- eine Ausnahme dazukommt, die nicht in der Baseline steht,
- eine Ausnahme Platzhalter nutzt (einzige erlaubte: die Freistellung der Tests) oder doppelt steht,
- ein Paket keiner Schicht zugeordnet ist (neue App, neues Wurzelpaket), womit es ungeprüft bliebe,
- ein Vertrag oder eine Einstellung gelockert oder umgebaut ist: ein Modul fehlt in ``modules``,
  ``source_modules``, ``forbidden_modules``, ``root_packages`` oder in den Schichten, Schichten sind
  umsortiert oder ein Paket ist in eine andere Schicht gewandert, eine Schicht erlaubt gegenseitige
  Importe („:“ statt „|“), ``allow_indirect_imports`` ist eingeschaltet,
  ``unmatched_ignore_imports_alerting`` gesenkt, oder eine sonstige Option ist geändert,
- die Verträge strenger geworden oder Ausnahmen abgebaut sind, die Baseline aber noch den alten
  Stand hat: mit ``--update`` nachziehen und im selben Pull Request committen, damit der Stand
  festgeschrieben ist.

``--update`` schreibt nur Verschärfungen fest; es nimmt nie Ausnahmen auf und lockert nie einen
Vertrag. Eine bewusste Lockerung oder ein Umbau (z. B. ein Paket wechselt die Schicht) geht nur über
eine Änderung der Baseline von Hand, die im Review sichtbar ist.

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

#: Schlüssel eines Vertrags, die nicht zum geschützten Inhalt gehören (Kennung, Anzeige, Art, Ausnahmen).
_NOT_CONTENT = {"id", "name", "type", "ignore_imports"}
#: Listen, bei denen jeder zusätzliche Eintrag die Prüfung verschärft und jeder fehlende sie lockert.
GROWING_LISTS = {"root_packages", "modules", "source_modules", "forbidden_modules"}
#: Schalter, die eingeschaltet die Prüfung lockern (Vorgabe: aus).
LOOSENING_FLAGS = {"allow_indirect_imports", "exclude_type_checking_imports"}
#: Schalter, die eingeschaltet die Prüfung verschärfen (Vorgabe: aus).
TIGHTENING_FLAGS = {"exhaustive"}
#: Stufen, aufsteigend strenger, mit der Vorgabe von import-linter.
LEVELS = {"unmatched_ignore_imports_alerting": (("none", "warn", "error"), "error")}

_HAND_EDIT = "bewusste Änderung nur über die Baseline von Hand, im Review sichtbar"


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


def _parse_layers(layers: object) -> list[tuple[bool, dict[str, bool]]]:
    """Schichten von oben nach unten: (unabhängig mit „|“, {Paket: optional})."""
    parsed = []
    for layer in layers if isinstance(layers, list) else []:
        text = str(layer)
        modules: dict[str, bool] = {}
        for part in text.replace("|", ":").split(":"):
            name = part.strip()
            optional = name.startswith("(") and name.endswith(")")
            modules[name.strip("()").strip()] = optional
        parsed.append(("|" in text, modules))
    return parsed


def _layer_modules(linter: dict[str, Any]) -> set[str]:
    modules: set[str] = set()
    for contract in linter.get("contracts", []):
        if contract.get("id") == "schichten":
            for _, layer in _parse_layers(contract.get("layers", [])):
                modules |= set(layer)
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


# --- Inhalt der Verträge und Einstellungen --------------------------------------------------------


def content(linter: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Geschützter Stand: Einstellungen und Inhalt der Pflichtverträge, jeweils ohne Ausnahmen."""
    settings = {key: value for key, value in linter.items() if key != "contracts"}
    contracts = {
        str(contract.get("id")): {key: value for key, value in contract.items() if key not in _NOT_CONTENT}
        for contract in linter.get("contracts", [])
        if contract.get("id") in CONTRACTS
    }
    return settings, contracts


def snapshot(linter: dict[str, Any]) -> dict[str, Any]:
    """Baseline aus dem aktuellen Stand."""
    settings, contracts = content(linter)
    current, _ = exceptions(linter)
    return {"einstellungen": settings, "vertraege": contracts, "ausnahmen": current}


def content_changes(linter: dict[str, Any], baseline: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Lockerungen bzw. Umbauten (verboten) und Verschärfungen (Baseline nachziehen)."""
    settings, contracts = content(linter)
    loosened, tightened = _compare_mapping("Einstellungen", settings, baseline.get("einstellungen", {}))
    before_contracts = baseline.get("vertraege", {})
    for contract_id in CONTRACTS:
        if contract_id not in contracts:
            continue  # meldet contract_problems
        loose, tight = _compare_mapping(contract_id, contracts[contract_id], before_contracts.get(contract_id, {}))
        loosened += loose
        tightened += tight
    return loosened, tightened


def _compare_mapping(scope: str, now: dict[str, Any], before: dict[str, Any]) -> tuple[list[str], list[str]]:
    loosened: list[str] = []
    tightened: list[str] = []
    for key in sorted(set(now) | set(before)):
        loose, tight = _compare_value(f"{scope}: {key}", key, before.get(key), now.get(key))
        loosened += loose
        tightened += tight
    return loosened, tightened


def _compare_value(label: str, key: str, before: Any, now: Any) -> tuple[list[str], list[str]]:
    if before == now:
        return [], []
    if key == "layers":
        return _compare_layers(label, before, now)
    if key in GROWING_LISTS and isinstance(before or [], list) and isinstance(now or [], list):
        old, new = {str(item) for item in before or []}, {str(item) for item in now or []}
        return (
            [f"{label}: „{item}“ entfernt" for item in sorted(old - new)],
            [f"{label}: „{item}“ ergänzt" for item in sorted(new - old)],
        )
    if key in LOOSENING_FLAGS or key in TIGHTENING_FLAGS:
        old_on, new_on = _flag(before), _flag(now)
        if old_on == new_on:
            return [], []
        change = f"{label}: {'eingeschaltet' if new_on else 'ausgeschaltet'}"
        stricter = new_on == (key in TIGHTENING_FLAGS)
        return ([], [change]) if stricter else ([change], [])
    if key in LEVELS:
        order, default = LEVELS[key]
        old_level, new_level = str(before or default), str(now or default)
        if old_level in order and new_level in order:
            if old_level == new_level:
                return [], []
            change = f"{label}: {old_level} → {new_level}"
            return ([], [change]) if order.index(new_level) > order.index(old_level) else ([change], [])
    return [f"{label}: geändert ({_HAND_EDIT})"], []


def _flag(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _compare_layers(label: str, before: Any, now: Any) -> tuple[list[str], list[str]]:
    """Neue Pakete in den Schichten verschärfen; Wegfall, Umsortieren und Verschieben lockern."""
    old, new = _parse_layers(before), _parse_layers(now)
    old_modules = {module for _, layer in old for module in layer}
    new_modules = {module for _, layer in new for module in layer}
    loosened = [f"{label}: „{module}“ keiner Schicht mehr zugeordnet" for module in sorted(old_modules - new_modules)]
    tightened = [f"{label}: „{module}“ neu zugeordnet" for module in sorted(new_modules - old_modules)]

    common = old_modules & new_modules
    old_kept = [(independent, layer) for independent, layer in old if set(layer) & common]
    new_kept = [(independent, layer) for independent, layer in new if set(layer) & common]
    if [set(layer) & common for _, layer in old_kept] != [set(layer) & common for _, layer in new_kept]:
        loosened.append(f"{label}: Schichten umsortiert oder Pakete verschoben ({_HAND_EDIT})")
        return loosened, tightened
    for (old_independent, old_layer), (new_independent, new_layer) in zip(old_kept, new_kept, strict=True):
        name = " / ".join(sorted(set(old_layer) & common))
        if old_independent != new_independent:
            change = f"{label}: Schicht {name} {'mit „|“ (unabhängig)' if new_independent else 'mit „:“ statt „|“'}"
            (tightened if new_independent else loosened).append(change)
        for module in sorted(set(old_layer) & common):
            if old_layer[module] != new_layer[module]:
                optional = new_layer[module]
                change = f"{label}: „{module}“ {'optional' if optional else 'nicht mehr optional'}"
                (loosened if optional else tightened).append(change)
    return loosened, tightened


# --- Ablauf -----------------------------------------------------------------------------------------


def _print(title: str, entries: list[str], mark: str) -> None:
    if entries:
        print(f"\n{title}")
        print("\n".join(f"  {mark} {entry}" for entry in entries))


def main(argv: list[str]) -> int:
    update = "--update" in argv
    linter = load_config()
    problems = contract_problems(linter) + coverage_problems(linter)
    current, form_problems = exceptions(linter)
    problems += form_problems
    if not BASELINE.exists():
        print(f"Baseline {BASELINE.relative_to(ROOT)} fehlt.")
        return 1
    baseline: dict[str, Any] = json.loads(BASELINE.read_text(encoding="utf-8"))
    added, removed = compare(current, baseline.get("ausnahmen", {}))
    loosened, tightened = content_changes(linter, baseline)

    for contract_id in CONTRACTS:
        count, before = len(current.get(contract_id, [])), len(baseline.get("ausnahmen", {}).get(contract_id, []))
        print(f"{contract_id:20s} {count:4d} Ausnahmen (Baseline {before})")
    _print("Schichtregeln unvollständig oder Ausnahmeliste unzulässig:", problems, "-")
    _print("Neue Ausnahmen sind nicht erlaubt (Abhängigkeit über Ereignis, Befehl oder Fassade lösen):", added, "+")
    _print("Verträge oder Einstellungen gelockert oder umgebaut (nicht erlaubt):", loosened, "!")
    if problems or added or loosened:
        return 1
    if removed or tightened:
        if update:
            BASELINE.write_text(json.dumps(snapshot(linter), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"\nBaseline nachgezogen: {len(removed)} Ausnahmen gestrichen, {len(tightened)} Verschärfungen.")
            return 0
        print("\nBaseline noch nicht nachgezogen (mit --update, im selben PR committen):")
        print("\n".join([f"  - {entry}" for entry in removed] + [f"  ^ {entry}" for entry in tightened]))
        return 1
    print("\nOK: keine neuen Ausnahmen, Verträge unverändert.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

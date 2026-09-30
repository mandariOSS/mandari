# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Register der Verträge für Ereignisse und Befehle (``docs/adr/20260929-ereignisvertraege.md``).

Die Schemas liegen unter ``schemas/<typ>/v<n>.json``; das Register lädt sie einmal, prüft jedes
gegen die Vertragsregeln (``rules.py``) und liefert das Schema je Typ und Version. Ein Fehler in
einem Schema verhindert das Laden ganz: Ein halbes Register gibt es nicht.

    from hub.contracts import get_registry

    registry = get_registry()
    registry.schema("ris.paper.released", 1)      # JSON Schema der Nutzlast (Kopie)
    registry.validate_event(envelope)              # Hülle, Typ, Sichtbarkeit und Nutzlast
    registry.validate_command("submission.submit", 1, body)
"""

from __future__ import annotations

import copy
import functools
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from .envelope import Envelope
from .naming import COMMAND, EVENT, Kind
from .rules import document_problems, parse_visibility
from .validation import ContractViolationError, envelope_problems, instance_problems, validator_for

SCHEMA_ROOT: Final = Path(__file__).resolve().parent / "schemas"

_VERSION_FILE = re.compile(r"^v([1-9][0-9]{0,4})\.json$")


class ContractError(Exception):
    """Mindestens ein Schema verletzt die Vertragsregeln; ``problems`` nennt alle Verstöße."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("Verträge ungültig:\n" + "\n".join(f"- {problem}" for problem in self.problems))


class UnknownContractError(LookupError):
    """Für diesen Typ und diese Version gibt es kein Schema im Register."""

    def __init__(self, name: str, version: int | None = None) -> None:
        self.name = name
        self.version = version
        suffix = f" v{version}" if version is not None else ""
        super().__init__(f"kein Vertrag für {name}{suffix}")


@dataclass(frozen=True)
class Contract:
    """Ein Vertrag: Typ, Version und Schema der Nutzlast samt Eigentümer und Sichtbarkeit."""

    name: str
    version: int
    kind: Kind
    owner: str
    visibility: frozenset[str]
    title: str
    description: str
    _schema: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def schema(self) -> dict[str, Any]:
        """JSON Schema der Nutzlast als eigenständige Kopie."""
        return copy.deepcopy(dict(self._schema))

    @property
    def examples(self) -> list[Any]:
        return copy.deepcopy(list(self._schema.get("examples", [])))

    @classmethod
    def from_document(cls, name: str, version: int, document: Mapping[str, Any]) -> Contract:
        """Vertrag aus einem Schema; wirft ``ContractError``, wenn es die Regeln verletzt."""
        problems = document_problems(name, version, document)
        if problems:
            raise ContractError(f"{name} v{version}: {problem}" for problem in problems)
        visibility, _ = parse_visibility(document["x-visibility"])
        return cls(
            name=name,
            version=version,
            kind=cast(Kind, document["x-kind"]),
            owner=str(document["x-owner"]),
            visibility=visibility,
            title=str(document["title"]),
            description=str(document["description"]),
            _schema=copy.deepcopy(dict(document)),
        )


class Registry:
    """Unveränderliches Register; Schlüssel ist (Typ, Version)."""

    def __init__(self, contracts: Iterable[Contract] = ()) -> None:
        self._contracts: dict[tuple[str, int], Contract] = {}
        problems: list[str] = []
        for contract in contracts:
            key = (contract.name, contract.version)
            if key in self._contracts:
                problems.append(f"{contract.name} v{contract.version}: doppelt im Register")
            self._contracts[key] = contract
        kinds: dict[str, set[str]] = {}
        for contract in self._contracts.values():
            kinds.setdefault(contract.name, set()).add(contract.kind)
        problems += [f"{name}: Versionen sind teils Ereignis, teils Befehl" for name, k in kinds.items() if len(k) > 1]
        if problems:
            raise ContractError(problems)
        self._validators: dict[tuple[str, int], Any] = {}

    # --- Abfragen ---------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._contracts)

    def __iter__(self) -> Iterator[Contract]:
        return iter(self.contracts())

    def __contains__(self, key: object) -> bool:
        return key in self._contracts

    def contracts(self, kind: Kind | None = None) -> tuple[Contract, ...]:
        """Alle Verträge, sortiert nach Typ und Version; wahlweise nur Ereignisse oder Befehle."""
        found = (c for c in self._contracts.values() if kind is None or c.kind == kind)
        return tuple(sorted(found, key=lambda c: (c.name, c.version)))

    def names(self, kind: Kind | None = None) -> tuple[str, ...]:
        return tuple(sorted({c.name for c in self.contracts(kind)}))

    def versions(self, name: str) -> tuple[int, ...]:
        found = tuple(sorted(version for (n, version) in self._contracts if n == name))
        if not found:
            raise UnknownContractError(name)
        return found

    def get(self, name: str, version: int) -> Contract:
        try:
            return self._contracts[(name, version)]
        except KeyError:
            raise UnknownContractError(name, version) from None

    def latest(self, name: str) -> Contract:
        return self.get(name, self.versions(name)[-1])

    def schema(self, name: str, version: int) -> dict[str, Any]:
        """JSON Schema der Nutzlast für Typ und Version (Kopie); ``UnknownContractError``, wenn es fehlt."""
        return self.get(name, version).schema

    # --- Prüfen -----------------------------------------------------------------------------

    def payload_problems(self, name: str, version: int, payload: object) -> list[str]:
        key = (name, version)
        validator = self._validators.get(key)
        if validator is None:
            validator = self._validators[key] = validator_for(self.get(name, version).schema)
        return instance_problems(validator, payload)

    def validate_payload(self, name: str, version: int, payload: object) -> None:
        problems = self.payload_problems(name, version, payload)
        if problems:
            raise ContractViolationError(f"{name} v{version}", problems)

    def validate_event(self, event: Envelope | Mapping[str, Any]) -> None:
        """Prüft Hülle, Typ (Ereignis im Register), Sichtbarkeit und Nutzlast eines Ereignisses."""
        data = event.to_dict() if isinstance(event, Envelope) else event
        problems = envelope_problems(data)
        if problems:
            raise ContractViolationError("Ereignishülle", problems)
        name, version = str(data["type"]), int(data["version"])
        contract = self.get(name, version)
        if contract.kind != EVENT:
            raise ContractViolationError(f"{name} v{version}", ["ist ein Befehl, kein Ereignis"])
        if data["visibility"] not in contract.visibility:
            allowed = ", ".join(sorted(contract.visibility))
            raise ContractViolationError(
                f"{name} v{version}", [f"Sichtbarkeit {data['visibility']} ist nicht vorgesehen (erlaubt: {allowed})"]
            )
        self.validate_payload(name, version, data["payload"])

    def validate_command(self, name: str, version: int, body: object) -> None:
        """Prüft den Inhalt eines Befehls gegen sein Schema."""
        if self.get(name, version).kind != COMMAND:
            raise ContractViolationError(f"{name} v{version}", ["ist ein Ereignis, kein Befehl"])
        self.validate_payload(name, version, body)


def load_registry(root: Path = SCHEMA_ROOT) -> Registry:
    """Lädt alle Schemas unter ``root``; wirft ``ContractError`` mit allen Verstößen."""
    contracts: list[Contract] = []
    problems: list[str] = []
    for path in sorted(root.rglob("*.json")) if root.is_dir() else []:
        relative = path.relative_to(root).as_posix()
        parts = relative.split("/")
        match = _VERSION_FILE.fullmatch(parts[-1])
        if len(parts) != 2 or match is None:
            problems.append(f"{relative}: Ablage muss schemas/<typ>/v<n>.json sein")
            continue
        name, version = parts[0], int(match.group(1))
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            problems.append(f"{relative}: kein lesbares JSON")
            continue
        try:
            contracts.append(Contract.from_document(name, version, document))
        except ContractError as exc:
            problems += exc.problems
    if problems:
        raise ContractError(problems)
    return Registry(contracts)


@functools.cache
def get_registry() -> Registry:
    """Register der ausgelieferten Schemas (einmal je Prozess geladen)."""
    return load_registry(SCHEMA_ROOT)

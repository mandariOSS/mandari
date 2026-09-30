# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfung von Daten gegen JSON Schema 2020-12.

Fehlermeldungen nennen nur die Stelle (JSON-Pfad), die verletzte Regel und Feldnamen aus dem
Schema, nie Werte: Nutzlasten können personenbezogen sein und landen sonst in Logs oder
Fehlermeldungen.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from .envelope import ENVELOPE_VERSION, envelope_schema_text

#: Dialekt aller Verträge.
SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"

_MAX_NAMES = 5


class ContractViolationError(ValueError):
    """Daten entsprechen nicht dem Vertrag; ``problems`` enthält je Verstoß eine Zeile ohne Werte."""

    def __init__(self, subject: str, problems: Sequence[str]) -> None:
        self.subject = subject
        self.problems = tuple(problems)
        super().__init__(f"{subject}: " + "; ".join(self.problems))


def validator_for(schema: Mapping[str, Any]) -> Any:
    """Validator für ein bereits geprüftes Schema, mit Prüfung der Formate (``uuid``, ``date`` …)."""
    from jsonschema import Draft202012Validator

    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def schema_problems(schema: object) -> list[str]:
    """Verstöße eines Schemas gegen das Metaschema von JSON Schema 2020-12."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"kein gültiges JSON Schema 2020-12 ({_describe(exc)})"]
    return []


def instance_problems(validator: Any, instance: object) -> list[str]:
    """Verstöße einer Instanz, sortiert nach Pfad; Meldungen ohne Werte."""
    errors = sorted(validator.iter_errors(instance), key=lambda error: str(error.json_path))
    return [_describe(error) for error in errors]


def _describe(error: Any) -> str:
    path = str(error.json_path)
    keyword = str(error.validator)
    instance = error.instance
    if keyword == "required" and isinstance(instance, Mapping):
        missing = [name for name in error.validator_value if name not in instance]
        return f"{path}: Pflichtfeld fehlt ({_names(missing)})"
    if keyword == "additionalProperties" and isinstance(instance, Mapping):
        schema = error.schema if isinstance(error.schema, Mapping) else {}
        known = set(schema.get("properties", {}))
        patterns = list(schema.get("patternProperties", {}))
        extra = [
            str(name)
            for name in instance
            if name not in known and not any(re.search(pattern, str(name)) for pattern in patterns)
        ]
        return f"{path}: nicht vorgesehene Felder ({_names(extra)})"
    return f"{path}: verletzt „{keyword}“"


def _names(names: Sequence[str]) -> str:
    shown = [name[:40] for name in sorted(names)[:_MAX_NAMES]]
    rest = len(names) - len(shown)
    return ", ".join(shown) + (f" und {rest} weitere" if rest > 0 else "")


@functools.cache
def _envelope_validator(version: int) -> Any:
    return validator_for(json.loads(envelope_schema_text(version)))


def envelope_problems(data: object, version: int = ENVELOPE_VERSION) -> list[str]:
    return instance_problems(_envelope_validator(version), data)


def validate_envelope(data: object, version: int = ENVELOPE_VERSION) -> None:
    """Prüft die JSON-Darstellung einer Hülle; wirft ``ContractViolationError``."""
    problems = envelope_problems(data, version)
    if problems:
        raise ContractViolationError("Ereignishülle", problems)

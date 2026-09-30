# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Prüfung von Daten gegen JSON Schema 2020-12.

Fehlermeldungen nennen nur die Stelle (JSON-Pfad), die verletzte Regel und Feldnamen, nie Werte:
Nutzlasten können personenbezogen sein und landen sonst in Logs oder Fehlermeldungen. Feldnamen aus
der geprüften Nutzlast (im Pfad und bei nicht vorgesehenen Feldern) erscheinen nur, wenn sie wie ein
Feldname aussehen (Kleinbuchstaben, Ziffern, Unterstrich, höchstens 40 Zeichen); sonst steht dort
``<Feld>``, denn bei frei wählbaren Schlüsseln kann schon der Name ein Inhalt sein.

Formate prüft ein eigener Prüfer: ``uuid``, ``date``, ``date-time``, ``time`` und ``duration``
unabhängig von optionalen Paketen (``formats.py``), alle übrigen wie ``jsonschema``.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final

from .envelope import ENVELOPE_VERSION, envelope_schema_text
from .formats import FORMAT_CHECKS

#: Dialekt aller Verträge.
SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"

_MAX_NAMES = 5
#: Schlüssel aus einer Nutzlast, die in Meldungen erscheinen dürfen; alle anderen werden zu ``<Feld>``.
_SAFE_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
_HIDDEN_KEY = "<Feld>"


class ContractViolationError(ValueError):
    """Daten entsprechen nicht dem Vertrag; ``problems`` enthält je Verstoß eine Zeile ohne Werte."""

    def __init__(self, subject: str, problems: Sequence[str]) -> None:
        self.subject = subject
        self.problems = tuple(problems)
        super().__init__(f"{subject}: " + "; ".join(self.problems))


def validator_for(schema: Mapping[str, Any]) -> Any:
    """Validator für ein bereits geprüftes Schema, mit Prüfung der Formate (``uuid``, ``date`` …)."""
    from jsonschema import Draft202012Validator

    return Draft202012Validator(schema, format_checker=format_checker())


@functools.cache
def format_checker() -> Any:
    """Formatprüfer von ``jsonschema``; die Prüfungen aus ``formats.py`` ersetzen die gleichnamigen."""
    from jsonschema import Draft202012Validator, FormatChecker

    checker = FormatChecker(formats=())
    checker.checkers.update(Draft202012Validator.FORMAT_CHECKER.checkers)
    for name, check in FORMAT_CHECKS.items():
        checker.checks(name)(check)
    return checker


def schema_problems(schema: object) -> list[str]:
    """Verstöße eines Schemas gegen das Metaschema von JSON Schema 2020-12."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        # Hier ist das Schema selbst die Instanz: Pfad und Namen stammen aus dem Schema.
        return [f"kein gültiges JSON Schema 2020-12 ({_describe(exc, from_schema=True)})"]
    return []


def instance_problems(validator: Any, instance: object) -> list[str]:
    """Verstöße einer Instanz, sortiert nach Pfad; Meldungen ohne Werte."""
    return sorted(_describe(error) for error in validator.iter_errors(instance))


def _describe(error: Any, *, from_schema: bool = False) -> str:
    """Stelle und Regel eines Verstoßes; Schlüssel der Instanz nur, wenn sie unbedenklich sind."""
    show = _schema_key if from_schema else _shown_key
    path = _json_path(error.absolute_path, show)
    keyword = str(error.validator)
    instance = error.instance
    if keyword == "required" and isinstance(instance, Mapping):
        # Die fehlenden Namen stammen aus ``required`` im Schema.
        missing = [name for name in error.validator_value if name not in instance]
        return f"{path}: Pflichtfeld fehlt ({_names(missing)})"
    if keyword == "additionalProperties" and isinstance(instance, Mapping):
        schema = error.schema if isinstance(error.schema, Mapping) else {}
        known = set(schema.get("properties", {}))
        patterns = list(schema.get("patternProperties", {}))
        extra = [
            show(str(name))
            for name in instance
            if name not in known and not any(re.search(pattern, str(name)) for pattern in patterns)
        ]
        return f"{path}: nicht vorgesehene Felder ({_names(extra)})"
    return f"{path}: verletzt „{keyword}“"


def _shown_key(key: str) -> str:
    return key if _SAFE_KEY.fullmatch(key) else _HIDDEN_KEY


def _schema_key(key: str) -> str:
    return key


def _json_path(parts: Iterable[object], show: Callable[[str], str]) -> str:
    """JSON-Pfad wie ``ValidationError.json_path``, Schlüssel über ``show`` gefiltert."""
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{show(str(part))}"
    return path


def _names(names: Sequence[str]) -> str:
    distinct = sorted(set(names))
    shown = [name[:40] for name in distinct[:_MAX_NAMES]]
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

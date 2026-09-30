# SPDX-License-Identifier: AGPL-3.0-or-later
"""Systemprüfung: Hülle und alle Schemas halten die Vertragsregeln ein (``manage.py check``)."""

from __future__ import annotations

from typing import Any

from django.core import checks

from .envelope import envelope_schema
from .registry import SCHEMA_ROOT, ContractError, load_registry
from .validation import instance_problems, schema_problems, validator_for


@checks.register("hub")
def check_contracts(app_configs: Any = None, **kwargs: Any) -> list[checks.CheckMessage]:
    meldungen: list[checks.CheckMessage] = []
    schema = envelope_schema()
    problems = schema_problems(schema)
    if not problems:
        validator = validator_for(schema)
        for index, example in enumerate(schema.get("examples", [])):
            problems += [f"examples[{index}] {problem}" for problem in instance_problems(validator, example)]
    meldungen += [checks.Error(f"Ereignishülle: {problem}", id="hub_contracts.E001") for problem in problems]
    try:
        load_registry(SCHEMA_ROOT)
    except ContractError as exc:
        meldungen += [checks.Error(problem, id="hub_contracts.E002") for problem in exc.problems]
    return meldungen

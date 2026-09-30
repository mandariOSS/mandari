# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bausteine für Tests des Vertragsregisters: gültige Schemas und ihre Ablage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PAPER_ID = "5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c"
TENANT_REF = "session:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"


def ereignis_schema(name: str = "ris.paper.released", version: int = 1, /, **abweichend: Any) -> dict[str, Any]:
    """Gültiges Ereignisschema (auch für nichtöffentliche Vorlagen, daher ohne Freitext)."""
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:mandari:event:{name}:v{version}",
        "title": "Vorlage freigegeben",
        "description": "Eine Vorlage wurde freigegeben bzw. veröffentlicht.",
        "x-kind": "event",
        "x-owner": "apps.session",
        "x-visibility": ["oeffentlich", "nichtoeffentlich"],
        "type": "object",
        "additionalProperties": False,
        "required": ["paper", "changed"],
        "properties": {
            "paper": {"type": "string", "format": "uuid"},
            "changed": {"type": "array", "items": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}},
        },
        "examples": [{"paper": PAPER_ID, "changed": ["status"]}],
    }
    schema.update(abweichend)
    return schema


def befehl_schema(name: str = "submission.submit", version: int = 1, /, **abweichend: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:mandari:command:{name}:v{version}",
        "title": "Antrag einreichen",
        "description": "Eine Organisation reicht einen Antrag bei der Verwaltung ein.",
        "x-kind": "command",
        "x-owner": "apps.session",
        "x-visibility": "nichtoeffentlich",
        "type": "object",
        "additionalProperties": False,
        "required": ["document"],
        "properties": {"document": {"type": "string", "format": "uuid"}},
        "examples": [{"document": PAPER_ID}],
    }
    schema.update(abweichend)
    return schema


def ablegen(wurzel: Path, name: str, version: int, schema: object) -> Path:
    """Schreibt ein Schema nach ``<wurzel>/<name>/v<version>.json``."""
    pfad = wurzel / name / f"v{version}.json"
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    return pfad


def huelle(**abweichend: Any) -> dict[str, Any]:
    """JSON-Darstellung einer gültigen Hülle für ``ris.paper.released`` v1."""
    daten: dict[str, Any] = {
        "event_id": "0f4c6f8e-6a1b-4d8e-9c55-2f1a7b3c9d10",
        "type": "ris.paper.released",
        "version": 1,
        "aggregate_type": "Paper",
        "aggregate_id": PAPER_ID,
        "tenant_ref": TENANT_REF,
        "body_id": None,
        "visibility": "oeffentlich",
        "operation": "upsert",
        "occurred_at": "2026-09-30T10:15:00+02:00",
        "actor_ref": "system:ingestor",
        "correlation_id": "2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d",
        "causation_id": None,
        "payload": {"paper": PAPER_ID, "changed": ["status"]},
    }
    daten.update(abweichend)
    return daten

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schema-Contract Django ↔ Ingestor (Issue #161).

Der erste Test hält den Contract fest; die weiteren beweisen, dass absichtliche
Abweichungen erkannt werden (sonst wäre das Gate wertlos).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from insight_core.schema_contract import ColumnSpec, SchemaSpec, compare, django_schema, sqlalchemy_schema

pytest.importorskip("sqlalchemy")

INGESTOR_DIR = Path(__file__).resolve().parents[3] / "ingestor"


@pytest.fixture(scope="module")
def schemas() -> tuple[SchemaSpec, SchemaSpec]:
    return django_schema(), sqlalchemy_schema(INGESTOR_DIR)


def test_contract_holds(schemas: tuple[SchemaSpec, SchemaSpec]) -> None:
    django, ingestor = schemas
    report = compare(django, ingestor)
    assert report.compared_tables, "Keine gemeinsamen Tabellen gefunden"
    assert "oparl_meetings" in report.compared_tables
    assert report.ok, "\n".join(str(f) for f in report.errors)


def _mutated(schema: SchemaSpec, table: str, column: str, **changes: object) -> SchemaSpec:
    copy: SchemaSpec = {t: dict(cols) for t, cols in schema.items()}
    copy[table][column] = replace(copy[table][column], **changes)  # type: ignore[arg-type]
    return copy


def test_detects_django_field_removed(schemas: tuple[SchemaSpec, SchemaSpec]) -> None:
    django, ingestor = schemas
    broken: SchemaSpec = {t: dict(cols) for t, cols in django.items()}
    del broken["oparl_meetings"]["meeting_state"]
    report = compare(broken, ingestor)
    assert any(f.column == "meeting_state" and f.level == "error" for f in report.errors)


def test_detects_django_not_null_column_unknown_to_ingestor(schemas: tuple[SchemaSpec, SchemaSpec]) -> None:
    django, ingestor = schemas
    broken: SchemaSpec = {t: dict(cols) for t, cols in django.items()}
    broken["oparl_meetings"]["neue_pflichtspalte"] = ColumnSpec(
        "neue_pflichtspalte", "string", nullable=False, length=50
    )
    report = compare(broken, ingestor)
    assert any(f.column == "neue_pflichtspalte" and f.level == "error" for f in report.errors)


def test_detects_ingestor_widening_length_and_nullability(schemas: tuple[SchemaSpec, SchemaSpec]) -> None:
    django, ingestor = schemas
    # Ingestor erlaubt 1000 Zeichen, Django nur 500 → Fehler
    broken = _mutated(ingestor, "oparl_meetings", "name", length=1000)
    report = compare(django, broken)
    assert any(f.column == "name" and "Länge" in f.message and f.level == "error" for f in report.errors)

    # Ingestor erlaubt NULL, Django nicht → Fehler
    broken = _mutated(ingestor, "oparl_meetings", "cancelled", nullable=True)
    report = compare(django, broken)
    assert any(f.column == "cancelled" and "NULL" in f.message and f.level == "error" for f in report.errors)


def test_detects_type_family_change(schemas: tuple[SchemaSpec, SchemaSpec]) -> None:
    django, ingestor = schemas
    broken = _mutated(ingestor, "oparl_meetings", "raw_json", kind="text")
    report = compare(django, broken)
    assert any(f.column == "raw_json" and "Typfamilie" in f.message and f.level == "error" for f in report.errors)


def test_compatible_families_only_warn(schemas: tuple[SchemaSpec, SchemaSpec]) -> None:
    django, ingestor = schemas
    softened = _mutated(ingestor, "oparl_meetings", "location_address", kind="string", length=None)
    report = compare(django, softened)
    assert not any(f.column == "location_address" for f in report.errors)
    assert any(f.column == "location_address" for f in report.warnings)

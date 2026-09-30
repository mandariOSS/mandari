# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Schema-Contract Django ↔ Ingestor (Issue #161).

Der erste Test hält den Contract fest; die weiteren beweisen, dass absichtliche
Abweichungen erkannt werden (sonst wäre das Gate wertlos).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.db import connection

from apps.events.models import Event, Operation
from insight_core.schema_contract import (
    ColumnSpec,
    SchemaSpec,
    compare,
    django_schema,
    elasticsearch_contract,
    ingestor_index_names,
    ingestor_models,
    sqlalchemy_schema,
)

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


# ---------------------------------------------------------------------------
# Journal der Ereignistechnik (Issue #513): Der Ingestor schreibt Ereignisse, vergibt aber keine
# Folgenummer
# ---------------------------------------------------------------------------


def test_journal_gehoert_zum_vertrag(schemas: tuple[SchemaSpec, SchemaSpec]) -> None:
    django, ingestor = schemas
    assert "events_event" in compare(django, ingestor).compared_tables
    assert set(ingestor["events_event"]) <= set(django["events_event"])


def test_ingestor_vergibt_keine_folgenummer(schemas: tuple[SchemaSpec, SchemaSpec]) -> None:
    """``seq`` vergibt der Sequenzierer nach dem Commit, ``xid`` und ``recorded_at`` setzt die Datenbank."""
    django, ingestor = schemas
    for spalte in ("seq", "xid", "recorded_at"):
        assert spalte in django["events_event"]
        assert spalte not in ingestor["events_event"]
    # Was der Ingestor nicht schreibt, darf seine INSERTs nicht scheitern lassen.
    assert django["events_event"]["seq"].nullable
    assert django["events_event"]["xid"].has_db_default
    assert django["events_event"]["recorded_at"].has_db_default


def test_detects_new_required_journal_column(schemas: tuple[SchemaSpec, SchemaSpec]) -> None:
    django, ingestor = schemas
    broken: SchemaSpec = {t: dict(cols) for t, cols in django.items()}
    broken["events_event"]["neue_pflichtspalte"] = ColumnSpec("neue_pflichtspalte", "text", nullable=False)
    report = compare(broken, ingestor)
    assert any(f.table == "events_event" and f.column == "neue_pflichtspalte" for f in report.errors)


@pytest.mark.django_db(transaction=True)
def test_ingestor_insert_gegen_das_journal_aus_den_migrationen() -> None:
    """
    Die Anweisung des Ingestors (``ingestor/src/storage/events.py``: ein INSERT mit mehreren Zeilen auf
    seiner Tabellenbeschreibung) gegen die Tabelle, die die Django-Migrationen anlegen: Standards,
    Transaktionskennung und Prüfbedingungen kommen aus der Datenbank, die Folgenummer bleibt leer.
    """
    if connection.vendor != "postgresql":
        pytest.skip("braucht PostgreSQL (CI); der Ingestor schreibt nur dorthin")
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool

    tabelle = ingestor_models(INGESTOR_DIR).JournalEvent.__table__
    einstellungen = connection.settings_dict
    url = sa.URL.create(
        "postgresql+psycopg",
        username=einstellungen["USER"],
        password=einstellungen["PASSWORD"],
        host=einstellungen["HOST"] or None,
        port=int(einstellungen["PORT"]) if einstellungen["PORT"] else None,
        database=einstellungen["NAME"],
    )
    quelle = f"source:{uuid.uuid4()}"
    zeit = datetime(2026, 9, 30, 8, 15, tzinfo=UTC)
    zeilen = [
        {
            "event_id": uuid.uuid4(),
            "type": typ,
            "version": 1,
            "aggregate_type": "Paper",
            "aggregate_id": uuid.uuid4(),
            "tenant_ref": quelle,
            "body_id": uuid.uuid4(),
            "visibility": "oeffentlich",
            "operation": operation,
            "occurred_at": zeit,
            "actor_ref": "system:ingestor",
            "correlation_id": uuid.uuid4(),
            "causation_id": None,
            "payload": {"changed": ["name"]},
        }
        for typ, operation in (("ris.paper.changed", "upsert"), ("ris.object.depublished", "delete"))
    ]
    engine = sa.create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as verbindung:
            verbindung.execute(sa.insert(tabelle).values(zeilen))
            # ohne Angabe der Operation gilt der Standard der Datenbank
            ohne_operation = {k: v for k, v in zeilen[0].items() if k != "operation"} | {"event_id": uuid.uuid4()}
            verbindung.execute(sa.insert(tabelle).values(ohne_operation))
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as verbindung:
            verbindung.execute(
                sa.insert(tabelle).values(zeilen[0] | {"event_id": uuid.uuid4(), "visibility": "geheim"})
            )
    finally:
        engine.dispose()

    ereignisse = list(Event.objects.filter(tenant_ref=quelle).order_by("id"))
    assert [e.type for e in ereignisse] == ["ris.paper.changed", "ris.object.depublished", "ris.paper.changed"]
    assert [e.operation for e in ereignisse] == [Operation.UPSERT, Operation.DELETE, Operation.UPSERT]
    assert {e.event_id for e in ereignisse[:2]} == {zeile["event_id"] for zeile in zeilen}
    assert ereignisse[0].occurred_at == zeit
    assert ereignisse[0].payload == {"changed": ["name"]}
    # Der Ingestor vergibt keine Folgenummer; Transaktion und Erfassungszeit setzt die Datenbank.
    assert {e.seq for e in ereignisse} == {None}
    assert len({e.xid for e in ereignisse}) == 1 and ereignisse[0].xid > 0
    assert all(e.recorded_at is not None for e in ereignisse)


# ---------------------------------------------------------------------------
# Elasticsearch-Indizes (Issue #215): Django legt an, der Ingestor schreibt nur
# ---------------------------------------------------------------------------


def _django_index_names() -> set[str]:
    from insight_search.management.commands.setup_elasticsearch import Command

    return set(Command()._get_index_configs([]).keys())


def test_elasticsearch_contract_holds() -> None:
    assert elasticsearch_contract(INGESTOR_DIR, _django_index_names()) == []


def test_ingestor_knows_exactly_the_django_indices() -> None:
    assert set(ingestor_index_names(INGESTOR_DIR)) == _django_index_names()


def _fake_ingestor(tmp_path: Path, quelle: str) -> Path:
    datei = tmp_path / "src" / "indexing" / "elasticsearch.py"
    datei.parent.mkdir(parents=True)
    datei.write_text(quelle, encoding="utf-8")
    return tmp_path


def test_detects_unknown_index_and_own_mapping(tmp_path: Path) -> None:
    fake = _fake_ingestor(
        tmp_path,
        'INDEX_NAMES = ("papers", "geheim")\nMAPPING = {"name": {"type": "text", "analyzer": "german"}}\n',
    )
    probleme = elasticsearch_contract(fake, {"papers", "meetings"})
    assert any("'geheim'" in p for p in probleme)
    assert any("'meetings'" in p for p in probleme)
    assert any('"analyzer"' in p for p in probleme)


def test_detects_missing_index_names_constant(tmp_path: Path) -> None:
    fake = _fake_ingestor(tmp_path, "INDICES = ()\n")
    probleme = elasticsearch_contract(fake, {"papers"})
    assert len(probleme) == 1 and "INDEX_NAMES" in probleme[0]

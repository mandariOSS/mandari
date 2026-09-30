# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``src/storage/events.py`` (Issue #513): Hülle, Tabellenbeschreibung und Anweisung, ohne Datenbank.

Das Zusammenspiel mit PostgreSQL (eine Transaktion mit der Datenänderung) prüft
``test_events_journal.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings
from src.storage import events
from src.storage.models import JournalEvent

REPO = Path(__file__).resolve().parents[2]
ENVELOPE = REPO / "mandari" / "hub" / "contracts" / "envelope" / "v1.json"
TENANT = "source:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"
PAPER = uuid.UUID("5b2d7c1e-8f3a-5e9b-a4c6-1d2e3f4a5b6c")


def _ereignis(**abweichend: Any) -> events.NewEvent:
    werte: dict[str, Any] = {
        "type": "ris.paper.changed",
        "aggregate_type": "Paper",
        "aggregate_id": PAPER,
        "tenant_ref": TENANT,
        "visibility": "oeffentlich",
        "payload": {"paper": str(PAPER), "changed": ["name"]},
    }
    werte.update(abweichend)
    return events.NewEvent(**werte)


# --- Schalter ----------------------------------------------------------------------------------------


def test_ereignisse_sind_standardmaessig_aus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INGESTOR_EVENTS_ENABLED", raising=False)
    assert Settings(_env_file=None).events_enabled is False


@pytest.mark.parametrize(("wert", "erwartet"), [("true", True), ("1", True), ("false", False), ("0", False)])
def test_schalter_ueber_die_umgebung(monkeypatch: pytest.MonkeyPatch, wert: str, erwartet: bool) -> None:
    monkeypatch.setenv("INGESTOR_EVENTS_ENABLED", wert)
    assert Settings(_env_file=None).events_enabled is erwartet


# --- Tabelle: der Ingestor vergibt keine Folgenummer ---------------------------------------------------


def test_ingestor_kennt_weder_folgenummer_noch_transaktionskennung() -> None:
    """``seq`` vergibt der Sequenzierer, ``xid`` und ``recorded_at`` die Datenbank."""
    spalten = set(JournalEvent.__table__.columns.keys())
    assert spalten.isdisjoint({"seq", "xid", "recorded_at"})
    assert spalten == {
        "id",
        "event_id",
        "type",
        "version",
        "aggregate_type",
        "aggregate_id",
        "tenant_ref",
        "body_id",
        "visibility",
        "operation",
        "occurred_at",
        "actor_ref",
        "correlation_id",
        "causation_id",
        "payload",
    }


def test_insert_schreibt_nur_die_huelle_in_einer_anweisung() -> None:
    zeilen = [events.event_row(_ereignis()) for _ in range(3)]
    sql = str(insert(JournalEvent.__table__).values(zeilen).compile(dialect=postgresql.dialect()))
    assert sql.count("INSERT INTO events_event") == 1
    kopf = sql.split("VALUES")[0]
    for spalte in ("seq", "xid", "recorded_at", " id"):
        assert spalte not in kopf
    assert sql.count("(%(event_id") == 3


# --- Hülle -------------------------------------------------------------------------------------------


def test_zeile_mit_standards() -> None:
    vorher = datetime.now(UTC)
    zeile = events.event_row(_ereignis())
    assert zeile["type"] == "ris.paper.changed"
    assert (zeile["version"], zeile["operation"], zeile["actor_ref"]) == (1, "upsert", "system:ingestor")
    assert zeile["tenant_ref"] == TENANT
    assert zeile["body_id"] is None and zeile["causation_id"] is None
    assert isinstance(zeile["event_id"], uuid.UUID) and isinstance(zeile["correlation_id"], uuid.UUID)
    assert vorher <= zeile["occurred_at"] <= datetime.now(UTC)
    assert zeile["payload"] == {"paper": str(PAPER), "changed": ["name"]}
    assert set(zeile) == set(JournalEvent.__table__.columns.keys()) - {"id"}


def test_muster_entsprechen_der_ereignishuelle() -> None:
    """Der Ingestor prüft die Hülle ohne das Schema zu lesen: gleiche Muster, gleiche Grenzen."""
    eigenschaften = json.loads(ENVELOPE.read_text(encoding="utf-8"))["properties"]
    assert eigenschaften["type"]["pattern"] == events.EVENT_TYPE_PATTERN
    assert eigenschaften["type"]["maxLength"] == events.MAX_EVENT_TYPE_LENGTH
    assert eigenschaften["version"]["maximum"] == events.MAX_VERSION
    assert eigenschaften["aggregate_type"]["pattern"] == events.AGGREGATE_TYPE_PATTERN
    assert eigenschaften["aggregate_type"]["maxLength"] == events.MAX_AGGREGATE_TYPE_LENGTH
    assert eigenschaften["tenant_ref"]["pattern"] == events.TENANT_REF_PATTERN
    assert eigenschaften["actor_ref"]["anyOf"][0]["pattern"] == events.ACTOR_REF_PATTERN
    assert tuple(eigenschaften["visibility"]["enum"]) == events.VISIBILITIES
    assert tuple(eigenschaften["operation"]["enum"]) == events.OPERATIONS
    # Spalten des Ingestors = Felder der Hülle (id ist der technische Schlüssel der Tabelle)
    assert set(eigenschaften) == set(JournalEvent.__table__.columns.keys()) - {"id"}


@pytest.mark.parametrize(
    ("abweichung", "feld"),
    [
        ({"type": "Ris.Paper"}, "type"),
        ({"type": "ris"}, "type"),
        ({"type": "ris.paper.changed.v2"}, "type"),
        ({"version": 0}, "version"),
        ({"version": True}, "version"),
        ({"aggregate_type": "paper"}, "aggregate_type"),
        ({"aggregate_id": str(PAPER)}, "aggregate_id"),
        ({"tenant_ref": "source:muenster"}, "tenant_ref"),
        ({"tenant_ref": "Stadt Musterstadt"}, "tenant_ref"),
        ({"visibility": "geheim"}, "visibility"),
        ({"operation": "update"}, "operation"),
        ({"actor_ref": "Erika Mustermann"}, "actor_ref"),
        ({"actor_ref": "user:erika@example.org"}, "actor_ref"),
        ({"occurred_at": datetime(2026, 9, 30, 8, 15)}, "occurred_at"),
        ({"payload": ["name"]}, "payload"),
        ({"body_id": "Musterstadt"}, "body_id"),
        ({"correlation_id": "keine-uuid"}, "correlation_id"),
    ],
)
def test_ungueltige_huelle_wird_abgelehnt(abweichung: dict[str, Any], feld: str) -> None:
    with pytest.raises(events.InvalidEventError, match=f"^{feld}:") as info:
        events.event_row(_ereignis(**abweichung))
    # Meldungen nennen Feld und Regel, nie den Wert
    for wert in abweichung.values():
        if isinstance(wert, str):
            assert wert not in str(info.value)


# --- Korrelation -------------------------------------------------------------------------------------


def test_ereignisse_eines_vorgangs_tragen_dieselbe_korrelation() -> None:
    vorgang = events.start_correlation()
    assert events.current_correlation_id() == vorgang
    erste, zweite = events.event_row(_ereignis()), events.event_row(_ereignis())
    assert erste["correlation_id"] == zweite["correlation_id"] == vorgang
    eigene = uuid.uuid4()
    assert events.event_row(_ereignis(correlation_id=eigene))["correlation_id"] == eigene
    assert events.start_correlation() != vorgang


# --- Transaktion -------------------------------------------------------------------------------------


async def test_publish_ohne_laufende_transaktion_wirft() -> None:
    """Das Ereignis gehört in die Transaktion der Datenänderung, nicht in eine eigene Sitzung."""
    async with AsyncSession() as session:
        assert not session.in_transaction()
        with pytest.raises(events.PublishOutsideTransactionError, match="Transaktion"):
            await events.publish(
                session,
                "ris.paper.changed",
                aggregate_type="Paper",
                aggregate_id=PAPER,
                tenant_ref=TENANT,
                visibility="oeffentlich",
                payload={"paper": str(PAPER), "changed": ["name"]},
            )
        with pytest.raises(events.PublishOutsideTransactionError):
            await events.publish_many(session, [_ereignis()])


async def test_keine_ereignisse_keine_anweisung() -> None:
    async with AsyncSession() as session:
        assert await events.publish_many(session, []) == []

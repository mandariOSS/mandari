# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse aus dem Ingestor ins Journal der Ereignistechnik (``events_event``).

Das Journal ist eine transaktionale Outbox (``docs/adr/20260929-ereignistechnik-postgres.md``):
Ein Ereignis entsteht in derselben Transaktion wie die Datenänderung. In Django übernimmt das
``apps.events.publish()``; der Ingestor schreibt über SQLAlchemy in dieselbe Tabelle:

    async with storage.get_session() as session:
        await session.execute(upsert)                      # Datenänderung
        await publish(session, "ris.paper.changed", ...)   # Ereignis, gleiche Transaktion
        await session.commit()                             # beides oder nichts

- ``publish()`` und ``publish_many()`` schreiben nur in die laufende Transaktion der übergebenen
  Sitzung und schließen sie nie ab. Ohne laufende Transaktion werfen sie
  ``PublishOutsideTransactionError``: Das Ereignis gehört hinter die Datenänderung, nicht davor und
  nicht in eine eigene Sitzung.
- Der Ingestor vergibt keine Folgenummer. ``seq`` vergibt der Sequenzierer nach dem Commit, ``xid``
  und ``recorded_at`` setzt die Datenbank; die Tabellenbeschreibung des Ingestors
  (``JournalEvent``) kennt diese Spalten nicht.
- Die Hülle wird wie in Django am Format geprüft (Typ, Version, Objekttyp, Mandant, Sichtbarkeit,
  Operation, Auslöser, Zeitzone). Meldungen nennen Feld und Regel, nie den Wert. Ob die Nutzlast
  zum Vertrag passt, prüfen die Vertragstests (``mandari/hub/contracts/tests/test_ingestor_events.py``).
- Mehrere Ereignisse einer Transaktion gehen als eine Anweisung in die Datenbank.

Welche Ereignisse aus einer Änderung am RIS-Bestand entstehen, steht in ``ris_events.py``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, cast

from sqlalchemy import Table, insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.storage.models import JournalEvent

#: Auslöser aller Ereignisse des Ingestors (``actor_ref`` der Hülle).
ACTOR_REF: Final = "system:ingestor"

VISIBILITIES: Final = ("oeffentlich", "nichtoeffentlich", "intern", "personenbezogen")
OPERATIONS: Final = ("upsert", "delete", "redact")

_UUID: Final = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Gleichlautend mit der Ereignishülle (mandari/hub/contracts/envelope/v1.json); tests/test_events.py vergleicht.
EVENT_TYPE_PATTERN: Final = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*){1,2}$"
AGGREGATE_TYPE_PATTERN: Final = r"^[A-Z][A-Za-z0-9]*$"
TENANT_REF_PATTERN: Final = rf"^(session|org|source):{_UUID}$"
ACTOR_REF_PATTERN: Final = rf"^(user:{_UUID}|system:[a-z][a-z0-9_.-]{{0,63}})$"
MAX_EVENT_TYPE_LENGTH: Final = 100
MAX_AGGREGATE_TYPE_LENGTH: Final = 64
MAX_VERSION: Final = 32767

_EVENT_TYPE = re.compile(EVENT_TYPE_PATTERN)
_AGGREGATE_TYPE = re.compile(AGGREGATE_TYPE_PATTERN)
_TENANT_REF = re.compile(TENANT_REF_PATTERN)
_ACTOR_REF = re.compile(ACTOR_REF_PATTERN)

#: Tabelle des Journals, wie der Ingestor sie kennt (ohne seq, xid und recorded_at).
JOURNAL: Final = cast(Table, JournalEvent.__table__)

#: So viele Ereignisse gehen höchstens in eine INSERT-Anweisung (Grenze der Bind-Parameter).
INSERT_CHUNK: Final = 500


class PublishOutsideTransactionError(RuntimeError):
    """``publish()`` wurde ohne laufende Transaktion der Sitzung aufgerufen."""

    def __init__(self) -> None:
        super().__init__(
            "publish() braucht die laufende Transaktion der Datenänderung: erst die Änderung ausführen, "
            "dann in derselben Sitzung veröffentlichen, dann festschreiben."
        )


class InvalidEventError(ValueError):
    """Die Angaben zur Hülle sind ungültig; die Meldung nennt das Feld und die Regel, nie den Wert."""


@dataclass(frozen=True, kw_only=True)
class NewEvent:
    """Ein Ereignis mit vollständiger Hülle; Kennung und Zeitpunkt werden vergeben, wenn sie fehlen."""

    type: str
    aggregate_type: str
    aggregate_id: uuid.UUID
    tenant_ref: str
    visibility: str
    payload: Mapping[str, Any]
    version: int = 1
    body_id: uuid.UUID | None = None
    operation: str = "upsert"
    occurred_at: datetime | None = None
    actor_ref: str | None = ACTOR_REF
    correlation_id: uuid.UUID | None = None
    causation_id: uuid.UUID | None = None
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)


# --- Korrelation ---------------------------------------------------------------------------------

_correlation_id: ContextVar[uuid.UUID | None] = ContextVar("ingestor_events_correlation_id", default=None)


def start_correlation() -> uuid.UUID:
    """
    Beginnt einen Vorgang, z. B. den Abgleich einer Kommune: Alle Ereignisse ab hier tragen dieselbe
    Korrelations-ID. Gilt für die laufende asyncio-Aufgabe und alle, die sie danach startet.
    """
    correlation_id = uuid.uuid4()
    _correlation_id.set(correlation_id)
    return correlation_id


def current_correlation_id() -> uuid.UUID | None:
    return _correlation_id.get()


# --- Veröffentlichen -----------------------------------------------------------------------------


async def publish(
    session: AsyncSession,
    event_type: str,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    tenant_ref: str,
    visibility: str,
    payload: Mapping[str, Any],
    version: int = 1,
    body_id: uuid.UUID | None = None,
    operation: str = "upsert",
    occurred_at: datetime | None = None,
    actor_ref: str | None = ACTOR_REF,
    correlation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Schreibt ein Ereignis in die laufende Transaktion der Sitzung und gibt seine ``event_id`` zurück."""
    event = NewEvent(
        type=event_type,
        version=version,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        tenant_ref=tenant_ref,
        body_id=body_id,
        visibility=visibility,
        operation=operation,
        occurred_at=occurred_at,
        actor_ref=actor_ref,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload,
    )
    return (await publish_many(session, [event]))[0]


async def publish_many(session: AsyncSession, events: Sequence[NewEvent]) -> list[uuid.UUID]:
    """Schreibt mehrere Ereignisse mit einer Anweisung in die laufende Transaktion der Sitzung."""
    if not events:
        return []
    if not session.in_transaction():
        raise PublishOutsideTransactionError()
    rows = [event_row(event) for event in events]
    for start in range(0, len(rows), INSERT_CHUNK):
        await session.execute(insert(JOURNAL).values(rows[start : start + INSERT_CHUNK]))
    return [row["event_id"] for row in rows]


def event_row(event: NewEvent) -> dict[str, Any]:
    """Spaltenwerte eines Ereignisses; prüft die Hülle am Format."""
    occurred_at = event.occurred_at or datetime.now(UTC)
    if (
        not isinstance(event.type, str)
        or len(event.type) > MAX_EVENT_TYPE_LENGTH
        or not _EVENT_TYPE.fullmatch(event.type)
    ):
        raise InvalidEventError("type: erwartet <bereich>.<objekt>.<ereignis> in Kleinbuchstaben")
    if isinstance(event.version, bool) or not isinstance(event.version, int) or not 1 <= event.version <= MAX_VERSION:
        raise InvalidEventError(f"version: ganze Zahl von 1 bis {MAX_VERSION} erwartet")
    if (
        not isinstance(event.aggregate_type, str)
        or len(event.aggregate_type) > MAX_AGGREGATE_TYPE_LENGTH
        or not _AGGREGATE_TYPE.fullmatch(event.aggregate_type)
    ):
        raise InvalidEventError("aggregate_type: kanonischer Typ wie Paper erwartet")
    if not isinstance(event.tenant_ref, str) or not _TENANT_REF.fullmatch(event.tenant_ref):
        raise InvalidEventError("tenant_ref: erwartet session:<uuid>, org:<uuid> oder source:<uuid>")
    if event.visibility not in VISIBILITIES:
        raise InvalidEventError("visibility: unbekannte Sichtbarkeit")
    if event.operation not in OPERATIONS:
        raise InvalidEventError("operation: erwartet upsert, delete oder redact")
    if event.actor_ref is not None and (
        not isinstance(event.actor_ref, str) or not _ACTOR_REF.fullmatch(event.actor_ref)
    ):
        raise InvalidEventError("actor_ref: erwartet user:<uuid> oder system:<auftrag>, nie einen Namen")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise InvalidEventError("occurred_at: Zeitpunkt braucht eine Zeitzone")
    if not isinstance(event.payload, Mapping):
        raise InvalidEventError("payload: Objekt erwartet")
    for name in ("aggregate_id", "event_id"):
        if not isinstance(getattr(event, name), uuid.UUID):
            raise InvalidEventError(f"{name}: UUID erwartet")
    for name in ("body_id", "correlation_id", "causation_id"):
        value = getattr(event, name)
        if value is not None and not isinstance(value, uuid.UUID):
            raise InvalidEventError(f"{name}: UUID erwartet")
    return {
        "event_id": event.event_id,
        "type": event.type,
        "version": event.version,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "tenant_ref": event.tenant_ref,
        "body_id": event.body_id,
        "visibility": event.visibility,
        "operation": event.operation,
        "occurred_at": occurred_at,
        "actor_ref": event.actor_ref,
        "correlation_id": event.correlation_id or _correlation_id.get() or uuid.uuid4(),
        "causation_id": event.causation_id,
        "payload": dict(event.payload),
    }

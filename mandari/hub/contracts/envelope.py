# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignishülle (Envelope) nach ``docs/adr/20260929-ereignistechnik-postgres.md``.

Die Hülle ist selbst ein Vertrag: ``envelope/v1.json`` (JSON Schema 2020-12). ``Envelope`` ist ihre
Python-Form; ``to_dict()`` liefert die JSON-Darstellung, die gegen das Schema geprüft wird. Die
Felder entsprechen den Spalten des Journals ``events_event``; ``xid``, ``seq`` und ``recorded_at``
vergibt die Datenbank bzw. der Sequenzierer und gehören nicht zur Hülle.
"""

from __future__ import annotations

import copy
import functools
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

#: Aktuelle Version der Hülle.
ENVELOPE_VERSION: Final = 1
ENVELOPE_DIR: Final = Path(__file__).resolve().parent / "envelope"

#: Sichtbarkeitsklassen (gleichlautend mit ``apps.events.models.Visibility``).
VISIBILITIES: Final[tuple[str, ...]] = ("oeffentlich", "nichtoeffentlich", "intern", "personenbezogen")
#: Klassen, deren Schemas keine Freitextfelder enthalten dürfen (``docs/adr/20260929-ereignisvertraege.md``).
RESTRICTED_VISIBILITIES: Final[frozenset[str]] = frozenset({"nichtoeffentlich", "personenbezogen"})
#: Operationen im Änderungsfeed (gleichlautend mit ``apps.events.models.Operation``).
OPERATIONS: Final[tuple[str, ...]] = ("upsert", "delete", "redact")

#: Präfixe von ``tenant_ref`` und ``actor_ref``.
TENANT_KINDS: Final[tuple[str, ...]] = ("session", "org", "source")


@functools.cache
def envelope_schema_text(version: int) -> str:
    return (ENVELOPE_DIR / f"v{version}.json").read_text(encoding="utf-8")


def envelope_schema(version: int = ENVELOPE_VERSION) -> dict[str, Any]:
    """Schema der Hülle als eigenständige Kopie (Änderungen wirken nicht zurück)."""
    return cast(dict[str, Any], json.loads(envelope_schema_text(version)))


@dataclass(frozen=True, slots=True, kw_only=True)
class Envelope:
    """Ein Ereignis mit Hülle; Kennungen werden beim Anlegen vergeben, wenn sie fehlen."""

    type: str
    version: int
    aggregate_type: str
    aggregate_id: uuid.UUID
    tenant_ref: str
    visibility: str
    payload: Mapping[str, Any]
    operation: str = "upsert"
    body_id: uuid.UUID | None = None
    actor_ref: str | None = None
    causation_id: uuid.UUID | None = None
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    correlation_id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at braucht eine Zeitzone")
        # Eigene Kopie der Nutzlast: Die Hülle bleibt unveränderlich, auch wenn der Aufrufer sein
        # Wörterbuch danach weiterverwendet.
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))

    def to_dict(self) -> dict[str, Any]:
        """JSON-Darstellung nach ``envelope/v1.json``."""
        return {
            "event_id": str(self.event_id),
            "type": self.type,
            "version": self.version,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": str(self.aggregate_id),
            "tenant_ref": self.tenant_ref,
            "body_id": str(self.body_id) if self.body_id is not None else None,
            "visibility": self.visibility,
            "operation": self.operation,
            "occurred_at": self.occurred_at.isoformat(),
            "actor_ref": self.actor_ref,
            "correlation_id": str(self.correlation_id),
            "causation_id": str(self.causation_id) if self.causation_id is not None else None,
            "payload": copy.deepcopy(dict(self.payload)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Envelope:
        """Hülle aus ihrer JSON-Darstellung; prüft vorher gegen das Schema der Hülle."""
        from .validation import validate_envelope

        validate_envelope(data)
        return cls(
            event_id=uuid.UUID(data["event_id"]),
            type=data["type"],
            version=data["version"],
            aggregate_type=data["aggregate_type"],
            aggregate_id=uuid.UUID(data["aggregate_id"]),
            tenant_ref=data["tenant_ref"],
            body_id=_optional_uuid(data.get("body_id")),
            visibility=data["visibility"],
            operation=data["operation"],
            occurred_at=datetime.fromisoformat(data["occurred_at"]),
            actor_ref=data.get("actor_ref"),
            correlation_id=uuid.UUID(data["correlation_id"]),
            causation_id=_optional_uuid(data.get("causation_id")),
            payload=data["payload"],
        )


def _optional_uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value is not None else None

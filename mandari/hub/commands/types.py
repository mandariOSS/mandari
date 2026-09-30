# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehl, Quittung und Ergebnis eines Handlers (``docs/adr/20260929-befehle-synchron.md``).

- ``Command``: Name und Version (Vertrag in ``hub.contracts``), Inhalt, Idempotenzschlüssel,
  Mandant (``tenant_ref`` wie in der Ereignishülle) und Auslöser (``actor_ref``).
- ``HandlerResult``: was der Handler beim Eigentümer liefert, v. a. die Eingangsnummer bzw. Kennung.
- ``Receipt``: die Quittung, die beide Clients zurückgeben: Eingangsnummer, Eingangszeit und
  Inhalts-Hash (SHA-256 über das kanonische JSON des Inhalts, RFC 8785).

Quittungen enthalten nur Kennungen, Codes und Zeitpunkte, nie Inhalte; sie werden gespeichert und bei
einer Wiederholung mit demselben Schlüssel unverändert zurückgegeben.

Der Inhalt eines Befehls ist höchstens ``MAX_DEPTH`` Ebenen tief verschachtelt. Kopieren, Prüfen und
kanonisches JSON arbeiten rekursiv; ein tiefer verschachtelter Inhalt endete sonst je nach Plattform
als ``RecursionError`` an wechselnden Stellen.
"""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from hub.contracts import envelope_schema

_ENVELOPE_PROPERTIES = envelope_schema()["properties"]
#: Muster aus der Ereignishülle, damit Befehle und Ereignisse dieselben Kennungen tragen.
TENANT_REF_RE: Final = re.compile(_ENVELOPE_PROPERTIES["tenant_ref"]["pattern"])
ACTOR_REF_RE: Final = re.compile(_ENVELOPE_PROPERTIES["actor_ref"]["anyOf"][0]["pattern"])
MAX_REFERENCE_LENGTH: Final = 200
#: Größte Verschachtelungstiefe des Inhalts (Objekte und Listen); Verträge sind weit flacher.
MAX_DEPTH: Final = 64
TOO_DEEP: Final = f"Der Inhalt ist zu tief verschachtelt (höchstens {MAX_DEPTH} Ebenen)."


def exceeds_depth(value: object, limit: int = MAX_DEPTH) -> bool:
    """Ist ``value`` tiefer als ``limit`` Ebenen verschachtelt? Ohne Rekursion, bricht früh ab."""
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        node, depth = pending.pop()
        if isinstance(node, Mapping):
            children: list[object] = list(node.values())
        elif isinstance(node, list | tuple):
            children = list(node)
        else:
            continue
        if depth > limit:
            return True
        pending.extend((child, depth + 1) for child in children)
    return False


@dataclass(frozen=True, kw_only=True)
class Command:
    """Ein Befehl an den Eigentümer der Daten; der Inhalt wird beim Anlegen kopiert."""

    name: str
    body: Mapping[str, Any]
    idempotency_key: str
    tenant_ref: str
    version: int = 1
    actor_ref: str | None = None
    correlation_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if not TENANT_REF_RE.fullmatch(self.tenant_ref):
            raise ValueError("tenant_ref muss session:<uuid>, org:<uuid> oder source:<uuid> sein")
        if self.actor_ref is not None and not ACTOR_REF_RE.fullmatch(self.actor_ref):
            raise ValueError("actor_ref muss user:<uuid> oder system:<auftrag> sein")
        if exceeds_depth(self.body):
            raise ValueError(TOO_DEEP)
        object.__setattr__(self, "body", copy.deepcopy(dict(self.body)))


@dataclass(frozen=True, kw_only=True)
class HandlerResult:
    """Ergebnis des Handlers: Eingangsnummer bzw. Kennung beim Eigentümer und weitere Kennungen."""

    reference: str
    aggregate_id: uuid.UUID | None = None
    received_at: datetime | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not 0 < len(self.reference) <= MAX_REFERENCE_LENGTH:
            raise ValueError(f"reference muss 1 bis {MAX_REFERENCE_LENGTH} Zeichen lang sein")
        if self.received_at is not None and self.received_at.utcoffset() is None:
            raise ValueError("received_at braucht eine Zeitzone")


@dataclass(frozen=True, kw_only=True)
class Receipt:
    """Quittung eines Befehls; dieselbe für die erste Ausführung und jede Wiederholung."""

    command: str
    version: int
    reference: str
    received_at: datetime
    content_hash: str
    aggregate_id: uuid.UUID | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "version": self.version,
            "reference": self.reference,
            "received_at": self.received_at.astimezone(UTC).isoformat(),
            "content_hash": self.content_hash,
            "aggregate_id": str(self.aggregate_id) if self.aggregate_id is not None else None,
            "data": copy.deepcopy(dict(self.data)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Receipt:
        aggregate_id = data.get("aggregate_id")
        return cls(
            command=str(data["command"]),
            version=int(data["version"]),
            reference=str(data["reference"]),
            received_at=datetime.fromisoformat(str(data["received_at"])),
            content_hash=str(data["content_hash"]),
            aggregate_id=uuid.UUID(str(aggregate_id)) if aggregate_id else None,
            data=dict(data.get("data") or {}),
        )

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignisse der Live-Übertragungen in der Datendrehscheibe (Issue #915).

``ris.broadcast.started``, ``ris.broadcast.agenda_item_started``, ``ris.broadcast.speaker_changed`` und
``ris.broadcast.ended`` (je Version 1, Schemas unter ``hub/contracts/schemas``). Regeln:

- **Nur Kennungen und Codes**, nie gelesene Texte (Name, Fraktion, Titel).
- **Aggregat ``Broadcast``** (Kennung der Übertragung): ein eigener Typ, damit weder der OParl-Änderungsfeed
  (``hub.api.changes`` liest nur die OParl-Typen) noch der Suchindex (``ris.meeting.*`` usw.) reagieren.
- **Mandant wie beim Ingestor**: die Quelle der Kommune (``source:<uuid>``); eine Quelle mit
  ``sync_config["events_enabled"] = false`` bleibt ausgenommen (``hub.ris.retraction.event_source_id``).
- **In der laufenden Transaktion** (``publish()``): Der Aufrufer schreibt Zustand und Ereignis zusammen.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from apps.events import CanonicalRef, publish, tenant_ref
from apps.events.models import Visibility
from hub.ris.retraction import event_source_id

VERSION: Final = 1
STARTED: Final = "ris.broadcast.started"
AGENDA_ITEM_STARTED: Final = "ris.broadcast.agenda_item_started"
SPEAKER_CHANGED: Final = "ris.broadcast.speaker_changed"
ENDED: Final = "ris.broadcast.ended"
AGGREGATE: Final = "Broadcast"

#: Sichtbarkeit je Typ (laut Vertrag): Wortmeldungen nennen eine Person und bleiben intern
_SICHTBARKEIT: Final[dict[str, str]] = {
    STARTED: Visibility.OEFFENTLICH,
    AGENDA_ITEM_STARTED: Visibility.OEFFENTLICH,
    SPEAKER_CHANGED: Visibility.INTERN,
    ENDED: Visibility.OEFFENTLICH,
}


def melden(typ: str, *, broadcast_id: uuid.UUID, body_id: uuid.UUID | None, payload: dict[str, Any]) -> bool:
    """Schreibt ein Ereignis in der laufenden Transaktion; ``False``, wenn die Kommune keine Ereignisse schreibt."""
    source_id = event_source_id(body_id)
    if source_id is None:
        return False
    publish(
        typ,
        version=VERSION,
        aggregate=CanonicalRef(AGGREGATE, broadcast_id),
        tenant=tenant_ref("source", source_id),
        body_id=body_id,
        visibility=_SICHTBARKEIT[typ],
        payload={k: v for k, v in payload.items() if v is not None},
        actor_ref="system:hub.live",
    )
    return True

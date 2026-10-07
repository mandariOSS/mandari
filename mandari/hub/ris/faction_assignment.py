# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ereignis ``ris.person.faction_assigned`` zur Fraktionszuordnung ohne OParl-Fraktion (Issue #916).

Viele Kommunen liefern keine Fraktionsmitgliedschaften. Insight ordnet Personen deshalb selbst eine Fraktion zu
(``insight_core.services.fraktionen``): aus der Einblendung einer Live-Übertragung, von Hand oder aus OParl. Jede
bestätigte Zuordnung und jede Änderung daran meldet dieses Modul, mit denselben Regeln wie die übrigen
Ereignisse zum RIS-Bestand (``hub.ris.retraction``, ``hub.ris.text_extraction``):

- **Sichtbarkeit ``oeffentlich``** wie ``ris.person.changed``: Die bestätigte Fraktion zeigt das Bürgerportal.
  Vorschläge sind nicht öffentlich und werden nicht gemeldet.
- **Nur Kennungen und Codes.** Die Bezeichnung der Fraktion und die Partei sind Freitext und stehen nie in der
  Nutzlast; Empfänger lesen sie aus dem Bestand.
- **Herkunft und Schalter wie beim Ingestor:** Mandant ist die Quelle der Körperschaft (``source:<uuid>``),
  ``INGESTOR_EVENTS_ENABLED`` gilt, eine Quelle mit ``sync_config["events_enabled"] = false`` bleibt ausgenommen.
- **In der laufenden Transaktion** (``publish()``): Zuordnung und Ereignis gelten gemeinsam oder gar nicht.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Final

from apps.events import CanonicalRef, publish, tenant_ref
from apps.events.models import Visibility
from hub.ris.retraction import event_source_id, events_enabled

#: Schemaversion des Ereignisses
VERSION: Final = 1
FACTION_ASSIGNED: Final = "ris.person.faction_assigned"
#: Codes des Vertrags (Felder ``source`` und ``status``), gleich den Werten in ``insight_core.models.PersonFraktion``
SOURCES: Final = frozenset({"einblendung", "hand", "oparl"})
STATUSES: Final = frozenset({"bestaetigt", "abgelehnt"})


def payload(
    *,
    assignment_id: uuid.UUID,
    person_id: uuid.UUID,
    source: str,
    status: str,
    organization_id: uuid.UUID | None = None,
    valid_from: date | None = None,
    valid_until: date | None = None,
) -> dict[str, Any]:
    """Nutzlast laut Vertrag: Kennungen, Codes und Daten, nie Bezeichnung oder Partei."""
    if source not in SOURCES:
        raise ValueError(f"Unbekannte Quelle einer Fraktionszuordnung; erlaubt: {', '.join(sorted(SOURCES))}")
    if status not in STATUSES:
        raise ValueError(f"Unbekannter Status für das Ereignis; erlaubt: {', '.join(sorted(STATUSES))}")
    nutzlast: dict[str, Any] = {
        "assignment": str(assignment_id),
        "person": str(person_id),
        "source": source,
        "status": status,
    }
    if organization_id is not None:
        nutzlast["organization"] = str(organization_id)
    if valid_from is not None:
        nutzlast["valid_from"] = valid_from.isoformat()
    if valid_until is not None:
        nutzlast["valid_until"] = valid_until.isoformat()
    return nutzlast


def report_faction_assigned(
    *,
    assignment_id: uuid.UUID,
    person_id: uuid.UUID,
    body_id: uuid.UUID,
    source: str,
    status: str,
    organization_id: uuid.UUID | None = None,
    valid_from: date | None = None,
    valid_until: date | None = None,
) -> bool:
    """Meldet den Stand einer Fraktionszuordnung in der laufenden Transaktion; ``True``, wenn geschrieben."""
    nutzlast = payload(
        assignment_id=assignment_id,
        person_id=person_id,
        source=source,
        status=status,
        organization_id=organization_id,
        valid_from=valid_from,
        valid_until=valid_until,
    )
    if not events_enabled():
        return False
    source_id = event_source_id(body_id)
    if source_id is None:
        return False
    publish(
        FACTION_ASSIGNED,
        version=VERSION,
        aggregate=CanonicalRef("Person", person_id),
        tenant=tenant_ref("source", source_id),
        body_id=body_id,
        visibility=Visibility.OEFFENTLICH,
        payload=nutzlast,
    )
    return True

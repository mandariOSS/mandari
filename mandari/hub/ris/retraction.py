# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rücknahme einzelner Objekte des RIS-Bestands – mit Ereignis für den Änderungsfeed (Issue #707).

Der Ingestor meldet eine Löschmarkierung als ``ris.object.depublished`` nur beim Übergang
``deleted = false → true`` (``ingestor/src/storage/database.py``, ``mark_entity_deleted``). mandari Session
nimmt eigene Objekte aber schon im Moment der Änderung aus dem Bürgerportal, ohne auf den nächsten Abgleich
zu warten (``apps.session.oparl_publication.retract_from_portal``): Ein Tagesordnungspunkt wird
nichtöffentlich, eine Anlage gelöscht, eine Vorlage zurück in den Entwurf genommen. Beim nächsten Abgleich
ist die Zeile schon markiert, und der Ingestor meldet nichts. Ohne eigene Meldung nennte der Änderungsfeed
die Rücknahme nie, und Abnehmer behielten das Objekt.

``retract`` markiert deshalb die Zeile und meldet die Rücknahme in derselben Transaktion, mit denselben
Regeln wie der Ingestor (``ingestor/src/storage/ris_events.py``, ``depublished_events``; ein Test vergleicht
beide):

- **Ereignis:** ``ris.object.depublished`` (öffentlich, Operation ``delete``) mit Typ, Kennung und Grund.
  Ein nichtöffentlicher Tagesordnungspunkt war für öffentliche Empfänger nie sichtbar; seine Löschung geht
  als ``ris.agendaitem.changed`` (``deleted``) nur an Empfänger der Sichtbarkeit ``nichtoeffentlich``.
- **Herkunft wie beim Ingestor:** Mandant ist die Quelle der Kommune (``source:<uuid>``), ``body_id`` die
  Kommune des Objekts (beim Tagesordnungspunkt die seiner Sitzung, bei der Mitgliedschaft die ihres
  Gremiums). Ohne Kommune gibt es kein Ereignis.
- **Schalter wie beim Ingestor:** ``INGESTOR_EVENTS_ENABLED``; eine Quelle mit
  ``sync_config["events_enabled"] = false`` bleibt ausgenommen. Beides gilt für den ganzen Bestand,
  gleich wer ihn schreibt.
- **Nie doppelt:** Die Zeile wird gesperrt (``FOR NO KEY UPDATE``) und erneut gelesen. Ist sie schon
  markiert (auch durch einen gleichzeitigen Abgleich), gibt es weder Markierung noch Ereignis. Umgekehrt
  findet der Ingestor danach keine unmarkierte Zeile mehr (``UPDATE … WHERE deleted = false``) und meldet
  seinerseits nichts.
- **Die Rücknahme geht vor:** Scheitert das Schreiben des Ereignisses, bleibt das Objekt trotzdem
  zurückgenommen (eigener Sicherungspunkt), und der Fehler wird protokolliert. Nichtöffentliches muss das
  Bürgerportal sofort verlassen; eine fehlende Meldung im Feed ist der kleinere Schaden.

Die Rücknahme einer ganzen Kommune (``apps.session.services.insight_service.retract_source``) meldet nichts:
Sie ändert den Bestand am Journal vorbei, und der Feed beginnt danach einen neuen Abschnitt
(``hub.api.changes``, ``Feed.epoch``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from django.conf import settings
from django.db import transaction

from apps.events import CanonicalRef, publish, tenant_ref
from apps.events.models import Operation, Visibility
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlLegislativeTerm,
    OParlLocation,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
)

logger = logging.getLogger(__name__)

#: Schemaversion der Ereignisse dieses Moduls
VERSION: Final = 1
DEPUBLISHED: Final = "ris.object.depublished"
AGENDA_ITEM_CHANGED: Final = "ris.agendaitem.changed"

#: Gründe einer Rücknahme (Vertrag ``ris.object.depublished``, Feld ``reason``). ``datenschutz`` gehört
#: nicht hierher: Es verlangt ``redact`` und das Entfernen aus Kopien, nicht nur eine Löschmarkierung.
REASON_DELETED_AT_SOURCE: Final = "quelle_geloescht"
REASON_WITHDRAWN: Final = "zurueckgenommen"
REASON_NOT_PUBLIC: Final = "nichtoeffentlich"
REASONS: Final = frozenset({REASON_DELETED_AT_SOURCE, REASON_WITHDRAWN, REASON_NOT_PUBLIC})

#: Schlüssel in ``OParlSource.sync_config``, mit dem eine Quelle von den Ereignissen ausgenommen ist
#: (gleichlautend mit ``SYNC_CONFIG_EVENTS_KEY`` in ``ingestor/src/storage/database.py``)
SYNC_CONFIG_EVENTS_KEY: Final = "events_enabled"

#: Modell des Bestands -> kanonischer Typ (``aggregate_type`` der Hülle, ``object_type`` der Nutzlast)
AGGREGATE_TYPES: Final[dict[type, str]] = {
    OParlBody: "Body",
    OParlOrganization: "Organization",
    OParlPerson: "Person",
    OParlMembership: "Membership",
    OParlLegislativeTerm: "LegislativeTerm",
    OParlMeeting: "Meeting",
    OParlAgendaItem: "AgendaItem",
    OParlPaper: "Paper",
    OParlConsultation: "Consultation",
    OParlFile: "File",
    OParlLocation: "Location",
}


@dataclass(frozen=True)
class Draft:
    """Ein Ereignis, wie es ``retract`` schreibt (ohne Hülle): Typ, Aggregat, Sichtbarkeit, Operation, Nutzlast."""

    type: str
    aggregate_type: str
    aggregate_id: uuid.UUID
    visibility: str
    operation: str
    payload: dict[str, Any]


def drafts(
    aggregate_type: str,
    object_id: uuid.UUID,
    reason: str,
    *,
    public: bool = True,
    meeting_id: uuid.UUID | None = None,
) -> list[Draft]:
    """
    Ereignisse zur Rücknahme eines Objekts (wie ``ris_events.depublished_events`` des Ingestors).

    ``public`` und ``meeting_id`` gelten für Tagesordnungspunkte: Einen nichtöffentlichen Punkt haben
    öffentliche Empfänger nie gesehen; eine öffentliche Rücknahme nennte ihnen erstmals seine Kennung.
    """
    if reason not in REASONS:
        raise ValueError(f"Unbekannter Grund einer Rücknahme; erlaubt: {', '.join(sorted(REASONS))}")
    if aggregate_type == "AgendaItem" and not public:
        if meeting_id is None:
            return []
        payload = {"agenda_item": str(object_id), "meeting": str(meeting_id), "change": "deleted"}
        return [
            Draft(AGENDA_ITEM_CHANGED, "AgendaItem", object_id, Visibility.NICHTOEFFENTLICH, Operation.DELETE, payload)
        ]
    payload = {"object_type": aggregate_type, "object": str(object_id), "reason": reason}
    return [Draft(DEPUBLISHED, aggregate_type, object_id, Visibility.OEFFENTLICH, Operation.DELETE, payload)]


def events_enabled() -> bool:
    """Schreibt diese Installation Ereignisse zum RIS-Bestand (``INGESTOR_EVENTS_ENABLED``)?"""
    return bool(getattr(settings, "INGESTOR_EVENTS_ENABLED", False))


def _body_id(row: Any) -> uuid.UUID | None:
    """Kommune des Objekts, wie sie der Ingestor in ``body_id`` einträgt."""
    if isinstance(row, OParlAgendaItem):
        return OParlMeeting.objects.filter(pk=row.meeting_id).values_list("body_id", flat=True).first()
    if isinstance(row, OParlMembership):
        return OParlOrganization.objects.filter(pk=row.organization_id).values_list("body_id", flat=True).first()
    if isinstance(row, OParlBody):
        return row.pk
    return getattr(row, "body_id", None)


def event_source_id(body_id: uuid.UUID | None) -> uuid.UUID | None:
    """Quelle der Kommune, sofern sie Ereignisse schreibt; sonst ``None``."""
    if body_id is None:
        return None
    found = OParlBody.objects.filter(pk=body_id).values_list("source_id", "source__sync_config").first()
    if found is None or found[0] is None:
        return None
    source_id: uuid.UUID = found[0]
    config = found[1]
    if isinstance(config, dict) and config.get(SYNC_CONFIG_EVENTS_KEY) is False:
        return None
    return source_id


def _report(row: Any, reason: str) -> int:
    """Rücknahme melden (in der laufenden Transaktion); Zahl der geschriebenen Ereignisse."""
    aggregate_type = AGGREGATE_TYPES.get(type(row))
    if aggregate_type is None or not events_enabled():
        return 0
    body_id = _body_id(row)
    source_id = event_source_id(body_id)
    if source_id is None:
        return 0
    public = not isinstance(row, OParlAgendaItem) or row.public is not False
    meeting_id = row.meeting_id if isinstance(row, OParlAgendaItem) else None
    written = 0
    for draft in drafts(aggregate_type, row.pk, reason, public=public, meeting_id=meeting_id):
        publish(
            draft.type,
            version=VERSION,
            aggregate=CanonicalRef(draft.aggregate_type, draft.aggregate_id),
            tenant=tenant_ref("source", source_id),
            body_id=body_id,
            visibility=draft.visibility,
            operation=draft.operation,
            occurred_at=row.deleted_at,
            payload=draft.payload,
        )
        written += 1
    return written


def retract(obj: Any, *, reason: str, when: datetime | None = None) -> bool:
    """
    Objekt des Bestands als gelöscht markieren und die Rücknahme melden.

    ``False``: Das Objekt war schon markiert (oder gibt es nicht mehr); dann geschieht nichts.
    """
    if reason not in REASONS:
        raise ValueError(f"Unbekannter Grund einer Rücknahme; erlaubt: {', '.join(sorted(REASONS))}")
    model = type(obj)
    with transaction.atomic():
        # So stark wie die Markierung selbst und wie die Sperre des Ingestors (``FOR NO KEY UPDATE``):
        # Einfügungen mit Fremdschlüssel auf die Zeile warten darauf nicht.
        row = model._default_manager.select_for_update(no_key=True).filter(pk=obj.pk, deleted=False).first()
        if row is None:
            return False
        row.mark_deleted(when, reason=reason)
        try:
            with transaction.atomic():
                _report(row, reason)
        except Exception:
            # Die Rücknahme gilt trotzdem; der Feed verpasst sie. Meldung ohne Inhalte, nur Kennungen.
            logger.exception("Rücknahme von %s %s ohne Ereignis im Journal", model.__name__, row.pk)
    obj.deleted, obj.deleted_at, obj.oparl_modified = row.deleted, row.deleted_at, row.oparl_modified
    return True

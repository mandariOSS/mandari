# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Testhilfe: Ereignisse für den Änderungsfeed anlegen – geprüft gegen das Vertragsregister.

Die Erzeuger der Ereignisse (Ingestor, Session) sind nicht Teil der Schnittstelle. Tests legen ihre
Ereignisse deshalb selbst an, aber nur solche, die der Vertrag ihres Typs erlaubt
(``hub.contracts.get_registry().validate_event``): Hülle, Sichtbarkeit und Nutzlast.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from django.db.models import Max

from apps.events.models import Event
from hub.contracts import Envelope, get_registry

#: Ereignistyp -> (kanonischer Objekttyp, Feld der Nutzlast mit der Kennung des Objekts).
#: ``ris.object.depublished`` nennt den Typ in der Nutzlast (``object_type``).
AGGREGATE: dict[str, tuple[str, str]] = {
    "ris.agendaitem.changed": ("AgendaItem", "agenda_item"),
    "ris.body.changed": ("Body", "body"),
    "ris.consultation.changed": ("Consultation", "consultation"),
    "ris.file.changed": ("File", "file"),
    "ris.legislativeterm.changed": ("LegislativeTerm", "legislative_term"),
    "ris.location.changed": ("Location", "location"),
    "ris.meeting.changed": ("Meeting", "meeting"),
    "ris.meeting.scheduled": ("Meeting", "meeting"),
    "ris.membership.changed": ("Membership", "membership"),
    "ris.object.depublished": ("", "object"),
    "ris.organization.changed": ("Organization", "organization"),
    "ris.paper.changed": ("Paper", "paper"),
    "ris.person.changed": ("Person", "person"),
    "ris.person.faction_assigned": ("Person", "person"),
    "ris.paper.released": ("Paper", "paper"),
    "ris.protocol.published": ("Meeting", "meeting"),
    "ris.resolution.adopted": ("AgendaItem", "agenda_item"),
    "ris.resolution.implementation_changed": ("AgendaItem", "agenda_item"),
    "ris.source.published": ("Body", "body"),
    "ris.voting.recorded": ("Voting", "voting"),
}
#: Öffentliche ``ris.*``-Typen, die bewusst keinen Eintrag im Feed ergeben: Live-Übertragungen (Issue #915) haben
#: den eigenen Objekttyp ``Broadcast`` und ändern den RIS-Bestand nicht
OHNE_EINTRAG: dict[str, tuple[str, str]] = {
    "ris.broadcast.started": ("Broadcast", "broadcast"),
    "ris.broadcast.agenda_item_started": ("Broadcast", "broadcast"),
    "ris.broadcast.ended": ("Broadcast", "broadcast"),
}

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
TENANT = "source:3c9a1e2b-4d5f-4a6b-8c7d-9e0f1a2b3c4d"


def naechste_nummer() -> int:
    return (Event.objects.aggregate(hoechste=Max("seq"))["hoechste"] or 0) + 1


def huelle(
    typ: str,
    body: uuid.UUID | None,
    objekt: uuid.UUID | None = None,
    *,
    sichtbarkeit: str = "oeffentlich",
    operation: str = "upsert",
    nutzlast: dict[str, Any] | None = None,
    zeit: datetime = T0,
    mandant: str = TENANT,
) -> Envelope:
    """Hülle eines Ereignisses; Nutzlast aus dem ersten Beispiel des Vertrags, Kennung des Objekts ersetzt."""
    vertrag = get_registry().latest(typ)
    objekttyp, feld = AGGREGATE[typ] if typ in AGGREGATE else OHNE_EINTRAG[typ]
    inhalt = dict(nutzlast if nutzlast is not None else vertrag.examples[0])
    objekt = objekt or uuid.uuid4()
    inhalt[feld] = str(objekt)
    return Envelope(
        type=typ,
        version=vertrag.version,
        aggregate_type=objekttyp or str(inhalt["object_type"]),
        aggregate_id=objekt,
        tenant_ref=mandant,
        visibility=sichtbarkeit,
        operation=operation,
        body_id=body,
        occurred_at=zeit,
        payload=inhalt,
    )


def schreiben(ereignis: Envelope, *, nummeriert: bool = True) -> Event:
    """
    Ereignis gegen das Vertragsregister prüfen und in das Journal schreiben. Mit ``nummeriert`` bekommt es
    die nächste Folgenummer, wie es der Sequenzierer nach dem Commit täte.
    """
    get_registry().validate_event(ereignis)
    return Event.objects.create(
        event_id=ereignis.event_id,
        type=ereignis.type,
        version=ereignis.version,
        aggregate_type=ereignis.aggregate_type,
        aggregate_id=ereignis.aggregate_id,
        tenant_ref=ereignis.tenant_ref,
        body_id=ereignis.body_id,
        visibility=ereignis.visibility,
        operation=ereignis.operation,
        occurred_at=ereignis.occurred_at,
        actor_ref=ereignis.actor_ref,
        correlation_id=ereignis.correlation_id,
        causation_id=ereignis.causation_id,
        payload=dict(ereignis.payload),
        seq=naechste_nummer() if nummeriert else None,
    )


def ereignis(typ: str, body: uuid.UUID | None, objekt: uuid.UUID | None = None, **angaben: Any) -> Event:
    """Ein nummeriertes, vertragsgemäßes Ereignis im Journal."""
    nummeriert = angaben.pop("nummeriert", True)
    return schreiben(huelle(typ, body, objekt, **angaben), nummeriert=nummeriert)


def ruecknahme(body: uuid.UUID, objekttyp: str, objekt: uuid.UUID, grund: str, **angaben: Any) -> Event:
    """``ris.object.depublished``: ``delete`` bzw. beim Grund ``datenschutz`` ``redact``."""
    angaben.setdefault("operation", "redact" if grund == "datenschutz" else "delete")
    nutzlast = {"object_type": objekttyp, "object": str(objekt), "reason": grund}
    return ereignis("ris.object.depublished", body, objekt, nutzlast=nutzlast, **angaben)

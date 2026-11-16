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
- **Grund ``datenschutz``:** Die Hülle trägt ``redact`` statt ``delete`` (Vertrag ``ris.object.depublished``,
  ``docs/adr/20260929-aenderungsfeed-format.md``). Der Änderungsfeed gibt das so aus; Abnehmer entfernen das
  Objekt dann auch aus Kopien, Zwischenspeichern und Weitergaben. Diesen Grund setzt nur der Betrieb
  (``insight_core.admin.RetractAdminMixin``), nie der Abgleich mit der Quelle.
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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from django.conf import settings
from django.db import transaction

from apps.events import CanonicalRef, publish, tenant_ref
from apps.events.models import Event, Operation, Visibility
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

#: Gründe einer Rücknahme (Vertrag ``ris.object.depublished``, Feld ``reason``), gleichlautend mit
#: ``insight_core.models.DELETION_REASON_CHOICES``. ``datenschutz`` meldet ``redact`` statt ``delete``
#: (``operation_for``): Abnehmer sollen das Objekt auch aus ihren Kopien entfernen.
REASON_DELETED_AT_SOURCE: Final = "quelle_geloescht"
REASON_WITHDRAWN: Final = "zurueckgenommen"
REASON_NOT_PUBLIC: Final = "nichtoeffentlich"
REASON_PRIVACY: Final = "datenschutz"
REASONS: Final = frozenset({REASON_DELETED_AT_SOURCE, REASON_WITHDRAWN, REASON_NOT_PUBLIC, REASON_PRIVACY})
#: Felder des Vertrags für die Objekte, in die ein zurückgenommenes eingebettet war (Issue #1229)
EMBEDDING_FIELDS: Final = frozenset({"meeting", "paper", "agenda_item", "person"})

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


def operation_for(reason: str) -> str:
    """
    Operation der Hülle zu einem Grund: ``redact`` bei ``datenschutz``, sonst ``delete`` (wie
    ``ris_events.depublished_draft`` des Ingestors).

    Ohne diese Unterscheidung erführen Abnehmer eine Entfernung aus Datenschutzgründen nur als gewöhnliche
    Rücknahme und behielten Kopien und Weitergaben.
    """
    return Operation.REDACT if reason == REASON_PRIVACY else Operation.DELETE


def drafts(
    aggregate_type: str,
    object_id: uuid.UUID,
    reason: str,
    *,
    public: bool = True,
    meeting_id: uuid.UUID | None = None,
    embedded_in: Mapping[str, uuid.UUID | None] | None = None,
) -> list[Draft]:
    """
    Ereignisse zur Rücknahme eines Objekts (wie ``ris_events.depublished_events`` des Ingestors).

    ``public`` und ``meeting_id`` gelten für Tagesordnungspunkte: Einen nichtöffentlichen Punkt haben
    öffentliche Empfänger nie gesehen; eine öffentliche Rücknahme nennte ihnen erstmals seine Kennung.

    ``embedded_in``: Objekte, in die das zurückgenommene eingebettet war (Felder ``meeting``, ``paper``,
    ``agenda_item``, ``person`` des Vertrags; ``None`` entfällt), damit der Änderungsfeed auch sie nennt, wenn die
    Zeile danach nicht mehr besteht (Issue #1229). Nur Objekte, die öffentlich waren: Ein öffentliches Ereignis nennt
    keine Kennung eines Objekts, das nie öffentlich war. Der Bestand behält seine Zeilen und braucht die Angabe nicht.
    """
    if reason not in REASONS:
        raise ValueError(f"Unbekannter Grund einer Rücknahme; erlaubt: {', '.join(sorted(REASONS))}")
    if aggregate_type == "AgendaItem" and not public:
        if meeting_id is None:
            return []
        payload = {"agenda_item": str(object_id), "meeting": str(meeting_id), "change": "deleted"}
        return [
            Draft(
                AGENDA_ITEM_CHANGED,
                "AgendaItem",
                object_id,
                Visibility.NICHTOEFFENTLICH,
                operation_for(reason),
                payload,
            )
        ]
    payload = {"object_type": aggregate_type, "object": str(object_id), "reason": reason}
    for name, parent in (embedded_in or {}).items():
        if name not in EMBEDDING_FIELDS:
            raise ValueError(f"Unbekanntes Feld für das einbettende Objekt: {name}")
        if parent is not None:
            payload[name] = str(parent)
    return [Draft(DEPUBLISHED, aggregate_type, object_id, Visibility.OEFFENTLICH, operation_for(reason), payload)]


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


def _report(row: Any, reason: str, when: datetime | None = None) -> int:
    """
    Rücknahme melden (in der laufenden Transaktion); Zahl der geschriebenen Ereignisse. ``when``: Zeitpunkt der
    Rücknahme, wenn er nicht der der Markierung ist (``report_withdrawal``).
    """
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
            occurred_at=when or row.deleted_at,
            payload=draft.payload,
        )
        written += 1
    return written


def retract(obj: Any, *, reason: str, when: datetime | None = None) -> bool:
    """
    Objekt des Bestands als gelöscht markieren und die Rücknahme melden.

    ``False``: Das Objekt war schon markiert (oder gibt es nicht mehr); dann geschieht nichts. Mit dem Grund
    ``datenschutz`` trägt die Meldung ``redact`` (``operation_for``); diese Markierung lässt der Abgleich mit der
    Quelle stehen (Ingestor, ``keeps_withdrawal``).
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
    # Zwischengespeicherte öffentliche Ausgaben (Feeds, Kartenpunkte) der Kommune entstehen neu
    from insight_core.services import ruecknahme_stand

    transaction.on_commit(ruecknahme_stand.forget, robust=True)
    return True


def redaction_reported(row: Any) -> bool:
    """
    Steht zu dieser Zeile schon eine Meldung ``redact`` im Journal?

    Eine Zeile kann den Grund ``datenschutz`` tragen, ohne dass Abnehmer davon wissen: im Formular der Verwaltung von
    Hand markiert, oder zurückgenommen, als dafür noch ``delete`` gemeldet wurde. Die Verwaltungsaktion holt die
    Meldung dann nach (``insight_core.admin``). Ist das Journal inzwischen aufgeräumt, lautet die Antwort wieder
    ``False``; eine zweite Meldung schadet nicht, Abnehmer verarbeiten Einträge wiederholbar.
    """
    return Event.objects.filter(aggregate_id=row.pk, type=DEPUBLISHED, operation=Operation.REDACT).exists()


def report_withdrawal(row: Any, *, reason: str, when: datetime | None = None) -> bool:
    """
    Rücknahme einer schon markierten Zeile melden, ohne sie zu markieren (``True``: gemeldet).

    Für die Verwaltungsaktion des Bürgerportals (``insight_core.admin.RetractAdminMixin``): Eine Zeile, die nur als
    in der Quelle gelöscht markiert war, blieb mit Hinweis öffentlich. Nimmt der Betrieb sie zurück, ändert sich ihr
    Grund, nicht die Markierung; ``retract`` meldete dann nichts, und Abnehmer, die in der Quelle Gelöschtes weiter
    zeigen, behielten ihre Kopie. Der Aufrufer sperrt die Zeile und setzt den Grund in derselben Transaktion. Wie bei
    ``retract`` geht die Rücknahme vor: Scheitert das Schreiben, bleibt sie bestehen, und der Fehler wird
    protokolliert. ``reason`` ist der Grund, den die Zeile danach trägt; mit ``datenschutz`` trägt die Meldung
    ``redact``.
    """
    if reason not in REASONS:
        raise ValueError(f"Unbekannter Grund einer Rücknahme; erlaubt: {', '.join(sorted(REASONS))}")
    try:
        with transaction.atomic():
            return _report(row, reason, when) > 0
    except Exception:
        logger.exception("Rücknahme von %s %s ohne Ereignis im Journal", type(row).__name__, row.pk)
        return False

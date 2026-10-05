# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``publish()``: die einzige Stelle, an der Fachmodule Ereignisse in das Journal schreiben.

    from django.db import transaction

    from apps.events import CanonicalRef, publish, tenant_ref

    with transaction.atomic():
        paper.save()
        publish(
            "ris.paper.changed",
            version=1,
            aggregate=CanonicalRef("Paper", paper_id),
            tenant=tenant_ref("session", tenant.id),
            body_id=body_id,
            visibility="oeffentlich",
            payload={"paper": str(paper_id), "changed": ["name"]},
        )

- **Transaktion:** Das Ereignis entsteht in derselben Transaktion wie die fachliche Änderung
  (``docs/adr/20260929-ereignistechnik-postgres.md``). Ohne offenen ``transaction.atomic()``-Block
  wirft ``publish()`` ``PublishOutsideTransactionError``. Der Rahmen, den Django-Tests um jeden
  Test legen, zählt dabei nicht: Ein Aufruf, der im Betrieb scheitern würde, scheitert auch im Test.
- **Kontext:** ``correlation_id`` und ``actor_ref`` muss der Aufrufer nicht durchreichen. Sie kommen
  aus ``event_context()``, sonst aus der laufenden Anfrage (Request-Kennung und angemeldetes Konto,
  ``apps.common.observability``), sonst aus dem OpenTelemetry-Kontext; ohne all das bekommt das
  Ereignis eine neue Korrelations-ID. ``actor_ref`` ist immer eine Kennung (``user:<uuid>`` oder
  ``system:<auftrag>``), nie ein Name.
- **Hülle:** Mandant, Sichtbarkeit, Operation, Typ und Kennungen werden immer geprüft (billige
  Formatprüfungen). Mandant und Sichtbarkeit sind Pflicht und kommen nie aus dem Kontext.
- **Vertrag:** Bei ``EVENTS_VALIDATE_CONTRACTS`` (Standard: wie ``DEBUG``, in Tests an) prüft
  ``publish()`` das Ereignis zusätzlich gegen das Vertragsregister. Die Plattform kennt die
  Drehscheibe nicht; ``hub.contracts`` hängt seine Prüfung beim Start über
  ``set_contract_validator()`` ein (``docs/adr/20260929-schichtenmodell.md``).

Dieses Modul lädt die Modelle erst beim Aufruf: ``apps.events`` exportiert ``publish`` schon, bevor
die App-Registry bereit ist.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, connections
from django.utils import timezone

from apps.common.observability import current_request_id, current_trace_context, user_id_var

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper

    from .models import Event

_UUID: Final = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Gleichlautend mit der Ereignishülle (``hub/contracts/envelope/v1.json``); ein Test der Drehscheibe
# vergleicht beide Seiten, weil die Plattform das Schema nicht lesen darf.
EVENT_TYPE_PATTERN: Final = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*){1,2}$"
AGGREGATE_TYPE_PATTERN: Final = r"^[A-Z][A-Za-z0-9]*$"
TENANT_REF_PATTERN: Final = rf"^(session|org|source):{_UUID}$"
ACTOR_REF_PATTERN: Final = rf"^(user:{_UUID}|system:[a-z][a-z0-9_.-]{{0,63}})$"
MAX_EVENT_TYPE_LENGTH: Final = 100
MAX_AGGREGATE_TYPE_LENGTH: Final = 64
MAX_VERSION: Final = 32767
#: Arten von Mandanten in ``tenant_ref``.
TENANT_KINDS: Final = ("session", "org", "source")

_EVENT_TYPE = re.compile(EVENT_TYPE_PATTERN)
_AGGREGATE_TYPE = re.compile(AGGREGATE_TYPE_PATTERN)
_TENANT_REF = re.compile(TENANT_REF_PATTERN)
_ACTOR_REF = re.compile(ACTOR_REF_PATTERN)
_SYSTEM_JOB = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

#: Namensraum für Korrelations-IDs aus Request-Kennungen, die selbst keine UUID sind.
_REQUEST_NAMESPACE: Final = uuid.uuid5(uuid.NAMESPACE_URL, "urn:mandari:request-id")

#: Prüfung eines Ereignisses gegen seinen Vertrag: bekommt die JSON-Darstellung der Hülle und wirft
#: bei einem Verstoß. Meldungen nennen Stelle und Regel, nie Werte.
ContractValidator = Callable[[Mapping[str, Any]], None]


class PublishOutsideTransactionError(RuntimeError):
    """``publish()`` wurde ohne offenen ``transaction.atomic()``-Block aufgerufen."""

    def __init__(self) -> None:
        super().__init__(
            "publish() braucht einen offenen transaction.atomic()-Block: Das Ereignis muss in derselben "
            "Transaktion entstehen wie die fachliche Änderung."
        )


class InvalidEventError(ValueError):
    """Die Angaben zur Hülle sind ungültig; die Meldung nennt das Feld und die Regel, nie den Wert."""


class CanonicalRef(NamedTuple):
    """Bezug auf ein Objekt: kanonischer Typ (z. B. ``Paper``) und kanonische Kennung."""

    type: str
    id: uuid.UUID


def tenant_ref(kind: str, tenant_id: uuid.UUID | str) -> str:
    """``tenant_ref`` der Hülle: ``session:<uuid>``, ``org:<uuid>`` oder ``source:<uuid>``."""
    if kind not in TENANT_KINDS:
        raise InvalidEventError("tenant_ref: Art muss session, org oder source sein")
    return f"{kind}:{_as_uuid(tenant_id, 'tenant_ref')}"


def user_ref(user_id: uuid.UUID | str) -> str:
    """``actor_ref`` für ein Konto: ``user:<uuid>`` (die Kennung, nie Name oder Mailadresse)."""
    return f"user:{_as_uuid(user_id, 'actor_ref')}"


def system_ref(job: str) -> str:
    """``actor_ref`` für einen technischen Auslöser: ``system:<auftrag>``, z. B. ``system:ingestor``."""
    if not _SYSTEM_JOB.fullmatch(job):
        raise InvalidEventError("actor_ref: Auftragsname klein, höchstens 64 Zeichen aus a-z, 0-9, _ . -")
    return f"system:{job}"


def correlation_id_for_request(request_id: str) -> uuid.UUID:
    """Korrelations-ID zu einer Request-Kennung (``X-Request-ID``).

    Ist die Kennung eine UUID (so erzeugt sie die Middleware selbst), wird sie übernommen. Eine
    andere Kennung des Reverse Proxys ergibt immer dieselbe abgeleitete UUID; so lassen sich
    Logzeilen und Ereignisse einer Anfrage auch dann zuordnen.
    """
    try:
        return uuid.UUID(request_id)
    except ValueError:
        return uuid.uuid5(_REQUEST_NAMESPACE, request_id)


# --- Kontext ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EventContext:
    """Angaben, die für alle Ereignisse eines Vorgangs gelten."""

    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None
    actor_ref: str | None = None


_context: ContextVar[EventContext | None] = ContextVar("events_publish_context", default=None)


def current_context() -> EventContext | None:
    """Der mit ``event_context()`` gesetzte Kontext oder ``None``."""
    return _context.get()


@contextmanager
def event_context(
    *,
    correlation_id: uuid.UUID | None = None,
    actor_ref: str | None = None,
    causation_id: uuid.UUID | None = None,
    caused_by: Event | None = None,
) -> Iterator[EventContext]:
    """Kontext für alle ``publish()``-Aufrufe im Block (auch in aufgerufenen Funktionen).

    - Aufträge und Befehle ohne Anfrage: ``event_context(actor_ref=system_ref("sync"))`` gibt allen
      Ereignissen des Blocks dieselbe Korrelations-ID.
    - Folgeereignisse in einem Handler: ``event_context(caused_by=ereignis)`` übernimmt dessen
      Korrelations-ID und setzt ``causation_id`` auf dessen ``event_id``.

    Was nicht angegeben ist, gilt aus dem umgebenden Kontext weiter; die Korrelations-ID kommt sonst
    aus der Anfrage bzw. dem OpenTelemetry-Kontext oder wird einmal für den Block erzeugt.
    """
    outer = _context.get()
    if caused_by is not None:
        correlation_id = correlation_id or caused_by.correlation_id
        causation_id = causation_id or caused_by.event_id
    if actor_ref is not None:
        _check_actor_ref(actor_ref)
    context = EventContext(
        correlation_id=correlation_id or (outer.correlation_id if outer else None) or _ambient_correlation_id(),
        causation_id=causation_id or (outer.causation_id if outer else None),
        actor_ref=actor_ref or (outer.actor_ref if outer else None),
    )
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def _ambient_correlation_id() -> uuid.UUID:
    """Korrelations-ID aus der Anfrage, sonst aus dem OpenTelemetry-Kontext, sonst neu."""
    request_id = current_request_id()
    if request_id:
        return correlation_id_for_request(request_id)
    trace_id, _ = current_trace_context()
    if trace_id:
        # Die Trace-Kennung hat wie eine UUID 128 Bit.
        return uuid.UUID(hex=trace_id)
    return uuid.uuid4()


def _ambient_actor_ref() -> str | None:
    """Das angemeldete Konto der laufenden Anfrage als ``user:<uuid>``, sonst ``None``."""
    user_id = user_id_var.get()
    if not user_id:
        return None
    try:
        return f"user:{uuid.UUID(user_id)}"
    except ValueError:
        return None


# --- Vertragsprüfung (Einhängepunkt) ---------------------------------------------------------------

_contract_validator: ContractValidator | None = None


def set_contract_validator(validator: ContractValidator | None) -> ContractValidator | None:
    """Hängt die Vertragsprüfung ein und gibt die bisherige zurück (``None`` hängt sie aus).

    Aufgerufen von ``hub.contracts`` beim Start. Die Plattform importiert die Drehscheibe nicht; wer
    die Verträge kennt, meldet sich hier an.
    """
    global _contract_validator
    previous = _contract_validator
    _contract_validator = validator
    return previous


def contract_validation_active() -> bool:
    """Prüft ``publish()`` gegen das Vertragsregister? Standard wie ``DEBUG``; in Tests an."""
    return bool(getattr(settings, "EVENTS_VALIDATE_CONTRACTS", settings.DEBUG))


# --- Veröffentlichen ---------------------------------------------------------------------------------


def publish(
    event_type: str,
    /,
    *,
    version: int,
    aggregate: CanonicalRef | tuple[str, uuid.UUID],
    tenant: str,
    visibility: str,
    payload: Mapping[str, Any],
    body_id: uuid.UUID | None = None,
    operation: str = "upsert",
    occurred_at: datetime | None = None,
    actor_ref: str | None = None,
    correlation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
    using: str = DEFAULT_DB_ALIAS,
) -> Event:
    """Schreibt ein Ereignis in das Journal, in der laufenden Transaktion (siehe Moduldokumentation).

    ``tenant`` ist die Mandantenkennung der Hülle (``tenant_ref("session", id)``), ``payload``
    enthält nur Kennungen, Codes und Namen geänderter Felder. Die Folgenummer vergibt nach dem Commit
    der Sequenzierer; das zurückgegebene Ereignis hat noch keine.
    """
    from .models import Event, Operation, Visibility

    if not in_transaction(connections[using]):
        raise PublishOutsideTransactionError()

    aggregate_type, aggregate_id = aggregate
    context = _context.get()
    actor = actor_ref or (context.actor_ref if context else None) or _ambient_actor_ref()
    correlation = correlation_id or (context.correlation_id if context else None) or _ambient_correlation_id()
    causation = causation_id or (context.causation_id if context else None)
    occurred = occurred_at or timezone.now()

    if (
        not isinstance(event_type, str)
        or len(event_type) > MAX_EVENT_TYPE_LENGTH
        or not _EVENT_TYPE.fullmatch(event_type)
    ):
        raise InvalidEventError("type: erwartet <bereich>.<objekt>.<ereignis> in Kleinbuchstaben")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= MAX_VERSION:
        raise InvalidEventError(f"version: ganze Zahl von 1 bis {MAX_VERSION} erwartet")
    if (
        not isinstance(aggregate_type, str)
        or len(aggregate_type) > MAX_AGGREGATE_TYPE_LENGTH
        or not _AGGREGATE_TYPE.fullmatch(aggregate_type)
    ):
        raise InvalidEventError("aggregate: kanonischer Typ wie Paper erwartet")
    if not isinstance(tenant, str) or not _TENANT_REF.fullmatch(tenant):
        raise InvalidEventError("tenant: erwartet session:<uuid>, org:<uuid> oder source:<uuid>")
    if visibility not in Visibility.values:
        raise InvalidEventError("visibility: unbekannte Sichtbarkeit")
    if operation not in Operation.values:
        raise InvalidEventError("operation: erwartet upsert, delete oder redact")
    if actor is not None:
        _check_actor_ref(actor)
    if occurred.tzinfo is None or occurred.utcoffset() is None:
        raise InvalidEventError("occurred_at: Zeitpunkt braucht eine Zeitzone")
    if not isinstance(payload, Mapping):
        raise InvalidEventError("payload: Objekt erwartet")

    event = Event(
        event_id=uuid.uuid4(),
        type=event_type,
        version=version,
        aggregate_type=aggregate_type,
        aggregate_id=_as_uuid(aggregate_id, "aggregate"),
        tenant_ref=tenant,
        body_id=_as_uuid(body_id, "body_id") if body_id is not None else None,
        visibility=str(visibility),
        operation=str(operation),
        occurred_at=occurred,
        actor_ref=actor,
        correlation_id=_as_uuid(correlation, "correlation_id"),
        causation_id=_as_uuid(causation, "causation_id") if causation is not None else None,
        payload=dict(payload),
    )

    if contract_validation_active():
        validator = _contract_validator
        if validator is None:
            raise ImproperlyConfigured(
                "EVENTS_VALIDATE_CONTRACTS ist an, aber es ist keine Vertragsprüfung eingehängt "
                "(hub.contracts in INSTALLED_APPS?)."
            )
        validator(envelope(event))

    event.save(force_insert=True, using=using)
    if event.operation == Operation.REDACT:
        # DSGVO: personenbezogene Nutzlasten zu den Personen neutralisieren, die das Ereignis nennt (Auftrag in
        # dieser Transaktion, nur mit EVENTS_REDACT_NEUTRALIZE und nur über die Standard-Datenbank;
        # apps.events.datenschutz)
        from .datenschutz import after_redact

        after_redact(event, using)
    return event


def envelope(event: Event) -> dict[str, Any]:
    """JSON-Darstellung der Hülle eines Ereignisses (Felder wie ``hub/contracts/envelope/v1.json``)."""
    return {
        "event_id": str(event.event_id),
        "type": event.type,
        "version": event.version,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": str(event.aggregate_id),
        "tenant_ref": event.tenant_ref,
        "body_id": str(event.body_id) if event.body_id is not None else None,
        "visibility": event.visibility,
        "operation": event.operation,
        "occurred_at": event.occurred_at.isoformat(),
        "actor_ref": event.actor_ref,
        "correlation_id": str(event.correlation_id),
        "causation_id": str(event.causation_id) if event.causation_id is not None else None,
        "payload": event.payload,
    }


def in_transaction(connection: BaseDatabaseWrapper) -> bool:
    """Ist auf der Verbindung ein ``transaction.atomic()``-Block offen, den der Code selbst geöffnet hat?

    Django-Tests legen um jeden Test einen eigenen Block. Er zählt nicht (wie bei
    ``atomic(durable=True)``), sonst fiele ein fehlendes ``atomic()`` erst im Betrieb auf.
    """
    if not connection.in_atomic_block:
        return False
    blocks = getattr(connection, "atomic_blocks", None)
    if not blocks:
        return True
    return any(not getattr(block, "_from_testcase", False) for block in blocks)


def _check_actor_ref(value: object) -> None:
    if not isinstance(value, str) or not _ACTOR_REF.fullmatch(value):
        raise InvalidEventError("actor_ref: erwartet user:<uuid> oder system:<auftrag>, nie einen Namen")


def _as_uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            pass
    raise InvalidEventError(f"{field}: UUID erwartet")

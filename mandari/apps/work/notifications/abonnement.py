# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``benachrichtigung`` (Issue #529): Benachrichtigungen als Konsument der Datendrehscheibe.

Statt die Benachrichtigung in der Anfrage anzulegen, meldet die Fachfunktion ein Ereignis
(``apps.work.tasks.ereignisse``); dieses Abonnement legt daraus die Benachrichtigungen an und reiht die
Mail als Auftrag ein. Regeln je Modul stehen in ``REGELN`` (Ereignistyp → Regel), zunächst für Aufgaben,
die häufigste Quelle von Benachrichtigungen.

- **Eine Benachrichtigung je Ereignis und Empfänger:** Der Schlüssel ``<event_id>:<mitgliedschaft>``
  steht eindeutig an der Benachrichtigung (``event_key``) und ist der Idempotenzschlüssel des
  Mailauftrags. Eine erneute Zustellung (Wiederholung, Nachspielen) legt nichts doppelt an.
- **Datenbank-Sicht:** Das Abonnement läuft transaktional; Benachrichtigungen, Mailauftrag und
  Cursor werden zusammen festgeschrieben.
- **Inhalte beim Eigentümer:** Das Ereignis nennt nur Kennungen; Titel und Empfänger liest die Regel aus
  der Aufgabe. Ist sie inzwischen gelöscht, entsteht nichts.
- **Schattenbetrieb:** Die Regel läuft in einem Sicherungspunkt ohne Mail, der wieder zurückgerollt
  wird; verglichen wird mit dem, was der bisherige Weg in der Anfrage angelegt hat
  (``mandari_notification_subscription_total{result="gleich"|"fehlt"}``).
- **Umschalten ohne Doppel und ohne Lücke:** Auch im Live-Betrieb legt die Regel keine Benachrichtigung an,
  die der bisherige Weg kurz zuvor schon angelegt hat (gleicher Empfänger, Typ und Bezug). Steht das
  Abonnement in der Datenbank noch auf ``schatten``, liest der Worker aber schon ``aktiv``, arbeitet der
  Handler wie aktiv (der bisherige Weg ist dann aus). Maßgeblich ist der Schalter des Workers: Er muss
  spätestens mit der Anwendung auf ``aktiv`` gehen, sonst vergleicht er nur, während die Anwendung schon
  nicht mehr benachrichtigt (siehe DEPLOYMENT.md, „Benachrichtigungen als Abonnement“).

Schalter ``WORK_NOTIFICATION_SUBSCRIPTION`` (``aus``, ``schatten``, ``aktiv``), siehe
``apps/work/subscribers.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from django.conf import settings
from django.db import transaction
from prometheus_client import Counter

if TYPE_CHECKING:
    from apps.events.models import Event
    from apps.events.registry import Delivery
    from apps.tenants.models import Membership

logger = logging.getLogger(__name__)

NAME: Final = "benachrichtigung"
BATCH: Final = 100
QUEUE: Final = "default"
#: Im Schatten zählt eine Benachrichtigung des bisherigen Weges, die so kurz vor dem Ereignis entstand
SCHATTEN_FENSTER: Final = timedelta(minutes=5)

ERGEBNIS = Counter(
    "mandari_notification_subscription_total",
    "Benachrichtigungen des Abonnements benachrichtigung je Ereignistyp und Ergebnis",
    ["type", "result"],
)

#: Regel: legt für ein Ereignis die Benachrichtigungen an und liefert sie; Optionen gehen an den
#: ``NotificationHub`` (``send_email``, ``direct_since``)
Regel = Callable[..., list[Any]]


def _uuid(wert: object) -> str:
    return str(wert or "")


def _actor(event: Event, organization_id: Any) -> Membership | None:
    """Mitgliedschaft des Auslösers in der Organisation (``actor_ref = user:<uuid>``)."""
    from apps.tenants.models import Membership

    ref = event.actor_ref or ""
    if not ref.startswith("user:"):
        return None
    return (
        Membership.objects.select_related("user")
        .filter(user_id=ref.removeprefix("user:"), organization_id=organization_id)
        .first()
    )


def _aufgabe(event: Event) -> Any:
    from apps.work.tasks.models import Task

    return (
        Task.objects.select_related("organization", "assigned_to__user", "created_by__user")
        .filter(pk=_uuid(event.payload.get("task")), organization_id=_uuid(event.payload.get("organization")))
        .first()
    )


def _schalter_aktiv() -> bool:
    return str(getattr(settings, "WORK_NOTIFICATION_SUBSCRIPTION", "aus")) == "aktiv"


def _hub() -> Any:
    from apps.work.notifications.services import NotificationHub

    return NotificationHub


def aufgabe_zugewiesen(event: Event, **optionen: Any) -> list[Any]:
    """``work.task.assigned``: die zugewiesene Mitgliedschaft (nicht, wer selbst zugewiesen hat)."""
    from apps.tenants.models import Membership

    task = _aufgabe(event)
    if task is None:
        return []
    assignee = (
        Membership.objects.select_related("user")
        .filter(pk=_uuid(event.payload.get("assignee")), organization_id=task.organization_id, is_active=True)
        .first()
    )
    if assignee is None:
        return []
    actor = _actor(event, task.organization_id)
    benachrichtigung = _hub().notify_task_assigned(task, assignee, actor, event_key=str(event.event_id), **optionen)
    return [benachrichtigung] if benachrichtigung else []


def aufgabe_erledigt(event: Event, **optionen: Any) -> list[Any]:
    """``work.task.completed``: Ersteller:in und Zuständige, außer wer erledigt hat.

    Ist der Auslöser nicht mehr als Mitgliedschaft zuzuordnen, gehen beide Benachrichtigungen raus (wie bisher).
    """
    task = _aufgabe(event)
    if task is None:
        return []
    actor = _actor(event, task.organization_id)
    return list(_hub().notify_task_completed(task, actor, event_key=str(event.event_id), **optionen))


def aufgabe_kommentiert(event: Event, **optionen: Any) -> list[Any]:
    """``work.task.commented``: Zuständige und Ersteller:in, außer wer kommentiert hat."""
    from apps.work.tasks.models import TaskActivity

    task = _aufgabe(event)
    if task is None:
        return []
    kommentar = (
        TaskActivity.objects.select_related("actor__user")
        .filter(pk=_uuid(event.payload.get("comment")), task=task, activity_type="comment")
        .first()
    )
    if kommentar is None:
        return []
    return list(_hub().notify_task_comment(task, kommentar, kommentar.actor, event_key=str(event.event_id), **optionen))


#: Regeln je Modul: Ereignistyp → Regel
REGELN: Final[Mapping[str, Regel]] = {
    "work.task.assigned": aufgabe_zugewiesen,
    "work.task.completed": aufgabe_erledigt,
    "work.task.commented": aufgabe_kommentiert,
}
TYPES: Final = tuple(REGELN)


def _vergleichen(event: Event, regel: Regel) -> None:
    """Schatten: Regel ohne Mail im Sicherungspunkt, zurückrollen, mit dem bisherigen Weg vergleichen."""
    punkt = transaction.savepoint()
    try:
        geplant = [(n.recipient_id, n.notification_type, n.metadata) for n in regel(event, send_email=False)]
    finally:
        transaction.savepoint_rollback(punkt)
    seit = event.occurred_at - SCHATTEN_FENSTER
    for empfaenger, art, metadata in geplant:
        vorhanden = _hub().already_direct(empfaenger, art, metadata, seit)
        ERGEBNIS.labels(type=event.type, result="gleich" if vorhanden else "fehlt").inc()
        if not vorhanden:
            logger.info("Schatten %s: Ereignis %s ergäbe eine Benachrichtigung, die fehlt", NAME, event.event_id)


def benachrichtigung(events: list[Event], delivery: Delivery) -> None:
    """Handler des Abonnements (idempotent je Ereignis und Empfänger)."""
    for event in events:
        regel = REGELN.get(event.type)
        if regel is None:
            continue
        # Schalter schon "aktiv", Abonnement in der Datenbank noch im Schatten: Der bisherige Weg ist aus,
        # vergleichen hieße benachrichtigen fällt aus; direct_since verhindert Doppel aus der Übergangszeit
        if delivery.shadow and not _schalter_aktiv():
            _vergleichen(event, regel)
            continue
        angelegt = regel(event, direct_since=event.occurred_at - SCHATTEN_FENSTER)
        ERGEBNIS.labels(type=event.type, result="angelegt" if angelegt else "keine").inc()

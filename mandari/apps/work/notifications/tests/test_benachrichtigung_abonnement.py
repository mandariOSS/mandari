# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``benachrichtigung`` (Issue #529): Benachrichtigungen entstehen aus Ereignissen.

Die Ereignisse laufen über die echte Zustellung (``deliver_batch``). Die Folgenummer vergibt im
Betrieb der Sequenzierer; hier ``nummerieren`` wie im Test der Zustellung.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.core import mail
from django.db.models import Max
from django.tasks import task_backends
from prometheus_client import REGISTRY

from apps.events import registry
from apps.events.dispatch import deliver_batch, ensure_subscription, rewind
from apps.events.models import Event, ParkedEvent
from apps.events.models import Task as TaskRow
from apps.events.registry import Subscriber, get
from apps.events.tasks_backend import JournalBackend
from apps.work import subscribers
from apps.work.notifications import abonnement
from apps.work.notifications.models import Notification, NotificationType
from apps.work.tasks import services
from apps.work.tasks.models import Task

pytestmark = pytest.mark.django_db


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Subscriber]]:
    """Eigenes Register: Was ein Test registriert, bleibt nicht für andere stehen."""
    eintraege: dict[str, Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    yield eintraege


@pytest.fixture
def ersteller(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view", "tasks.create"], email="ersteller@example.org")


@pytest.fixture
def zustaendig(org: Any, make_member: Any) -> Any:
    return make_member(org, ["tasks.view"], email="zustaendig@example.org")


def _abonnement(settings: Any, modus: str = "aktiv") -> Subscriber:
    settings.WORK_NOTIFICATION_SUBSCRIPTION = modus
    assert subscribers.register()
    spec = get(abonnement.NAME)
    ensure_subscription(spec)
    return spec


def _nummerieren() -> None:
    """Folgenummern wie der Sequenzierer vergeben (in der Reihenfolge der Erfassung)."""
    hoechste = Event.objects.aggregate(hoechste=Max("seq"))["hoechste"] or 0
    for event in Event.objects.filter(seq__isnull=True).order_by("id"):
        hoechste += 1
        Event.objects.filter(pk=event.pk).update(seq=hoechste)


def _zustellen(spec: Subscriber) -> int:
    _nummerieren()
    return deliver_batch(spec).delivered


def _aufgabe(org: Any, ersteller: Any, zustaendig: Any) -> Task:
    task = Task(title="Plakate kleben", assigned_to=zustaendig)
    return services.create_task(task, org, ersteller)


def _benachrichtigungen(empfaenger: Any, art: str) -> list[Notification]:
    return list(Notification.objects.filter(recipient=empfaenger, notification_type=art))


def _zaehler(typ: str, ergebnis: str) -> float:
    labels = {"type": typ, "result": ergebnis}
    return REGISTRY.get_sample_value("mandari_notification_subscription_total", labels) or 0.0


# -- aus: wie bisher -----------------------------------------------------------------------------


def test_ohne_schalter_kein_ereignis_und_benachrichtigung_in_der_anfrage(
    settings: Any, org: Any, ersteller: Any, zustaendig: Any
) -> None:
    settings.WORK_NOTIFICATION_SUBSCRIPTION = "aus"
    _aufgabe(org, ersteller, zustaendig)

    assert not Event.objects.exists()
    assert len(_benachrichtigungen(zustaendig, NotificationType.TASK_ASSIGNED)) == 1
    assert [m.to for m in mail.outbox] == [["zustaendig@example.org"]]


# -- aktiv: die Benachrichtigung entsteht im Abonnement --------------------------------------------


def test_zuweisung_wird_ereignis_und_erst_das_abonnement_benachrichtigt(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    spec = _abonnement(settings)
    task = _aufgabe(org, ersteller, zustaendig)

    [ereignis] = Event.objects.all()
    assert (ereignis.type, ereignis.aggregate_type, ereignis.aggregate_id) == ("work.task.assigned", "Task", task.pk)
    assert ereignis.visibility == "intern" and ereignis.tenant_ref == f"org:{org.pk}"
    assert ereignis.payload == {
        "task": str(task.pk),
        "organization": str(org.pk),
        "assignee": str(zustaendig.pk),
        "created": True,
    }
    assert ereignis.actor_ref == f"user:{ersteller.user_id}"
    assert not Notification.objects.exists() and not mail.outbox, "nicht mehr in der Anfrage"

    assert _zustellen(spec) == 1
    [benachrichtigung] = _benachrichtigungen(zustaendig, NotificationType.TASK_ASSIGNED)
    assert benachrichtigung.event_key == f"{ereignis.event_id}:{zustaendig.pk}"
    assert benachrichtigung.actor == ersteller and "Plakate kleben" in benachrichtigung.message
    assert [m.to for m in mail.outbox] == [["zustaendig@example.org"]]


def test_doppelte_zustellung_benachrichtigt_einmal(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    """Zustellung mindestens einmal: Nachspielen legt weder Benachrichtigung noch Mail doppelt an."""
    spec = _abonnement(settings)
    task = _aufgabe(org, ersteller, zustaendig)
    services.toggle_completion(task, zustaendig)
    services.add_comment(task, zustaendig, "Erledigt, Fotos folgen")
    assert _zustellen(spec) == 3
    vorher = sorted(Notification.objects.values_list("recipient_id", "notification_type", "event_key"))
    mails = len(mail.outbox)

    erstes = Event.objects.order_by("seq").first()
    assert erstes is not None and erstes.seq is not None
    rewind(abonnement.NAME, erstes.seq)
    assert deliver_batch(spec).delivered == 3

    assert sorted(Notification.objects.values_list("recipient_id", "notification_type", "event_key")) == vorher
    assert len(mail.outbox) == mails
    assert not ParkedEvent.objects.exists()


def test_erledigt_und_kommentiert(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    spec = _abonnement(settings)
    task = _aufgabe(org, ersteller, zustaendig)
    services.toggle_completion(task, zustaendig)
    kommentar = services.add_comment(task, zustaendig, "Fotos folgen")

    assert list(Event.objects.order_by("id").values_list("type", flat=True)) == [
        "work.task.assigned",
        "work.task.completed",
        "work.task.commented",
    ]
    assert Event.objects.get(type="work.task.commented").payload["comment"] == str(kommentar.pk)
    _zustellen(spec)

    # Wer erledigt bzw. kommentiert, wird nicht benachrichtigt
    assert len(_benachrichtigungen(ersteller, NotificationType.TASK_COMPLETED)) == 1
    assert len(_benachrichtigungen(ersteller, NotificationType.TASK_COMMENT)) == 1
    assert not _benachrichtigungen(zustaendig, NotificationType.TASK_COMPLETED)
    assert not _benachrichtigungen(zustaendig, NotificationType.TASK_COMMENT)


def test_geloeschte_aufgabe_ergibt_nichts(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    spec = _abonnement(settings)
    task = _aufgabe(org, ersteller, zustaendig)
    services.delete_task(task)

    assert _zustellen(spec) == 1
    assert not Notification.objects.exists() and not mail.outbox


def test_mailauftrag_mit_schluessel_je_ereignis_und_empfaenger(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    assert isinstance(task_backends["default"], JournalBackend)
    spec = _abonnement(settings)
    _aufgabe(org, ersteller, zustaendig)
    _zustellen(spec)

    [ereignis] = Event.objects.all()
    [auftrag] = TaskRow.objects.filter(queue="mail")
    assert auftrag.idempotency_key is not None
    assert auftrag.idempotency_key.endswith(f"{ereignis.event_id}:{zustaendig.pk}")


# -- schatten: vergleichen, nichts anlegen ------------------------------------------------------


def test_schatten_vergleicht_mit_dem_bisherigen_weg(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    spec = _abonnement(settings, "schatten")
    gleich = _zaehler("work.task.assigned", "gleich")
    _aufgabe(org, ersteller, zustaendig)

    assert Event.objects.filter(type="work.task.assigned").count() == 1
    assert len(_benachrichtigungen(zustaendig, NotificationType.TASK_ASSIGNED)) == 1, "bisheriger Weg"
    assert len(mail.outbox) == 1

    assert _zustellen(spec) == 1
    assert len(_benachrichtigungen(zustaendig, NotificationType.TASK_ASSIGNED)) == 1, "Schatten legt nichts an"
    assert len(mail.outbox) == 1
    assert _zaehler("work.task.assigned", "gleich") == gleich + 1


def test_schatten_meldet_fehlende(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    spec = _abonnement(settings, "schatten")
    fehlt = _zaehler("work.task.assigned", "fehlt")
    _aufgabe(org, ersteller, zustaendig)
    Notification.objects.all().delete()

    _zustellen(spec)
    assert _zaehler("work.task.assigned", "fehlt") == fehlt + 1
    assert not Notification.objects.exists()


def test_umschalten_ohne_doppelte_benachrichtigung(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    """Abonnement schon aktiv, Anwendung noch im Schatten: der bisherige Weg hat benachrichtigt, das Abonnement nicht."""
    spec = _abonnement(settings, "aktiv")
    settings.WORK_NOTIFICATION_SUBSCRIPTION = "schatten"
    _aufgabe(org, ersteller, zustaendig)
    assert len(_benachrichtigungen(zustaendig, NotificationType.TASK_ASSIGNED)) == 1

    assert _zustellen(spec) == 1
    assert len(_benachrichtigungen(zustaendig, NotificationType.TASK_ASSIGNED)) == 1
    assert len(mail.outbox) == 1

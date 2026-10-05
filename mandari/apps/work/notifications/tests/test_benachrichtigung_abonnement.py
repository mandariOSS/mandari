# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``benachrichtigung`` (Issue #529): Benachrichtigungen entstehen aus Ereignissen.

Die Ereignisse laufen über die echte Zustellung (``deliver_batch``). Die Folgenummer vergibt im
Betrieb der Sequenzierer; hier ``nummerieren`` wie im Test der Zustellung.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
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


def test_schalter_aktiv_abonnement_noch_im_schatten(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    """Anwendung schon aktiv (bisheriger Weg aus), Abonnement in der Datenbank noch im Schatten: keine Lücke."""
    spec = _abonnement(settings, "schatten")
    settings.WORK_NOTIFICATION_SUBSCRIPTION = "aktiv"
    _aufgabe(org, ersteller, zustaendig)
    assert not Notification.objects.exists()

    assert _zustellen(spec) == 1
    assert len(_benachrichtigungen(zustaendig, NotificationType.TASK_ASSIGNED)) == 1
    assert [m.to for m in mail.outbox] == [["zustaendig@example.org"]]


@pytest.fixture
def vertretung(org: Any, make_member: Any, zustaendig: Any) -> Any:
    from datetime import timedelta

    from django.utils import timezone

    from apps.work.organization.models import MemberAbsence

    vertreterin = make_member(org, ["tasks.view"], email="vertretung@example.org")
    heute = timezone.now().date()
    MemberAbsence.objects.create(
        organization=org,
        membership=zustaendig,
        start_date=heute - timedelta(days=1),
        end_date=heute + timedelta(days=1),
        deputy=vertreterin,
        notify_deputy=True,
    )
    return vertreterin


def test_vertretung_einmal_je_ereignis(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    ersteller: Any,
    zustaendig: Any,
    vertretung: Any,
) -> None:
    spec = _abonnement(settings)
    _aufgabe(org, ersteller, zustaendig)
    _zustellen(spec)

    [ereignis] = Event.objects.all()
    [weitergeleitet] = _benachrichtigungen(vertretung, NotificationType.TASK_ASSIGNED)
    assert weitergeleitet.title.startswith("[Vertretung]")
    assert weitergeleitet.event_key == f"{ereignis.event_id}:vertretung:{vertretung.pk}"
    assert sorted(m.to[0] for m in mail.outbox) == ["vertretung@example.org", "zustaendig@example.org"]

    assert ereignis.seq is not None
    rewind(abonnement.NAME, ereignis.seq)
    deliver_batch(spec)
    assert len(_benachrichtigungen(vertretung, NotificationType.TASK_ASSIGNED)) == 1
    assert len(mail.outbox) == 2


def test_schatten_versendet_auch_an_die_vertretung_nichts(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    ersteller: Any,
    zustaendig: Any,
    vertretung: Any,
) -> None:
    spec = _abonnement(settings, "schatten")
    _aufgabe(org, ersteller, zustaendig)
    mails = len(mail.outbox)  # bisheriger Weg: Zuständige und Vertretung

    _zustellen(spec)
    assert len(mail.outbox) == mails
    assert Notification.objects.filter(event_key__isnull=False).count() == 0


def test_erledigt_ohne_zuordenbaren_ausloeser_benachrichtigt_beide(
    settings: Any, leeres_register: dict[str, Subscriber], org: Any, ersteller: Any, zustaendig: Any
) -> None:
    spec = _abonnement(settings)
    task = _aufgabe(org, ersteller, zustaendig)
    services.toggle_completion(task, zustaendig)
    # Auslöser inzwischen ohne Mitgliedschaft in der Organisation
    Event.objects.filter(type="work.task.completed").update(actor_ref="user:00000000-0000-4000-8000-000000000001")

    _zustellen(spec)
    assert len(_benachrichtigungen(ersteller, NotificationType.TASK_COMPLETED)) == 1
    assert len(_benachrichtigungen(zustaendig, NotificationType.TASK_COMPLETED)) == 1


def test_protokoll_ohne_adressen_und_ohne_zurueckgerollte(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    ersteller: Any,
    zustaendig: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    spec = _abonnement(settings, "schatten")
    _aufgabe(org, ersteller, zustaendig)
    _zustellen(spec)
    assert "zustaendig@example.org" not in caplog.text
    assert "Notification sent" not in caplog.text


def test_abonnement_verlangt_auftraege_im_worker() -> None:
    """Ohne JournalBackend liefe der Mailversand in der Zustelltransaktion: Start verweigert."""
    from django.conf import settings as django_settings

    umgebung = {**os.environ, "WORK_NOTIFICATION_SUBSCRIPTION": "aktiv", "TASKS_BACKEND": ""}
    befehl = [sys.executable, "-c", "import mandari.settings"]
    ohne = subprocess.run(befehl, cwd=django_settings.BASE_DIR, env=umgebung, capture_output=True, text=True)
    assert ohne.returncode != 0 and "TASKS_BACKEND=journal" in ohne.stderr

    umgebung["TASKS_BACKEND"] = "journal"
    mit = subprocess.run(befehl, cwd=django_settings.BASE_DIR, env=umgebung, capture_output=True, text=True)
    assert mit.returncode == 0, mit.stderr[-500:]


def test_schluessel_als_teilindex_ohne_tabellensperre() -> None:
    """Eindeutig nur über gesetzte Schlüssel, ohne _like-Index; Index CONCURRENTLY außerhalb einer Transaktion."""
    import importlib

    from django.db.models import UniqueConstraint

    feld: Any = Notification._meta.get_field("event_key")
    assert not feld.unique and not feld.db_index
    [regel] = [c for c in Notification._meta.constraints if c.name == "uniq_notification_event_key"]
    assert isinstance(regel, UniqueConstraint) and regel.condition is not None
    migration = importlib.import_module("apps.work.migrations.0068_benachrichtigung_aus_ereignis")
    assert migration.Migration.atomic is False
    assert "CONCURRENTLY" in migration._concurrently.__code__.co_consts
    assert 'WHERE "event_key" IS NOT NULL' in migration.CREATE_SQL

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorhandene Hintergrundaufträge laufen mit beiden Tasks-Backends (Issue #506).

Ohne Umschalten führt Django sie sofort in der Anfrage aus; mit ``TASKS_BACKEND=journal`` landen sie
in ``events_task`` und der Runner arbeitet sie ab. Der Mailversand zu Benachrichtigungen lief
bisher immer synchron: Der Aufruf ``from django.tasks import enqueue`` gibt es nicht, der
ImportError führte still in den Rückfall.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.tasks import task_backends

from apps.events.models import Task as TaskRow
from apps.events.models import TaskStatus
from apps.events.task_runner import run_pending
from apps.events.tasks_backend import JournalBackend
from apps.work.notifications.models import Notification, NotificationType
from apps.work.notifications.services import NotificationHub
from apps.work.organization.models import DataExport
from apps.work.organization.services import start_data_export

pytestmark = pytest.mark.django_db

JOURNAL = "apps.events.tasks_backend.JournalBackend"
MAIL_TASK = "apps.work.background_tasks.send_notification_email_task"
EXPORT_TASK = "apps.work.background_tasks.generate_dsgvo_export_task"


@pytest.fixture
def journal(settings: Any) -> JournalBackend:
    """Wie in Produktion mit ``TASKS_BACKEND=journal``: Warteschlangen und Optionen aus settings.py."""
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": JOURNAL}}
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    return backend


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="empfaengerin@example.org")


def _benachrichtigen(mitglied: Any) -> Notification:
    notification = NotificationHub.send(
        recipient=mitglied,
        notification_type=NotificationType.TASK_ASSIGNED,
        title="Neue Aufgabe",
        message="Bitte ansehen",
    )
    assert notification is not None
    return notification


# -- Benachrichtigungsmail --------------------------------------------------------------------


def test_ohne_umschalten_geht_die_mail_sofort_raus(mitglied: Any) -> None:
    notification = _benachrichtigen(mitglied)

    assert [m.to for m in mail.outbox] == [["empfaengerin@example.org"]]
    notification.refresh_from_db()
    assert notification.email_sent
    assert not TaskRow.objects.exists()


def test_mit_journal_wird_die_mail_eingereiht_und_im_runner_versendet(journal: JournalBackend, mitglied: Any) -> None:
    notification = _benachrichtigen(mitglied)

    assert mail.outbox == [], "nicht mehr in der Anfrage"
    zeile = TaskRow.objects.get()
    assert (zeile.task_path, zeile.queue) == (MAIL_TASK, "mail")
    assert zeile.args == {"args": [str(notification.id)], "kwargs": {}}, "nur die Kennung, keine Inhalte"

    assert run_pending() == 1
    assert [m.to for m in mail.outbox] == [["empfaengerin@example.org"]]
    notification.refresh_from_db()
    assert notification.email_sent


def test_mailauftrag_ist_idempotent(journal: JournalBackend, mitglied: Any) -> None:
    """Zustellung mindestens einmal: Ein wiederholter Auftrag verschickt keine zweite Mail."""
    notification = _benachrichtigen(mitglied)
    NotificationHub._queue_email(notification)
    assert TaskRow.objects.count() == 2

    assert run_pending() == 2
    assert len(mail.outbox) == 1


def test_smtp_fehler_wird_im_runner_wiederholt(journal: JournalBackend, mitglied: Any) -> None:
    _benachrichtigen(mitglied)
    with mock.patch("apps.work.background_tasks.send_mail", side_effect=OSError("SMTP nicht erreichbar")):
        assert run_pending() == 1
    zeile = TaskRow.objects.get()
    assert (zeile.status, zeile.attempts, zeile.result_code) == (TaskStatus.WARTEND, 1, "builtins.OSError")


def test_smtp_fehler_ohne_umschalten_bricht_die_anfrage_nicht_ab(mitglied: Any) -> None:
    with mock.patch("apps.work.background_tasks.send_mail", side_effect=OSError("SMTP nicht erreichbar")):
        notification = _benachrichtigen(mitglied)
    notification.refresh_from_db()
    assert not notification.email_sent


# -- DSGVO-Export -----------------------------------------------------------------------------


@pytest.fixture
def media_root(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def test_export_mit_journal_im_runner(journal: JournalBackend, org: Any, mitglied: Any, media_root: Path) -> None:
    export = start_data_export(org, mitglied, "json")

    export.refresh_from_db()
    assert export.status == "pending", "nicht mehr in der Anfrage"
    zeile = TaskRow.objects.get()
    assert (zeile.task_path, zeile.queue, zeile.max_attempts) == (EXPORT_TASK, "default", 3)

    with mock.patch(
        "apps.work.organization.export_service.dsgvo_export_service.collect_user_data", return_value={"konto": {}}
    ):
        assert run_pending() == 1
    export.refresh_from_db()
    assert export.status == "completed"
    assert (media_root / export.file_path).is_file()


def test_export_ohne_umschalten_sofort(org: Any, mitglied: Any, media_root: Path) -> None:
    with mock.patch(
        "apps.work.organization.export_service.dsgvo_export_service.collect_user_data", return_value={"konto": {}}
    ):
        export = start_data_export(org, mitglied, "json")
    export.refresh_from_db()
    assert export.status == "completed"
    assert not DataExport.objects.exclude(pk=export.pk).exists()

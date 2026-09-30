# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorhandene Hintergrundaufträge laufen mit beiden Tasks-Backends (Issue #506).

Ohne Umschalten führt Django sie sofort in der Anfrage aus; mit ``TASKS_BACKEND=journal`` landen sie
in ``events_task`` und der Runner arbeitet sie ab. Der Mailversand zu Benachrichtigungen lief
bisher immer synchron: Der Aufruf ``from django.tasks import enqueue`` gibt es nicht, der
ImportError führte still in den Rückfall.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from django.core import mail
from django.tasks import task_backends
from django.utils import timezone

from apps.events.models import Task as TaskRow
from apps.events.models import TaskStatus
from apps.events.task_runner import claim, release_expired, run_pending
from apps.events.tasks_backend import JournalBackend
from apps.work.background_tasks import generate_dsgvo_export_task
from apps.work.notifications.models import Notification, NotificationType
from apps.work.notifications.services import NotificationHub
from apps.work.organization.models import DATA_EXPORT_FAILED_MESSAGE, DATA_EXPORT_STALE_AFTER, DataExport
from apps.work.organization.selectors import has_active_export
from apps.work.organization.services import ServiceError, start_data_export

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


# -- DSGVO-Export: Wiederaufnahme nach abgebrochenem Versuch ----------------------------------

DATEN: dict[str, Any] = {"konto": {}}


def _sammeln() -> Any:
    return mock.patch(
        "apps.work.organization.export_service.dsgvo_export_service.collect_user_data", return_value=DATEN
    )


def _abgebrochen(export: DataExport, vor: timedelta = timedelta(0)) -> None:
    """Zustand nach einem abgebrochenen Versuch: Export „in Arbeit“, aber kein Thread mehr daran."""
    DataExport.objects.filter(pk=export.pk).update(status="processing", started_at=timezone.now() - vor)


def test_export_wird_nach_absturz_im_naechsten_versuch_fertig(
    journal: JournalBackend, org: Any, mitglied: Any, media_root: Path
) -> None:
    """Absturz/Speichergrenze während der Erzeugung: Nach Ablauf der Sperre übernimmt der zweite Versuch."""
    export = start_data_export(org, mitglied, "pdf")
    zeile = TaskRow.objects.get()
    assert claim("default", journal.config) is not None
    _abgebrochen(export)
    TaskRow.objects.filter(pk=zeile.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    assert release_expired() == 1
    TaskRow.objects.filter(pk=zeile.pk).update(run_after=timezone.now() - timedelta(seconds=1))

    with (
        _sammeln(),
        mock.patch("apps.work.organization.export_service.dsgvo_export_service._html_to_pdf", return_value=b"%PDF-1.7"),
    ):
        assert run_pending() == 1
    export.refresh_from_db()
    assert export.status == "completed"
    assert (media_root / export.file_path).read_bytes() == b"%PDF-1.7"
    zeile.refresh_from_db()
    assert (zeile.status, zeile.attempts) == (TaskStatus.ERLEDIGT, 2)


def test_export_in_arbeit_wird_spaeter_erneut_versucht(
    journal: JournalBackend, org: Any, mitglied: Any, media_root: Path
) -> None:
    """Wurde der Versuch beim erzwungenen Ende freigegeben (zählt nicht), wartet der erste Versuch ab."""
    export = start_data_export(org, mitglied, "json")
    _abgebrochen(export)

    with _sammeln():
        assert run_pending() == 1
    export.refresh_from_db()
    assert export.status == "processing"
    zeile = TaskRow.objects.get()
    assert (zeile.status, zeile.result_code) == (TaskStatus.WARTEND, "apps.work.background_tasks.ExportBusyError")

    TaskRow.objects.update(run_after=timezone.now() - timedelta(seconds=1))
    with _sammeln():
        assert run_pending() == 1
    export.refresh_from_db()
    assert export.status == "completed"


def test_fertiger_export_wird_bei_wiederholung_nicht_neu_erstellt(
    journal: JournalBackend, org: Any, mitglied: Any, media_root: Path
) -> None:
    """Zustellung mindestens einmal: Ein erneuter Auftrag zu einem fertigen Export ändert nichts."""
    export = start_data_export(org, mitglied, "json")
    with _sammeln():
        assert run_pending() == 1
    export.refresh_from_db()
    fertig_am = export.completed_at

    TaskRow.objects.update(status=TaskStatus.WARTEND, run_after=timezone.now() - timedelta(seconds=1))
    with _sammeln() as sammeln:
        assert run_pending() == 1
    sammeln.assert_not_called()
    export.refresh_from_db()
    assert (export.status, export.completed_at) == ("completed", fertig_am)


def test_abgebrochener_export_sperrt_keinen_neuen(org: Any, mitglied: Any, media_root: Path) -> None:
    """Auskunftsrecht: Ein seit der Frist hängender Export blockiert das Mitglied nicht dauerhaft."""
    with _sammeln():
        alt = start_data_export(org, mitglied, "json")
    _abgebrochen(alt, vor=DATA_EXPORT_STALE_AFTER + timedelta(minutes=1))
    assert not has_active_export(org, mitglied)

    with _sammeln():
        neu = start_data_export(org, mitglied, "json")
    alt.refresh_from_db()
    neu.refresh_from_db()
    assert (alt.status, alt.error_message) == ("failed", DATA_EXPORT_FAILED_MESSAGE)
    assert neu.status == "completed"


def test_laufender_export_sperrt_einen_zweiten(org: Any, mitglied: Any, media_root: Path) -> None:
    with _sammeln():
        export = start_data_export(org, mitglied, "json")
    _abgebrochen(export, vor=DATA_EXPORT_STALE_AFTER - timedelta(minutes=1))
    assert has_active_export(org, mitglied)
    with pytest.raises(ServiceError):
        start_data_export(org, mitglied, "json")
    export.refresh_from_db()
    assert export.status == "processing"


def test_abgebrochener_export_wird_auch_im_ersten_versuch_uebernommen(
    org: Any, mitglied: Any, media_root: Path
) -> None:
    """Ohne Runner (sofort ausführendes Backend) zählt nur das Alter des abgebrochenen Versuchs."""
    export = DataExport.objects.create(organization=org, membership=mitglied, export_format="json")
    _abgebrochen(export, vor=DATA_EXPORT_STALE_AFTER + timedelta(minutes=1))
    with _sammeln():
        cast(Any, generate_dsgvo_export_task).enqueue(str(export.id))
    export.refresh_from_db()
    assert export.status == "completed"


def test_frist_fuer_abgebrochene_exporte_liegt_ueber_der_zeitgrenze(journal: JournalBackend) -> None:
    zeitgrenze = journal.config.timeout_for(EXPORT_TASK, "default")
    assert 2 * timedelta(seconds=zeitgrenze) <= DATA_EXPORT_STALE_AFTER

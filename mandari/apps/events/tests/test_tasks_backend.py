# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tasks-Backend ``JournalBackend`` auf ``events_task`` (Issue #506): Einreihen, Umschalten, Idempotenz."""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast
from unittest import mock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.tasks import TaskResultStatus, default_task_backend, task_backends
from django.tasks.backends.immediate import ImmediateBackend
from django.tasks.exceptions import InvalidTask, TaskResultDoesNotExist
from django.tasks.signals import task_enqueued
from django.utils import timezone

from apps.events.models import Task as TaskRow
from apps.events.models import TaskStatus
from apps.events.tasks_backend import (
    DEFAULT_CONCURRENCY,
    QUEUES,
    JournalBackend,
    JournalOptions,
    enqueue_once,
)
from apps.events.tests import auftraege
from apps.events.tests.auftraege import journal_einstellungen

T = cast(Any, auftraege)


@pytest.fixture(autouse=True)
def _zuruecksetzen() -> None:
    auftraege.zuruecksetzen()


@pytest.fixture
def journal(settings: Any) -> JournalBackend:
    settings.TASKS = journal_einstellungen()
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    return backend


# -- Umschalten -----------------------------------------------------------------


def test_standard_bleibt_das_sofort_ausfuehrende_backend() -> None:
    assert isinstance(task_backends["default"], ImmediateBackend)


@pytest.mark.django_db
def test_mit_sofort_ausfuehrendem_backend_laeuft_der_auftrag_in_der_anfrage() -> None:
    ergebnis = T.merken.enqueue("a")
    assert ergebnis.status == TaskResultStatus.SUCCESSFUL
    assert auftraege.aufrufe == [("merken", ("a", 0))]
    assert not TaskRow.objects.exists()


@pytest.mark.django_db
def test_mit_journal_backend_wird_nur_eingereiht(journal: JournalBackend) -> None:
    ergebnis = T.merken.enqueue("a", zusatz=3)

    assert ergebnis.status == TaskResultStatus.READY
    assert auftraege.aufrufe == [], "ausgeführt wird im Runner, nicht in der Anfrage"
    zeile = TaskRow.objects.get()
    assert str(zeile.pk) == ergebnis.id
    assert zeile.task_path == "apps.events.tests.auftraege.merken"
    assert zeile.queue == "default"
    assert zeile.args == {"args": ["a"], "kwargs": {"zusatz": 3}}
    assert zeile.status == TaskStatus.WARTEND
    assert zeile.attempts == 0
    assert zeile.max_attempts == 8
    assert zeile.idempotency_key is None
    assert ergebnis.enqueued_at is not None


class TestEinstellungAusDerUmgebung:
    """``TASKS_BACKEND`` wählt das Backend; ohne Angabe bleibt es beim sofort ausführenden."""

    @pytest.fixture(autouse=True)
    def _einstellungen_wiederherstellen(self) -> Iterator[None]:
        yield
        importlib.reload(importlib.import_module("mandari.settings"))

    @staticmethod
    def _backend(umgebung: dict[str, str]) -> str:
        with mock.patch.dict(os.environ, umgebung, clear=False):
            if "TASKS_BACKEND" not in umgebung:
                os.environ.pop("TASKS_BACKEND", None)
            modul = importlib.reload(importlib.import_module("mandari.settings"))
        return str(modul.TASKS["default"]["BACKEND"])

    def test_ohne_angabe_sofort(self) -> None:
        assert self._backend({}) == "django.tasks.backends.immediate.ImmediateBackend"

    def test_journal(self) -> None:
        assert self._backend({"TASKS_BACKEND": "journal"}) == "apps.events.tasks_backend.JournalBackend"

    def test_importpfad(self) -> None:
        pfad = "django.tasks.backends.dummy.DummyBackend"
        assert self._backend({"TASKS_BACKEND": pfad}) == pfad

    def test_beide_backends_kennen_alle_warteschlangen(self) -> None:
        with mock.patch.dict(os.environ, {"TASKS_BACKEND": "immediate"}, clear=False):
            modul = importlib.reload(importlib.import_module("mandari.settings"))
        assert modul.TASKS["default"]["QUEUES"] == list(QUEUES)


# -- Einreihen -------------------------------------------------------------------


@pytest.mark.django_db
def test_warteschlange_prioritaet_und_frueheste_ausfuehrung(journal: JournalBackend) -> None:
    spaeter = timezone.now() + timedelta(hours=1)
    T.mail_merken.enqueue("m")
    T.wichtig.enqueue("w")
    T.merken.using(run_after=spaeter).enqueue("s")

    zeilen = {z.task_path.rsplit(".", 1)[1]: z for z in TaskRow.objects.all()}
    assert zeilen["mail_merken"].queue == "mail"
    assert zeilen["wichtig"].priority == 10
    assert zeilen["merken"].run_after == spaeter


@pytest.mark.django_db
def test_unbekannte_warteschlange_wird_abgelehnt(journal: JournalBackend) -> None:
    with pytest.raises(InvalidTask):
        T.merken.using(queue_name="gibt-es-nicht").enqueue("a")


@pytest.mark.django_db
def test_argumente_muessen_json_sein(journal: JournalBackend) -> None:
    with pytest.raises(TypeError):
        T.merken.enqueue(object())
    assert not TaskRow.objects.exists()


@pytest.mark.django_db
def test_zurueckgerollte_transaktion_hinterlaesst_keinen_auftrag(journal: JournalBackend) -> None:
    """Der Auftrag existiert genau dann, wenn die fachliche Änderung festgeschrieben ist (ADR A4)."""
    with pytest.raises(RuntimeError), transaction.atomic():
        T.merken.enqueue("a")
        assert TaskRow.objects.count() == 1
        raise RuntimeError("fachlicher Fehler")
    assert not TaskRow.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_signal_task_enqueued_erst_nach_dem_commit(journal: JournalBackend) -> None:
    gemeldet: list[str] = []

    def empfaenger(sender: Any, task_result: Any, **kwargs: Any) -> None:
        gemeldet.append(task_result.id)

    task_enqueued.connect(empfaenger)
    try:
        with transaction.atomic():
            ergebnis = T.merken.enqueue("a")
            assert gemeldet == []
        assert gemeldet == [ergebnis.id]
        with pytest.raises(RuntimeError), transaction.atomic():
            T.merken.enqueue("b")
            raise RuntimeError("zurückrollen")
        assert gemeldet == [ergebnis.id]
    finally:
        task_enqueued.disconnect(empfaenger)


# -- Idempotenzschlüssel -----------------------------------------------------------


@pytest.mark.django_db
def test_doppelter_idempotenzschluessel_legt_keinen_zweiten_auftrag_an(journal: JournalBackend) -> None:
    erster = enqueue_once(T.merken, "export:1", "a")
    zweiter = enqueue_once(T.merken, "export:1", "a")

    assert erster.id == zweiter.id
    assert TaskRow.objects.count() == 1
    assert TaskRow.objects.get().idempotency_key == "apps.events.tests.auftraege.merken:export:1"


@pytest.mark.django_db
def test_idempotenzschluessel_gilt_auch_nach_dem_lauf(journal: JournalBackend) -> None:
    erster = enqueue_once(T.merken, "k", "a")
    TaskRow.objects.update(status=TaskStatus.ERLEDIGT, finished_at=timezone.now(), result_code="ok")

    zweiter = enqueue_once(T.merken, "k", "a")
    assert zweiter.id == erster.id
    assert zweiter.status == TaskResultStatus.SUCCESSFUL
    assert TaskRow.objects.count() == 1


@pytest.mark.django_db
def test_idempotenzschluessel_gilt_je_auftragstyp(journal: JournalBackend) -> None:
    enqueue_once(T.merken, "k", "a")
    enqueue_once(T.mail_merken, "k", "a")
    assert TaskRow.objects.count() == 2


@pytest.mark.django_db
def test_doppelter_schluessel_bricht_die_umgebende_transaktion_nicht_ab(journal: JournalBackend) -> None:
    with transaction.atomic():
        enqueue_once(T.merken, "k", "a")
        enqueue_once(T.merken, "k", "a")
        T.merken.enqueue("b")
    assert TaskRow.objects.count() == 2


@pytest.mark.django_db
def test_leerer_idempotenzschluessel_ist_ein_fehler(journal: JournalBackend) -> None:
    with pytest.raises(ValueError):
        enqueue_once(T.merken, "", "a")


@pytest.mark.django_db
def test_ohne_journal_fuehrt_enqueue_once_sofort_aus() -> None:
    enqueue_once(T.merken, "k", "a")
    enqueue_once(T.merken, "k", "a")
    assert len(auftraege.aufrufe) == 2, "ohne Aufzeichnung gibt es keine Idempotenz"


# -- Ergebnis abfragen ---------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "erwartet"),
    [
        (TaskStatus.WARTEND, TaskResultStatus.READY),
        (TaskStatus.LAEUFT, TaskResultStatus.RUNNING),
        (TaskStatus.ERLEDIGT, TaskResultStatus.SUCCESSFUL),
        (TaskStatus.FEHLGESCHLAGEN, TaskResultStatus.FAILED),
        (TaskStatus.TOT, TaskResultStatus.FAILED),
    ],
)
def test_get_result_bildet_den_status_ab(journal: JournalBackend, status: str, erwartet: TaskResultStatus) -> None:
    ergebnis = T.merken.enqueue("a", zusatz=1)
    TaskRow.objects.update(status=status, attempts=2, result_code="builtins.RuntimeError")

    geladen = T.merken.get_result(ergebnis.id)
    assert geladen.status == erwartet
    assert geladen.args == ["a"]
    assert geladen.kwargs == {"zusatz": 1}
    assert geladen.attempts == 2
    if erwartet == TaskResultStatus.FAILED:
        assert geladen.errors[0].exception_class_path == "builtins.RuntimeError"
        assert geladen.errors[0].traceback == "", "Stacktraces stehen nur im Protokoll"
    else:
        assert geladen.errors == []


@pytest.mark.django_db
def test_refresh_liest_den_neuen_stand(journal: JournalBackend) -> None:
    ergebnis = T.merken.enqueue("a")
    TaskRow.objects.update(status=TaskStatus.ERLEDIGT, finished_at=timezone.now(), result_code="ok")
    ergebnis.refresh()
    assert ergebnis.status == TaskResultStatus.SUCCESSFUL
    assert ergebnis.finished_at is not None


@pytest.mark.django_db
@pytest.mark.parametrize("kennung", ["keine-uuid", "00000000-0000-0000-0000-000000000000"])
def test_unbekanntes_ergebnis(journal: JournalBackend, kennung: str) -> None:
    with pytest.raises(TaskResultDoesNotExist):
        default_task_backend.get_result(kennung)


# -- Einstellungen -------------------------------------------------------------------------


def test_standardwerte_der_warteschlangen() -> None:
    optionen = JournalOptions.from_settings({}, frozenset(QUEUES))
    assert dict(optionen.concurrency) == dict(DEFAULT_CONCURRENCY)
    assert optionen.concurrency == {"default": 4, "mail": 2, "index": 2, "ocr": 1, "ai": 1, "adapter": 2}
    assert optionen.max_attempts == 8


def test_zeitgrenze_und_versuche_je_auftragstyp() -> None:
    optionen = JournalOptions.from_settings(
        {"timeouts": {"mail": 30}, "tasks": {"x.y": {"timeout": 5, "max_attempts": 2}}, "max_attempts": 4},
        frozenset(QUEUES),
    )
    assert optionen.timeout_for("x.y", "mail") == 5
    assert optionen.timeout_for("x.z", "mail") == 30
    assert optionen.timeout_for("x.z", "default") == 300
    assert optionen.max_attempts_for("x.y") == 2
    assert optionen.max_attempts_for("x.z") == 4


@pytest.mark.django_db
def test_versuche_je_auftragstyp_landen_in_der_zeile(settings: Any) -> None:
    settings.TASKS = journal_einstellungen(tasks={"apps.events.tests.auftraege.merken": {"max_attempts": 2}})
    T.merken.enqueue("a")
    T.mail_merken.enqueue("b")
    assert dict(TaskRow.objects.values_list("queue", "max_attempts")) == {"default": 2, "mail": 8}


@pytest.mark.parametrize(
    "optionen",
    [
        {"concurrency": {"unbekannt": 1}},
        {"concurrency": {"default": -1}},
        {"concurrency": {"default": "4"}},
        {"timeouts": {"mail": 0}},
        {"tasks": {"x.y": {"zeitgrenze": 5}}},
        {"max_attempts": 0},
        {"max_memory_mb": -1},
        {"tippfehler": 1},
    ],
)
def test_fehlerhafte_einstellungen_werden_abgelehnt(optionen: dict[str, Any]) -> None:
    with pytest.raises(ImproperlyConfigured):
        JournalOptions.from_settings(optionen, frozenset(QUEUES))

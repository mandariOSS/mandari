# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runner für Aufträge (Issue #506): Holen, Ergebnis, Wiederholung, Sperren, Zeitgrenzen, Neustart."""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
import uuid
from datetime import timedelta
from typing import Any, cast
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.tasks import task_backends
from django.utils import timezone
from prometheus_client import REGISTRY

from apps.events import task_runner
from apps.events.models import Task as TaskRow
from apps.events.models import TaskStatus
from apps.events.task_metrics import TaskCollector
from apps.events.task_runner import (
    CODE_ABORTED,
    CODE_LOCK_EXPIRED,
    CODE_TIMEOUT,
    CODE_UNKNOWN_TASK,
    Outcome,
    StopReason,
    TaskRunner,
    claim,
    execute,
    finish,
    purge_finished,
    release,
    release_expired,
    renew_locks,
    retry_delay,
    run_pending,
)
from apps.events.tasks_backend import JournalBackend, JournalOptions
from apps.events.tests import auftraege
from apps.events.tests.auftraege import journal_einstellungen
from apps.events.tests.hilfen import nur_postgres

T = cast(Any, auftraege)
PFAD = "apps.events.tests.auftraege"


@pytest.fixture(autouse=True)
def _zuruecksetzen() -> None:
    auftraege.zuruecksetzen()


@pytest.fixture
def journal(settings: Any) -> JournalBackend:
    settings.TASKS = journal_einstellungen()
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    return backend


def _runner(backend: JournalBackend, **kwargs: Any) -> TaskRunner:
    kwargs.setdefault("poll_interval", 0.05)
    kwargs.setdefault("burst", True)
    return TaskRunner(backend.config, list(backend.config.concurrency), **kwargs)


def _laufen(runner: TaskRunner, stop: threading.Event | None = None, **kwargs: Any) -> StopReason:
    return runner.run(stop or threading.Event(), **kwargs)


def _zeile(ergebnis: Any) -> TaskRow:
    return TaskRow.objects.get(pk=ergebnis.id)


def _wert(name: str, **labels: str) -> float:
    """Stand einer Metrik dieses Prozesses (0, solange sie für die Labels nichts gemessen hat)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


# -- Holen ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_holen_sperrt_und_zaehlt_den_versuch(journal: JournalBackend) -> None:
    ergebnis = T.merken.enqueue("a", zusatz=2)

    geholt = claim("default", journal.config)
    assert geholt is not None
    assert (geholt.args, geholt.kwargs, geholt.attempt, geholt.timeout) == (["a"], {"zusatz": 2}, 1, 300)
    zeile = _zeile(ergebnis)
    assert zeile.status == TaskStatus.LAEUFT
    assert zeile.attempts == 1
    assert zeile.locked_until is not None and zeile.locked_until > timezone.now()
    assert claim("default", journal.config) is None, "ein laufender Auftrag wird nicht zweimal geholt"


@pytest.mark.django_db
def test_reihenfolge_prioritaet_dann_faelligkeit(journal: JournalBackend) -> None:
    T.merken.enqueue("zuerst eingereiht")
    T.wichtig.enqueue("höhere Priorität")
    T.merken.using(run_after=timezone.now() + timedelta(hours=1)).enqueue("noch nicht fällig")

    reihenfolge = []
    while (geholt := claim("default", journal.config)) is not None:
        reihenfolge.append(geholt.args[0])
    assert reihenfolge == ["höhere Priorität", "zuerst eingereiht"]


@pytest.mark.django_db
def test_holen_nur_aus_der_eigenen_warteschlange(journal: JournalBackend) -> None:
    T.mail_merken.enqueue("m")
    assert claim("default", journal.config) is None
    assert claim("mail", journal.config) is not None


# -- Ergebnis --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_erfolg(journal: JournalBackend) -> None:
    ergebnis = T.merken.enqueue("a", zusatz=1)
    geholt = claim("default", journal.config)
    assert geholt is not None
    gemessen = _wert("mandari_tasks_duration_seconds_count", queue="default")

    ausgang = execute(geholt)
    assert _wert("mandari_tasks_duration_seconds_count", queue="default") == gemessen + 1
    assert ausgang.ok
    assert finish(geholt, ausgang) == TaskStatus.ERLEDIGT
    assert auftraege.aufrufe == [("merken", ("a", 1))]
    zeile = _zeile(ergebnis)
    assert (zeile.status, zeile.result_code, zeile.locked_until) == (TaskStatus.ERLEDIGT, "ok", None)
    assert zeile.finished_at is not None


@pytest.mark.django_db
def test_fehler_wird_mit_wachsender_wartezeit_wiederholt_und_ist_dann_tot(settings: Any) -> None:
    settings.TASKS = journal_einstellungen(tasks={f"{PFAD}.scheitern": {"max_attempts": 3}})
    config = cast(JournalBackend, task_backends["default"]).config
    ergebnis = T.scheitern.enqueue("x")

    for versuch in (1, 2, 3):
        geholt = claim("default", config)
        assert geholt is not None and geholt.attempt == versuch
        assert _zeile(ergebnis).result_code is None, "Ergebniscode gilt je Versuch"
        fehler = _wert("mandari_tasks_failed_total", queue="default", grund="fehler")
        ausgang = execute(geholt)
        assert _wert("mandari_tasks_failed_total", queue="default", grund="fehler") == fehler + 1
        assert ausgang == Outcome("builtins.RuntimeError")
        status = finish(geholt, ausgang)
        zeile = _zeile(ergebnis)
        if versuch < 3:
            assert status == TaskStatus.WARTEND
            assert zeile.run_after > timezone.now(), "erst nach einer Wartezeit erneut"
            assert zeile.result_code == "builtins.RuntimeError"
            TaskRow.objects.filter(pk=zeile.pk).update(run_after=timezone.now() - timedelta(seconds=1))
        else:
            assert status == TaskStatus.TOT
            assert zeile.finished_at is not None
    assert claim("default", config) is None


def test_wartezeit_waechst_und_ist_begrenzt() -> None:
    assert timedelta(seconds=10) <= retry_delay(1) <= timedelta(seconds=12)
    assert timedelta(seconds=20) <= retry_delay(2) <= timedelta(seconds=24)
    assert retry_delay(30) <= timedelta(hours=1, minutes=12)


@pytest.mark.django_db
def test_endgueltiger_fehler_wird_nicht_wiederholt(journal: JournalBackend) -> None:
    ergebnis = T.endgueltig.enqueue("x")
    geholt = claim("default", journal.config)
    assert geholt is not None
    endgueltig = _wert("mandari_tasks_failed_total", queue="default", grund="endgueltig")
    assert finish(geholt, execute(geholt)) == TaskStatus.FEHLGESCHLAGEN
    assert _wert("mandari_tasks_failed_total", queue="default", grund="endgueltig") == endgueltig + 1
    assert _zeile(ergebnis).result_code == "apps.events.tasks_backend.PermanentTaskError"


@pytest.mark.django_db
def test_unbekannter_auftragstyp_wird_wiederholt(journal: JournalBackend) -> None:
    """Während eines Updates kann ein älterer Worker einen neuen Auftragstyp holen."""
    TaskRow.objects.create(queue="default", task_path=f"{PFAD}.gibt_es_nicht", args={"args": [], "kwargs": {}})
    geholt = claim("default", journal.config)
    assert geholt is not None
    ausgang = execute(geholt)
    assert ausgang == Outcome(CODE_UNKNOWN_TASK)
    assert finish(geholt, ausgang) == TaskStatus.WARTEND


@pytest.mark.django_db
def test_funktion_ohne_task_dekorator_wird_nicht_ausgefuehrt(journal: JournalBackend) -> None:
    TaskRow.objects.create(queue="default", task_path=f"{PFAD}.keine_task", args={"args": ["x"], "kwargs": {}})
    geholt = claim("default", journal.config)
    assert geholt is not None
    assert finish(geholt, execute(geholt)) == TaskStatus.FEHLGESCHLAGEN
    assert auftraege.aufrufe == []


@pytest.mark.django_db
def test_kontext_kennt_versuch_und_kennung(journal: JournalBackend) -> None:
    ergebnis = T.mit_kontext.enqueue("k")
    TaskRow.objects.update(attempts=2)
    geholt = claim("default", journal.config)
    assert geholt is not None
    assert execute(geholt).ok
    assert auftraege.aufrufe == [("mit_kontext", ("k", 3, ergebnis.id))]


@pytest.mark.django_db
def test_ereignisse_eines_auftrags_tragen_seine_kennung_als_korrelation(journal: JournalBackend) -> None:
    """``publish()`` im Auftrag: Korrelation = Kennung des Auftrags, Auslöser ``system:<auftrag>`` (#510)."""
    ergebnis = T.kontext_merken.enqueue("k")
    geholt = claim("default", journal.config)
    assert geholt is not None
    assert execute(geholt).ok
    assert auftraege.aufrufe == [("kontext_merken", ("k", uuid.UUID(str(ergebnis.id)), "system:kontext_merken"))]


@pytest.mark.django_db
def test_fremder_versuch_ueberschreibt_nichts(journal: JournalBackend) -> None:
    """Abgrenzung: Hat ein anderer Runner nach Ablauf der Sperre übernommen, gilt das alte Ergebnis nicht."""
    ergebnis = T.merken.enqueue("a")
    alt = claim("default", journal.config)
    assert alt is not None
    TaskRow.objects.update(status=TaskStatus.WARTEND, locked_until=None)
    neu = claim("default", journal.config)
    assert neu is not None and neu.attempt == 2

    assert finish(alt, Outcome("builtins.RuntimeError")) is None
    assert _zeile(ergebnis).status == TaskStatus.LAEUFT
    assert finish(neu, task_runner.SUCCESS) == TaskStatus.ERLEDIGT


# -- Sperren -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_haengender_auftrag_wird_nach_ablauf_der_sperre_erneut_ausgefuehrt(journal: JournalBackend) -> None:
    """ADR A4: Ein Auftrag, dessen Runner abgestürzt ist, läuft nach ``locked_until`` erneut."""
    ergebnis = T.merken.enqueue("a")
    assert claim("default", journal.config) is not None
    assert release_expired() == 0, "Sperre noch gültig"

    TaskRow.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
    abgelaufen = _wert("mandari_tasks_failed_total", queue="default", grund="sperre_abgelaufen")
    assert release_expired() == 1
    zeile = _zeile(ergebnis)
    assert (zeile.status, zeile.result_code, zeile.attempts) == (TaskStatus.WARTEND, CODE_LOCK_EXPIRED, 1)
    assert _wert("mandari_tasks_failed_total", queue="default", grund="sperre_abgelaufen") == abgelaufen + 1
    assert zeile.run_after > timezone.now()

    TaskRow.objects.update(run_after=timezone.now() - timedelta(seconds=1))
    assert run_pending() == 1
    assert auftraege.aufrufe == [("merken", ("a", 0))]
    assert _zeile(ergebnis).status == TaskStatus.ERLEDIGT


@pytest.mark.django_db
def test_abgelaufene_sperre_nach_dem_letzten_versuch_ist_tot(journal: JournalBackend) -> None:
    ergebnis = T.merken.enqueue("a")
    TaskRow.objects.update(status=TaskStatus.LAEUFT, attempts=8, locked_until=timezone.now() - timedelta(seconds=1))
    assert release_expired() == 1
    assert _zeile(ergebnis).status == TaskStatus.TOT


@pytest.mark.django_db
def test_verlaengern_nur_fuer_den_eigenen_versuch(journal: JournalBackend) -> None:
    T.merken.enqueue("a")
    geholt = claim("default", journal.config, lock_ttl=timedelta(seconds=5))
    assert geholt is not None
    vorher = TaskRow.objects.get().locked_until
    assert vorher is not None

    assert renew_locks([geholt], timedelta(minutes=5)) == 1
    nachher = TaskRow.objects.get().locked_until
    assert nachher is not None and nachher > vorher
    fremd = dataclasses.replace(geholt, attempt=7)
    assert renew_locks([fremd], timedelta(minutes=5)) == 0
    assert renew_locks([]) == 0


@pytest.mark.django_db
def test_freigeben_beim_erzwungenen_ende_zaehlt_den_versuch_nicht(journal: JournalBackend) -> None:
    ergebnis = T.merken.enqueue("a")
    geholt = claim("default", journal.config)
    assert geholt is not None
    assert release(geholt)
    zeile = _zeile(ergebnis)
    assert (zeile.status, zeile.attempts, zeile.result_code) == (TaskStatus.WARTEND, 0, CODE_ABORTED)
    assert not release(geholt), "nur der eigene laufende Versuch"


# -- Aufbewahrung -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_aufbewahrung_14_tage_erledigt_90_tage_tot(journal: JournalBackend) -> None:
    jetzt = timezone.now()

    def anlegen(status: str, alter: timedelta | None) -> uuid.UUID:
        zeile = TaskRow.objects.create(
            queue="default",
            task_path=f"{PFAD}.merken",
            args={"args": [], "kwargs": {}},
            status=status,
            finished_at=jetzt - alter if alter is not None else None,
        )
        return zeile.pk

    bleiben = {
        anlegen(TaskStatus.ERLEDIGT, timedelta(days=13)),
        anlegen(TaskStatus.TOT, timedelta(days=30)),
        anlegen(TaskStatus.FEHLGESCHLAGEN, timedelta(days=89)),
        anlegen(TaskStatus.WARTEND, None),
    }
    anlegen(TaskStatus.ERLEDIGT, timedelta(days=15))
    anlegen(TaskStatus.TOT, timedelta(days=91))
    anlegen(TaskStatus.FEHLGESCHLAGEN, timedelta(days=100))

    assert purge_finished(batch=1) == 3
    assert set(TaskRow.objects.values_list("pk", flat=True)) == bleiben


# -- Koordinator ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_runner_arbeitet_alle_warteschlangen_ab(journal: JournalBackend) -> None:
    T.merken.enqueue("a")
    T.mail_merken.enqueue("m")
    T.merken.enqueue("b")

    runner = _runner(journal)
    assert _laufen(runner) == StopReason.LEER
    assert sorted(aufruf[0] for aufruf in auftraege.aufrufe) == ["mail_merken", "merken", "merken"]
    assert set(TaskRow.objects.values_list("status", flat=True)) == {TaskStatus.ERLEDIGT}
    assert runner.completed == 3


@pytest.mark.django_db
def test_warteschlange_arbeitet_mit_ihrer_parallelitaet(journal: JournalBackend) -> None:
    """Zwei Aufträge treffen sich nur, wenn sie gleichzeitig laufen (``default`` hat Parallelität 4)."""
    T.treffen.enqueue("1")
    T.treffen.enqueue("2")
    assert _laufen(_runner(journal)) == StopReason.LEER
    assert set(TaskRow.objects.values_list("status", flat=True)) == {TaskStatus.ERLEDIGT}


@pytest.mark.django_db
def test_parallelitaet_null_schaltet_eine_warteschlange_ab(journal: JournalBackend) -> None:
    T.mail_merken.enqueue("m")
    runner = _runner(journal, concurrency={"mail": 0})
    assert runner.slot_count == sum(journal.config.concurrency.values()) - 2
    assert _laufen(runner) == StopReason.LEER
    assert TaskRow.objects.get().status == TaskStatus.WARTEND


@pytest.mark.django_db
def test_neustart_nach_n_auftraegen(journal: JournalBackend) -> None:
    for kennung in "abc":
        T.merken.enqueue(kennung)

    runner = _runner(journal, max_tasks=2, burst=False)
    assert _laufen(runner) == StopReason.ANZAHL
    assert runner.completed == 2
    assert TaskRow.objects.filter(status=TaskStatus.WARTEND).count() == 1, "nicht mehr geholt als erlaubt"


@pytest.mark.django_db
def test_neustart_bei_ueberschrittener_speichergrenze(journal: JournalBackend) -> None:
    for kennung in "ab":
        T.merken.enqueue(kennung)
    belegt = iter([100, 300, 300, 300, 300, 300])
    runner = _runner(
        journal, max_memory_mb=200, burst=False, concurrency={"default": 1}, rss=lambda: next(belegt) * 1024 * 1024
    )
    assert _laufen(runner) == StopReason.SPEICHER
    assert runner.completed == 1
    assert TaskRow.objects.filter(status=TaskStatus.WARTEND).count() == 1


@pytest.mark.django_db
def test_speichergrenze_unter_dem_grundbedarf_wird_ausgeschaltet(journal: JournalBackend) -> None:
    T.merken.enqueue("a")
    runner = _runner(journal, max_memory_mb=50, rss=lambda: 100 * 1024 * 1024)
    assert _laufen(runner) == StopReason.LEER
    assert runner.max_memory_bytes == 0
    assert _wert("mandari_worker_rss_bytes", role="tasks") == 100 * 1024 * 1024


@pytest.mark.django_db
def test_zeitgrenze_je_auftragstyp_fuehrt_zum_neustart(settings: Any) -> None:
    """Der hängende Auftrag gilt als gescheitert; ein gleichzeitig laufender darf noch fertig werden.

    Bis der Prozess endet, bleibt der hängende Auftrag gesperrt: Kein anderer Runner führt ihn parallel
    zum noch laufenden Thread ein zweites Mal aus. Erst danach läuft die Sperre ab.
    """
    settings.TASKS = journal_einstellungen(tasks={f"{PFAD}.haengen": {"timeout": 0.3}})
    backend = cast(JournalBackend, task_backends["default"])
    haengt = T.haengen.enqueue("h")
    T.langsam.enqueue(1.5)
    zeitgrenzen = _wert("mandari_tasks_failed_total", queue="ai", grund="zeitgrenze")
    abgelaufen = _wert("mandari_tasks_failed_total", queue="ai", grund="sperre_abgelaufen")
    try:
        runner = _runner(backend, burst=False, lock_ttl=timedelta(seconds=1), renew_interval=0.1)
        beginn = time.monotonic()
        assert _laufen(runner) == StopReason.ZEITGRENZE
        assert time.monotonic() - beginn < 10
        zeile = _zeile(haengt)
        assert (zeile.status, zeile.result_code, zeile.attempts) == (TaskStatus.LAEUFT, CODE_TIMEOUT, 1)
        assert zeile.locked_until is not None and zeile.locked_until > timezone.now(), "bis zum Ende verlängert"
        TaskRow.objects.filter(pk=zeile.pk).update(run_after=timezone.now() - timedelta(minutes=1))
        assert claim("ai", backend.config) is None, "auch nach der Wartezeit nicht neben dem hängenden Thread"
        assert TaskRow.objects.get(task_path=f"{PFAD}.langsam").status == TaskStatus.ERLEDIGT
        assert ("langsam", 1.5) in auftraege.aufrufe
        assert _wert("mandari_tasks_failed_total", queue="ai", grund="zeitgrenze") == zeitgrenzen + 1

        # Prozess neu gestartet: Die Sperre läuft ab, der Auftrag wird wiederholt
        TaskRow.objects.filter(pk=zeile.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        assert release_expired() == 1
        zeile = _zeile(haengt)
        assert (zeile.status, zeile.result_code, zeile.attempts) == (TaskStatus.WARTEND, CODE_TIMEOUT, 1)
        assert _wert("mandari_tasks_failed_total", queue="ai", grund="sperre_abgelaufen") == abgelaufen, "einmal"
    finally:
        auftraege.FREIGABE.set()


@pytest.mark.django_db
def test_nach_der_zeitgrenze_gilt_ein_spaetes_ergebnis(settings: Any) -> None:
    """Wird der hängende Thread vor dem Neustart doch fertig, zählt sein Ergebnis; keine Wiederholung."""
    settings.TASKS = journal_einstellungen(tasks={f"{PFAD}.haengen": {"timeout": 0.3}})
    backend = cast(JournalBackend, task_backends["default"])
    haengt = T.haengen.enqueue("h")
    T.langsam.enqueue(1.5)
    vermerken = task_runner.mark_timed_out

    def vermerken_und_freigeben(auftrag: task_runner.ClaimedTask) -> bool:
        ergebnis = vermerken(auftrag)
        auftraege.FREIGABE.set()
        return ergebnis

    try:
        with mock.patch.object(task_runner, "mark_timed_out", side_effect=vermerken_und_freigeben):
            assert _laufen(_runner(backend, burst=False)) == StopReason.ZEITGRENZE
        zeile = _zeile(haengt)
        assert (zeile.status, zeile.result_code, zeile.attempts) == (TaskStatus.ERLEDIGT, "ok", 1)
        assert ("haengen", "h") in auftraege.aufrufe
    finally:
        auftraege.FREIGABE.set()


@pytest.mark.django_db
def test_stopp_holt_nichts_mehr(journal: JournalBackend) -> None:
    T.merken.enqueue("a")
    stop = threading.Event()
    stop.set()
    assert _laufen(_runner(journal, burst=False), stop) == StopReason.STOP
    assert TaskRow.objects.get().status == TaskStatus.WARTEND


@pytest.mark.django_db
def test_erzwungenes_ende_gibt_laufende_auftraege_frei(journal: JournalBackend) -> None:
    ergebnis = T.haengen.enqueue("h")
    stop = threading.Event()
    force = threading.Event()

    def erzwingen() -> None:
        auftraege.HAENGT.wait(10)
        stop.set()
        force.set()

    helfer = threading.Thread(target=erzwingen)
    helfer.start()
    try:
        assert _laufen(_runner(journal, burst=False), stop, force=force) == StopReason.STOP
        zeile = _zeile(ergebnis)
        assert (zeile.status, zeile.attempts, zeile.result_code) == (TaskStatus.WARTEND, 0, CODE_ABORTED)
    finally:
        auftraege.FREIGABE.set()
        helfer.join(10)


@pytest.mark.django_db
def test_runner_gibt_beim_start_abgelaufene_sperren_frei(journal: JournalBackend) -> None:
    ergebnis = T.merken.enqueue("a")
    TaskRow.objects.update(status=TaskStatus.LAEUFT, attempts=1, locked_until=timezone.now() - timedelta(minutes=1))
    _laufen(_runner(journal))
    assert _zeile(ergebnis).status == TaskStatus.WARTEND, "freigegeben, aber erst nach der Wartezeit fällig"


@pytest.mark.django_db
def test_heartbeat_datei(journal: JournalBackend, tmp_path: Any) -> None:
    datei = tmp_path / "heartbeat"
    _laufen(_runner(journal, heartbeat_file=datei))
    assert datei.exists()


def test_neustartgruende() -> None:
    assert {grund for grund in StopReason if grund.restart} == {
        StopReason.ANZAHL,
        StopReason.SPEICHER,
        StopReason.ZEITGRENZE,
    }


def test_speicher_ist_messbar_oder_unbekannt() -> None:
    belegt = task_runner.current_rss_bytes()
    assert belegt is None or belegt > 0


# -- Befehl ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_befehl_burst_arbeitet_ab(journal: JournalBackend) -> None:
    T.merken.enqueue("a")
    T.mail_merken.enqueue("m")
    call_command("events_tasks", "--burst", "--interval", "0.05")
    assert set(TaskRow.objects.values_list("status", flat=True)) == {TaskStatus.ERLEDIGT}


@pytest.mark.django_db
def test_befehl_nur_gewaehlte_warteschlangen(journal: JournalBackend) -> None:
    T.merken.enqueue("a")
    T.mail_merken.enqueue("m")
    call_command("events_tasks", "--burst", "--interval", "0.05", "--queues", "mail", "--concurrency", "mail=1")
    assert dict(TaskRow.objects.values_list("queue", "status")) == {
        "default": TaskStatus.WARTEND,
        "mail": TaskStatus.ERLEDIGT,
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "argumente",
    [
        ["--queues", "gibt-es-nicht"],
        ["--queues", "mail", "--concurrency", "default=2"],
        ["--concurrency", "mail=zwei"],
        ["--max-tasks", "-1"],
        ["--queues", "mail", "--concurrency", "mail=0"],
    ],
)
def test_befehl_prueft_argumente(journal: JournalBackend, argumente: list[str]) -> None:
    with pytest.raises(CommandError):
        call_command("events_tasks", "--burst", *argumente)


@pytest.mark.django_db
def test_befehl_laeuft_auch_vor_dem_umschalten() -> None:
    """Der Worker darf starten, bevor die Webprozesse auf JournalBackend umschalten (Rollout)."""
    TaskRow.objects.create(queue="default", task_path=f"{PFAD}.merken", args={"args": ["a"], "kwargs": {}})
    call_command("events_tasks", "--burst", "--interval", "0.05")
    assert TaskRow.objects.get().status == TaskStatus.ERLEDIGT


# -- Nur PostgreSQL: SKIP LOCKED ------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_gesperrte_zeile_wird_uebersprungen(journal: JournalBackend, pg_verbindungen: Any) -> None:
    """Zwei Runner holen nie denselben Auftrag: Eine gesperrte Zeile wird übersprungen, nicht abgewartet."""
    nur_postgres()
    erster = T.merken.enqueue("a")
    zweiter = T.merken.enqueue("b")
    andere = pg_verbindungen(autocommit=False)
    andere.execute("SELECT id FROM events_task WHERE id = %s FOR UPDATE", (uuid.UUID(erster.id),))

    beginn = time.monotonic()
    geholt = claim("default", journal.config)
    assert time.monotonic() - beginn < 5
    assert geholt is not None and str(geholt.id) == zweiter.id
    andere.rollback()


@pytest.mark.django_db(transaction=True)
def test_zwei_runner_fuehren_jeden_auftrag_genau_einmal_aus(journal: JournalBackend) -> None:
    nur_postgres()
    for nummer in range(20):
        T.merken.enqueue(str(nummer))

    def zweiter_runner() -> None:
        try:
            _laufen(_runner(journal))
        finally:
            connection.close()

    faden = threading.Thread(target=zweiter_runner)
    faden.start()
    _laufen(_runner(journal))
    faden.join(30)

    assert sorted(aufruf[1][0] for aufruf in auftraege.aufrufe) == sorted(str(n) for n in range(20))
    assert set(TaskRow.objects.values_list("status", "attempts")) == {(TaskStatus.ERLEDIGT, 1)}


def test_optionen_des_runners_aus_der_konfiguration() -> None:
    config = JournalOptions.from_settings({"max_tasks_per_process": 7, "max_memory_mb": 64}, frozenset({"default"}))
    runner = TaskRunner(config, ["default"])
    assert (runner.max_tasks, runner.max_memory_bytes, runner.slot_count) == (7, 64 * 1024 * 1024, 4)
    runner = TaskRunner(config, ["default"], max_tasks=0, max_memory_mb=0, concurrency={"default": 2})
    assert (runner.max_tasks, runner.max_memory_bytes, runner.slot_count) == (0, 0, 2)


@pytest.mark.django_db
def test_befehl_endet_ohne_neustart_mit_exit_code_75(journal: JournalBackend) -> None:
    T.merken.enqueue("a")
    with pytest.raises(SystemExit) as beendet:
        call_command("events_tasks", "--max-tasks", "1", "--no-restart", "--interval", "0.05")
    assert beendet.value.code == 75
    assert TaskRow.objects.get().status == TaskStatus.ERLEDIGT


@pytest.mark.django_db
def test_befehl_ersetzt_sich_beim_neustart_selbst(journal: JournalBackend) -> None:
    """Neustart per exec: gleiche Prozessnummer, kein Neustart-Zähler im Container."""
    T.merken.enqueue("a")

    class ErsetztError(Exception):
        pass

    with (
        mock.patch("apps.events.management.commands.events_tasks.os.name", "posix"),
        mock.patch("apps.events.management.commands.events_tasks.os.execv", side_effect=ErsetztError) as execv,
        pytest.raises(ErsetztError),
    ):
        call_command("events_tasks", "--max-tasks", "1", "--interval", "0.05")
    programm, argumente = execv.call_args.args
    assert programm == sys.executable
    assert argumente[0] == sys.executable


# -- Metriken ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_metriken_zeigen_rueckstand_laufende_und_tote(journal: JournalBackend) -> None:
    T.merken.enqueue("a")
    T.merken.enqueue("b")
    T.merken.using(run_after=timezone.now() + timedelta(hours=1)).enqueue("noch nicht fällig")
    TaskRow.objects.filter(pk=T.mail_merken.enqueue("m").id).update(status=TaskStatus.LAEUFT)
    for status in (TaskStatus.TOT, TaskStatus.FEHLGESCHLAGEN, TaskStatus.ERLEDIGT):
        TaskRow.objects.create(
            queue="ai", task_path=f"{PFAD}.haengen", args={}, status=status, finished_at=timezone.now()
        )
    # Älter als 24 Stunden: Der Alarm soll nicht bis zum Löschen nach 90 Tagen anstehen
    TaskRow.objects.create(
        queue="ai",
        task_path=f"{PFAD}.haengen",
        args={},
        status=TaskStatus.TOT,
        finished_at=timezone.now() - timedelta(hours=25),
    )

    werte = {
        (probe.name, probe.labels["queue"]): probe.value
        for familie in TaskCollector().collect()
        for probe in familie.samples
    }
    assert werte[("mandari_tasks_queued", "default")] == 2
    assert werte[("mandari_tasks_oldest_queued_seconds", "default")] >= 0
    assert werte[("mandari_tasks_running", "mail")] == 1
    assert werte[("mandari_tasks_dead", "ai")] == 2
    assert ("mandari_tasks_queued", "mail") not in werte

# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Worker mit wählbaren Rollen in einem Prozess (Issue #508, ``apps.events.worker``).

Die Tests mit Ersatzrollen laufen mit SQLite und PostgreSQL; dort arbeitet nur der Testfaden mit
der Datenbank. Tests mit echten Rollen in eigenen Fäden brauchen PostgreSQL (CI), weil die Fäden
festgeschriebene Daten sehen müssen.
"""

from __future__ import annotations

import json
import signal
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.tasks import task_backends
from prometheus_client import REGISTRY

from apps.common import db_connections
from apps.events import leases, presence, subscriber
from apps.events.dispatch import Dispatcher
from apps.events.models import NOTIFY_CHANNEL, Event, Lease, Subscription, TaskStatus, WorkerProcess
from apps.events.models import Task as TaskRow
from apps.events.registry import Delivery, Subscriber
from apps.events.scheduler import LEASE_NAME as SCHEDULER_LEASE
from apps.events.sequencer import SEQUENCED_CHANNEL
from apps.events.task_runner import StopReason
from apps.events.tasks_backend import JournalBackend
from apps.events.tests import auftraege
from apps.events.tests.auftraege import journal_einstellungen
from apps.events.tests.hilfen import ereignis_anlegen, nummeriert, nur_postgres
from apps.events.worker import (
    POOL_RESERVE,
    ROLES,
    ExitReason,
    Heartbeat,
    Worker,
    connection_budget,
)

T = cast(Any, auftraege)
FRIST = 60.0


@pytest.fixture(autouse=True)
def _zuruecksetzen() -> None:
    auftraege.zuruecksetzen()


def _bis(bedingung: Callable[[], bool], frist: float = FRIST, hinweis: str = "") -> None:
    ende = time.monotonic() + frist
    while not bedingung():
        if time.monotonic() > ende:
            raise AssertionError(f"Zeitüberschreitung: {hinweis}")
        time.sleep(0.02)


class Schleife:
    """Ersatzrolle ohne Datenbank: meldet jeden Durchlauf, bis ``stop`` gesetzt ist.

    ``haengt``: meldet sich nach dem ersten Durchlauf nicht mehr (bis ``stop``). ``fehler``: wirft
    im ersten Durchlauf.
    """

    def __init__(self, *, haengt: bool = False, fehler: BaseException | None = None) -> None:
        self.haengt = haengt
        self.fehler = fehler
        self.durchlaeufe = 0
        self.beendet = threading.Event()

    def run(
        self,
        stop: threading.Event,
        interval: float = 0.01,
        wake: threading.Event | None = None,
        beat: Callable[[], None] | None = None,
    ) -> None:
        try:
            while not stop.is_set():
                if beat is not None and not (self.haengt and self.durchlaeufe):
                    beat()
                self.durchlaeufe += 1
                if self.fehler is not None:
                    raise self.fehler
                stop.wait(interval)
        finally:
            self.beendet.set()


class Runner:
    """Ersatz für den Runner: ``neustart_nach`` simuliert den Neustartwunsch, ``zaeh`` laufende Aufträge."""

    def __init__(self, *, neustart_nach: float | None = None, zaeh: bool = False) -> None:
        self.on_beat: Callable[[], None] | None = None
        self.neustart_nach = neustart_nach
        self.zaeh = zaeh
        self.erzwungen = False
        self.geweckt = threading.Event()

    def wake(self) -> None:
        self.geweckt.set()

    def run(self, stop: threading.Event, force: threading.Event | None = None) -> StopReason:
        beginn = time.monotonic()
        while True:
            if self.on_beat is not None:
                self.on_beat()
            if force is not None and force.is_set():
                self.erzwungen = True
                return StopReason.STOP
            if stop.is_set() and not self.zaeh:
                return StopReason.STOP
            if self.neustart_nach is not None and time.monotonic() - beginn >= self.neustart_nach:
                return StopReason.ANZAHL
            time.sleep(0.01)


def _worker(**kwargs: Any) -> Worker:
    kwargs.setdefault("supervise_interval", 0.01)
    kwargs.setdefault("heartbeat_interval", 0.02)
    kwargs.setdefault("listen", False)
    for intervall in ("sequencer_interval", "dispatch_interval", "scheduler_interval"):
        kwargs.setdefault(intervall, 0.01)
    for rolle in ("sequencer", "scheduler", "runner", "dispatcher"):
        if rolle in kwargs:
            kwargs[rolle] = cast(Any, kwargs[rolle])
    return Worker(**kwargs)


def _stoppen_wenn(
    stop: threading.Event, bedingung: Callable[[], bool], danach: Callable[[], None] | None = None
) -> None:
    """Hilfsfaden ohne Datenbank: setzt ``stop``, sobald ``bedingung`` gilt."""

    def warten() -> None:
        try:
            _bis(bedingung, frist=10.0)
            if danach is not None:
                danach()
        finally:
            stop.set()

    threading.Thread(target=warten, daemon=True).start()


# -- ohne Datenbank -------------------------------------------------------------------------


def test_verbindungsbudget_ist_summe_der_parallelitaet_plus_reserve() -> None:
    alle = connection_budget(ROLES, subscriptions=3, task_slots=12, listener=True, metrics=True)
    # Hauptfaden + Reserve, Sequenzierer, 3 Abonnements, Koordinator + 12 Plätze, Zeitpläne, Listener, /metrics
    assert alle == 1 + POOL_RESERVE + 1 + 3 + 13 + 1 + 1 + 1
    assert connection_budget(["tasks"], task_slots=1) == 1 + POOL_RESERVE + 2
    assert connection_budget(["dispatch"], subscriptions=0) == 1 + POOL_RESERVE


def test_lebenszeichen_zaehlt_das_aelteste_je_rolle() -> None:
    jetzt = [100.0]
    herz = Heartbeat(clock=lambda: jetzt[0])
    assert herz.age(["a"]) is None
    herz.beat("a")
    jetzt[0] = 103.0
    herz.beat("b")
    jetzt[0] = 104.0
    assert herz.age(["a", "b"]) == 4.0
    assert herz.age(["b"]) == 1.0
    assert herz.age(["a", "c"]) is None


# -- Rollen, Lebenszeichen, Beenden (Ersatzrollen) ------------------------------------------


@pytest.mark.django_db
def test_rollen_arbeiten_in_eigenen_faeden_mit_heartbeat_und_anmeldung(tmp_path: Path) -> None:
    datei = tmp_path / "worker.heartbeat"
    sequenzierer, planer, runner = Schleife(), Schleife(), Runner()
    worker = _worker(
        sequencer=sequenzierer, scheduler=planer, runner=runner, queues=["mail"], heartbeat_file=datei, holder="w1"
    )
    stop = threading.Event()
    gesehen: dict[str, Any] = {}

    def pruefen() -> None:
        gesehen["zustand"] = worker.role_states()
        gesehen["faeden"] = {faden.name for faden in threading.enumerate()}

    _stoppen_wenn(stop, lambda: datei.exists() and sequenzierer.durchlaeufe > 3 and planer.durchlaeufe > 3, pruefen)
    with mock.patch.object(presence, "announce", wraps=presence.announce) as angemeldet:
        assert worker.run(stop) == ExitReason.STOP

    assert set(gesehen["zustand"]) == {"sequencer", "tasks", "scheduler"}
    assert all(zustand.up for zustand in gesehen["zustand"].values())
    assert {"events-worker-sequencer", "events-worker-tasks", "events-worker-scheduler"} <= gesehen["faeden"]
    angemeldet.assert_called_with("w1", ("sequencer", "tasks", "scheduler"), ("mail",))
    assert not WorkerProcess.objects.exists(), "beim Beenden abgemeldet"
    assert sequenzierer.beendet.is_set() and planer.beendet.is_set()


@pytest.mark.django_db
def test_haengende_rolle_stoppt_heartbeat_und_anmeldung(tmp_path: Path) -> None:
    datei = tmp_path / "worker.heartbeat"
    haengt = Schleife(haengt=True)
    worker = _worker(sequencer=haengt, scheduler=Schleife(), heartbeat_file=datei, stale_after=0.2)
    stop = threading.Event()
    gesehen: dict[str, Any] = {}

    def pruefen() -> None:
        gesehen["zustand"] = worker.role_states()
        gesehen["health"] = worker.health()
        datei.unlink(missing_ok=True)
        time.sleep(0.2)  # mehrere Runden des Hauptfadens
        gesehen["neu"] = datei.exists()

    _stoppen_wenn(stop, lambda: not worker.role_states().get("sequencer", mock.Mock(up=True)).up, pruefen)
    with mock.patch.object(presence, "announce") as angemeldet:
        worker.run(stop)

    assert gesehen["zustand"]["sequencer"].up is False
    assert gesehen["zustand"]["scheduler"].up is True
    assert gesehen["health"] == (False, {"roles": {"sequencer": {"ok": False}, "scheduler": {"ok": True}}})
    assert gesehen["neu"] is False, "Heartbeat-Datei wird nicht mehr erneuert"
    assert angemeldet.call_count >= 1, "angemeldet, solange alle Rollen arbeiteten"


@pytest.mark.django_db
def test_ausfall_einer_rolle_beendet_alle_rollen_mit_fehler() -> None:
    planer = Schleife()
    worker = _worker(sequencer=Schleife(fehler=RuntimeError("kaputt")), scheduler=planer)
    stop = threading.Event()
    assert worker.run(stop) == ExitReason.FAILURE
    assert stop.is_set()
    assert planer.beendet.is_set()


@pytest.mark.django_db
def test_neustartwunsch_des_runners_haelt_die_anderen_rollen_bis_dahin_nicht_an() -> None:
    planer = Schleife()
    runner = Runner(neustart_nach=0.3)
    worker = _worker(scheduler=planer, runner=runner)
    beginn = time.monotonic()
    assert worker.run(threading.Event()) == ExitReason.RESTART
    assert time.monotonic() - beginn >= 0.3
    assert planer.durchlaeufe >= 10, "Zeitpläne liefen weiter, während der Runner auf seine Aufträge wartete"
    assert planer.beendet.is_set()


@pytest.mark.django_db
def test_beenden_gibt_laufende_auftraege_nach_der_frist_frei() -> None:
    runner = Runner(zaeh=True)
    worker = _worker(runner=runner, shutdown_timeout=0.2)
    stop = threading.Event()
    stop.set()
    beginn = time.monotonic()
    assert worker.run(stop) == ExitReason.STOP
    assert runner.erzwungen
    assert 0.2 <= time.monotonic() - beginn < 5.0


@pytest.mark.django_db
def test_zweites_signal_gibt_sofort_frei() -> None:
    runner = Runner(zaeh=True)
    worker = _worker(runner=runner, shutdown_timeout=30.0)
    stop, force = threading.Event(), threading.Event()

    def zwei_signale() -> None:
        time.sleep(0.1)
        stop.set()
        time.sleep(0.1)
        force.set()

    threading.Thread(target=zwei_signale, daemon=True).start()
    beginn = time.monotonic()
    assert worker.run(stop, force) == ExitReason.STOP
    assert runner.erzwungen
    assert time.monotonic() - beginn < 10.0


@pytest.mark.django_db
def test_ein_listener_fuer_sequenzierer_und_zustellung(leeres_register: dict[str, Subscriber]) -> None:
    subscriber("test.worker", types=["test.*"])(lambda events, delivery: None)
    zusteller = Dispatcher(list(leeres_register.values()))
    worker = _worker(sequencer=Schleife(), dispatcher=zusteller, listen=True)
    stop = threading.Event()
    stop.set()
    with (
        mock.patch("apps.events.worker.start_listener") as listener,
        mock.patch.object(Worker, "_anmelden"),
        mock.patch.object(Worker, "_abmelden"),
    ):
        worker.run(stop)
    listener.assert_called_once()
    rueckrufe = listener.call_args.args[0]
    assert set(rueckrufe) == {NOTIFY_CHANNEL, SEQUENCED_CHANNEL}
    assert rueckrufe[SEQUENCED_CHANNEL] == [zusteller.wake]


# -- /metrics und /health -----------------------------------------------------------------


def _abrufen(port: int, pfad: str, **kopf: str) -> tuple[int, bytes]:
    anfrage = urllib.request.Request(f"http://127.0.0.1:{port}{pfad}", headers=kopf)
    try:
        with urllib.request.urlopen(anfrage, timeout=10) as antwort:  # noqa: S310 – lokaler Testserver
            return int(antwort.status), bytes(antwort.read())
    except urllib.error.HTTPError as fehler:
        return int(fehler.code), bytes(fehler.read())


@pytest.mark.django_db
def test_metrics_und_health_des_workers(settings: Any) -> None:
    settings.METRICS_TOKEN = "geheim-" + "x" * 32
    worker = _worker(runner=Runner(), scheduler=Schleife(), metrics_port=0, metrics_addr="127.0.0.1")
    stop = threading.Event()
    gesehen: dict[str, Any] = {}

    def abrufen() -> None:
        port = worker.metrics_server_port
        assert port is not None
        gesehen["health"] = _abrufen(port, "/health")
        gesehen["metrics"] = _abrufen(port, "/metrics")
        settings.METRICS_ALLOWED_NETWORKS = ["192.0.2.0/24"]
        gesehen["fremd"] = _abrufen(port, "/metrics")
        gesehen["token"] = _abrufen(port, "/metrics", Authorization=f"Bearer {settings.METRICS_TOKEN}")
        gesehen["sonst"] = _abrufen(port, "/admin/")

    _stoppen_wenn(stop, lambda: worker.metrics_server_port is not None, abrufen)
    with mock.patch(
        "apps.common.db_connections.close_thread_connections", wraps=db_connections.close_thread_connections
    ) as freigegeben:
        worker.run(stop)

    status, inhalt = gesehen["health"]
    assert status == 200
    assert json.loads(inhalt) == {"status": "ok", "roles": {"tasks": {"ok": True}, "scheduler": {"ok": True}}}
    status, inhalt = gesehen["metrics"]
    assert status == 200
    text = inhalt.decode()
    assert 'mandari_worker_role_up{role="tasks"} 1.0' in text
    assert 'mandari_worker_role_beat_age_seconds{role="scheduler"}' in text
    assert gesehen["fremd"][0] == 404, "außerhalb der erlaubten Netze nicht einmal bestätigt"
    assert gesehen["token"][0] == 200
    assert gesehen["sonst"][0] == 404
    assert freigegeben.called, "der Abruf gibt seine Datenbankverbindung zurück"
    assert REGISTRY.get_sample_value("mandari_worker_role_up", {"role": "tasks"}) is None, "nach dem Ende kein Wert"


def test_health_meldet_503_ohne_arbeitende_rolle() -> None:
    from apps.common.metrics_server import metrics_app

    antworten: list[str] = []

    def start_response(status: str, kopf: list[tuple[str, str]], *_: Any) -> Callable[[bytes], object]:
        antworten.append(status)
        return lambda daten: None

    app = metrics_app(lambda: (False, {"roles": {"tasks": {"ok": False}}}))
    inhalt = b"".join(app({"PATH_INFO": "/health", "REQUEST_METHOD": "GET"}, start_response))
    assert antworten == ["503 Service Unavailable"]
    assert json.loads(inhalt)["status"] == "error"


# -- Lebenszeichen in der Datenbank ---------------------------------------------------------


@pytest.mark.django_db
def test_anmeldung_erneuern_abmelden_und_aufraeumen() -> None:
    presence.announce("w1", ["sequencer", "tasks"], [])
    presence.announce("w2", ["tasks"], ["ocr"])
    presence.announce("w1", ["sequencer", "tasks"], [])
    assert [worker.holder for worker in presence.live_workers()] == ["w1", "w2"]
    assert presence.live_roles() == {"sequencer", "tasks"}

    WorkerProcess.objects.filter(holder="w2").update(
        seen_at=WorkerProcess.objects.get(holder="w2").seen_at.replace(year=2020)
    )
    assert presence.live_roles() == {"sequencer", "tasks"}
    assert [worker.holder for worker in presence.live_workers()] == ["w1"]
    assert presence.purge_stale() == 1
    presence.withdraw("w1")
    assert not WorkerProcess.objects.exists()


# -- Befehl ----------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("argumente", "meldung"),
    [
        (["--roles", "sequencer,unbekannt"], "Unbekannte Rollen: unbekannt"),
        (["--roles", ","], "Mindestens eine Rolle"),
        (["--queues", "gibtsnicht"], "Unbekannte Warteschlangen: gibtsnicht"),
        (["--roles", "scheduler", "--concurrency", "mail=1"], "--concurrency gilt nur für die Rolle tasks"),
        (["--roles", "tasks", "--concurrency", "mail=x"], "--concurrency erwartet"),
        (["--roles", "tasks", "--queues", "ocr", "--concurrency", "ocr=0"], "keine Warteschlange"),
        (["--roles", "scheduler", "--subscription", "test.x"], "--subscription gilt nur für die Rolle dispatch"),
        (["--roles", "dispatch", "--subscription", "test.x"], "Nicht registriert: test.x"),
        (["--max-tasks", "-1"], "--max-tasks darf nicht negativ sein"),
    ],
)
def test_befehl_prueft_argumente(argumente: list[str], meldung: str, leeres_register: dict[str, Subscriber]) -> None:
    with pytest.raises(CommandError, match=meldung):
        call_command("events_worker", "--metrics-port", "0", *argumente)


@pytest.mark.django_db
def test_befehl_sequenzierer_ohne_postgresql() -> None:
    if connection.vendor == "postgresql":
        pytest.skip("nur ohne PostgreSQL")
    with pytest.raises(CommandError, match="braucht PostgreSQL"):
        call_command("events_worker", "--roles", "sequencer", "--metrics-port", "0")


def _signal_senden(bedingung: Callable[[], bool], *, zweimal: bool = False) -> threading.Thread:
    """Ruft den Signal-Handler des Befehls auf, sobald ``bedingung`` gilt (wie ``kill -TERM``)."""
    vorher = signal.getsignal(signal.SIGTERM)

    def senden() -> None:
        _bis(lambda: signal.getsignal(signal.SIGTERM) is not vorher, frist=30.0, hinweis="Handler fehlt")
        _bis(bedingung, hinweis="Bedingung vor dem Signal")
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        for _ in range(2 if zweimal else 1):
            handler(signal.SIGTERM, None)

    faden = threading.Thread(target=senden, daemon=True)
    faden.start()
    return faden


@pytest.fixture
def journal(settings: Any) -> Iterator[JournalBackend]:
    settings.TASKS = journal_einstellungen()
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    yield backend


@pytest.mark.django_db(transaction=True)
def test_befehl_fuehrt_auftraege_aus_meldet_sich_an_und_endet_bei_sigterm(
    journal: JournalBackend, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nur_postgres()
    ergebnis = T.merken.enqueue("vom-worker")
    datei = tmp_path / "worker.heartbeat"
    angemeldet: list[list[str]] = []

    def erledigt_und_angemeldet() -> bool:
        zeile = WorkerProcess.objects.first()
        if zeile is not None:
            angemeldet.append(list(zeile.roles))
        fertig = TaskRow.objects.filter(pk=ergebnis.id, status=TaskStatus.ERLEDIGT).exists()
        connection.close()
        return fertig and bool(angemeldet) and datei.exists()

    faden = _signal_senden(erledigt_und_angemeldet)
    call_command(
        "events_worker",
        "--roles",
        "tasks,scheduler",
        "--queues",
        "default",
        "--metrics-port",
        "0",
        "--heartbeat-file",
        str(datei),
    )
    faden.join(5)

    assert auftraege.aufrufe == [("merken", ("vom-worker", 0))]
    assert angemeldet[-1] == ["tasks", "scheduler"]
    assert not WorkerProcess.objects.exists()
    assert not Lease.objects.filter(name=SCHEDULER_LEASE).exists(), "Lease beim Beenden freigegeben"
    ausgabe = capsys.readouterr().out
    assert "Worker gestartet" in ausgabe and "Aufträge default=4" in ausgabe
    assert "Worker beendet (stop)" in ausgabe


@pytest.mark.django_db(transaction=True)
def test_sigterm_schreibt_den_laufenden_batch_samt_cursor_fest(leeres_register: dict[str, Subscriber]) -> None:
    """Das Signal kommt, während der Handler einen Batch bearbeitet: Der Batch wird noch festgeschrieben."""
    nur_postgres()
    im_handler, weiter = threading.Event(), threading.Event()
    zugestellt: list[list[int]] = []

    def handler(events: list[Event], delivery: Delivery) -> None:
        im_handler.set()
        assert weiter.wait(30)
        zugestellt.append([ereignis.seq or 0 for ereignis in events])

    subscriber("test.worker.batch", types=["test.*"], from_beginning=True)(handler)
    with transaction.atomic():
        folgenummern = [nummeriert().seq or 0 for _ in range(5)]

    def signal_im_batch() -> bool:
        if not im_handler.is_set():
            return False
        threading.Timer(0.3, weiter.set).start()  # das Signal kommt mitten im Batch
        return True

    faden = _signal_senden(signal_im_batch)
    call_command("events_worker", "--roles", "dispatch", "--metrics-port", "0", "--no-listen")
    faden.join(5)

    assert zugestellt == [folgenummern], "genau ein Batch, nichts nach dem Signal"
    assert Subscription.objects.get(name="test.worker.batch").cursor_seq == folgenummern[-1]
    assert not Lease.objects.filter(name="dispatch:test.worker.batch").exists()


@pytest.mark.django_db(transaction=True)
def test_sequenzierer_und_zustellung_im_selben_prozess(leeres_register: dict[str, Subscriber]) -> None:
    """Neue Journalzeilen bekommen eine Folgenummer und werden zugestellt; ein Listener weckt beide."""
    nur_postgres()
    angekommen: list[str] = []

    def handler(events: list[Event], delivery: Delivery) -> None:
        angekommen.extend(str(ereignis.event_id) for ereignis in events)

    subscriber("test.worker.kette", types=["test.*"], from_beginning=True)(handler)
    geschrieben: list[str] = []

    def schreiben_und_warten() -> bool:
        if not geschrieben:
            with transaction.atomic():
                geschrieben.extend(str(ereignis_anlegen().event_id) for _ in range(3))
            connection.close()
        return set(geschrieben) <= set(angekommen)

    faden = _signal_senden(schreiben_und_warten)
    call_command("events_worker", "--roles", "sequencer,dispatch", "--metrics-port", "0")
    faden.join(5)

    assert set(geschrieben) <= set(angekommen)
    assert not Lease.objects.filter(name="sequencer").exists()
    assert leases.acquire("sequencer", "danach"), "Lease frei für den nächsten Prozess"


@pytest.mark.django_db
def test_befehl_ersetzt_sich_beim_neustartwunsch(journal: JournalBackend) -> None:
    with (
        mock.patch("apps.events.management.commands.events_worker.Worker.run", return_value=ExitReason.RESTART),
        mock.patch("apps.events.management.commands.events_worker.os.name", "posix"),
        mock.patch("apps.events.management.commands.events_worker.replace_process") as ersetzen,
    ):
        call_command("events_worker", "--roles", "tasks", "--metrics-port", "0")
    ersetzen.assert_called_once_with()

    with (
        mock.patch("apps.events.management.commands.events_worker.Worker.run", return_value=ExitReason.RESTART),
        pytest.raises(SystemExit) as beendet,
    ):
        call_command("events_worker", "--roles", "tasks", "--metrics-port", "0", "--no-restart")
    assert beendet.value.code == 75


@pytest.mark.django_db
def test_befehl_endet_mit_fehler_bei_ausfall_einer_rolle(journal: JournalBackend) -> None:
    with (
        mock.patch("apps.events.management.commands.events_worker.Worker.run", return_value=ExitReason.FAILURE),
        pytest.raises(CommandError, match="Rolle ist ausgefallen"),
    ):
        call_command("events_worker", "--roles", "tasks", "--metrics-port", "0")


@pytest.mark.django_db
def test_befehl_vergroessert_den_pool_auf_das_budget(journal: JournalBackend) -> None:
    with (
        mock.patch("apps.events.management.commands.events_worker.Worker.run", return_value=ExitReason.STOP),
        mock.patch("apps.events.management.commands.events_worker.ensure_pool_capacity") as pool,
    ):
        call_command(
            "events_worker", "--roles", "tasks,scheduler", "--queues", "mail", "--metrics-port", "0", "--no-listen"
        )
    # Hauptfaden + Reserve, Koordinator + 2 Plätze für mail, Zeitpläne
    pool.assert_called_once_with(1 + POOL_RESERVE + 3 + 1)

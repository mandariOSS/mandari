# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mehrere Anwendungsprozesse (Daphne) in einem Container (Issue #718).

Start aus ``docker-entrypoint.sh``::

    python -m mandari.app_processes --bind 0.0.0.0 --port 8000 -- --proxy-headers mandari.asgi:application

Alles nach ``--`` geht unverändert an Daphne. Die Zahl der Prozesse kommt aus ``APP_PROCESSES``
(Vorgabe 1, höchstens 64) oder ``--processes``.

**Ein Prozess (Vorgabe):** Das Modul ersetzt sich per ``exec`` durch
``daphne --bind … --port … <Argumente>``, genau der Befehl von vorher. Es bleibt kein eigener Prozess.

**Mehrere Prozesse:** Das Modul startet N Daphne-Prozesse am selben Port. Jeder bekommt über ``--fd``
einen eigenen Socket mit ``SO_REUSEPORT``; der Kernel verteilt neue Verbindungen gleichmäßig nach
Absender auf die Sockets. Ein einziger gemeinsamer Socket genügt nicht: Dort nimmt meist derselbe Prozess
an, und Caddy hält seine Verbindungen dann alle zu diesem einen offen. Endet ein Prozess, verschwindet sein
Socket aus der Verteilung, bis er neu gestartet ist. Caddy, der Docker-Healthcheck und ``METRICS_URL``
bleiben bei Port 8000, nichts außerhalb des Containers ändert sich. Daphne statt eines anderen
ASGI-Servers, weil es HTTP und WebSocket im Betrieb trägt und die Schutzmaßnahmen gegen den Pool-Verlust
(#344, ``apps.common.db_connections``) darauf abgestimmt sind.

- **Prüfung je Prozess:** Jeder Prozess hört zusätzlich auf ``127.0.0.1:<Prüfport>`` (Vorgabe Port + 100 +
  Nummer, also 8101, 8102, …), nur innerhalb des Containers. Darüber ruft dieses Modul alle 10 s
  ``/health/live/`` jedes einzelnen Prozesses ab. Nach drei Fehlschlägen in Folge (etwa ein festgefahrener
  Datenbank-Pool, #344) startet es nur diesen Prozess neu. Der Healthcheck auf Port 8000 erreicht dagegen
  nur einen beliebigen Prozess.
- **Absturz:** Endet ein Prozess unerwartet, startet das Modul ihn neu. Endet er in den ersten 30 s nach
  seinem Start, ist das ein Startfehler: Dann enden alle, und Docker startet den Container neu wie bisher.
- **Herunterfahren:** SIGTERM und SIGINT gehen an alle Prozesse; wer nach 8 s noch läuft, bekommt SIGKILL
  (Docker wartet 10 s).
- **Metriken:** Die Prozesse schreiben ihre Zähler in ``PROMETHEUS_MULTIPROC_DIR`` (Vorgabe
  ``/tmp/mandari-metriken``, beim Start geleert); ``/metrics/`` summiert sie über alle Prozesse
  (``apps.common.metrics``).
- **WebSocket:** Verbindungen derselben Sitzung oder desselben Dokuments landen in verschiedenen Prozessen
  und erreichen sich über den Channel-Layer in Redis. Ohne Redis (``REDIS_URL`` leer) gibt es nur den
  Channel-Layer im Speicher eines Prozesses; dann verweigert das Modul den Start mit mehreren Prozessen.

Das Modul importiert kein Django, damit es auch dann startet, wenn die Anwendung beim Import scheitert.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

MAX_PROCESSES = 64
DEFAULT_SERVER = ("daphne",)
CHECK_PATH = "/health/live/"
CHECK_PORT_OFFSET = 100
# Warteschlange noch nicht angenommener Verbindungen je Prozess
LISTEN_BACKLOG = 1024
METRICS_DIR_NAME = "mandari-metriken"
# Unterhalb dieses Speichers je Prozess warnt der Start (gemessen 300 bis 480 MB je Prozess unter Last,
# docs/LASTTESTS.md); empfohlen ist 1 GB je Prozess
MIN_MEMORY_PER_PROCESS = 512 * 1024 * 1024
DEFAULT_DB_POOL_MAX = 10
# Windows kennt kein SIGKILL (dort laufen nur die Tests der Überwachung ohne echte Prozesse)
SIGKILL: int = getattr(signal, "SIGKILL", signal.SIGTERM)


@dataclass(frozen=True)
class Timing:
    """Zeiten der Überwachung in Sekunden; die Tests verkürzen sie."""

    check_interval: float = 10.0
    check_timeout: float = 5.0
    check_failures: int = 3
    # So lange nach dem Start zählen Fehlschläge der Prüfung nicht (Anwendung lädt noch)
    start_grace: float = 60.0
    # Endet ein Prozess früher als das nach seinem Start, enden alle (Startfehler)
    early_exit: float = 30.0
    stop_timeout: float = 8.0
    poll: float = 0.5


def log(message: str) -> None:
    print(f"app_processes: {message}", file=sys.stderr, flush=True)


def parse_processes(raw: str | None) -> int:
    """Zahl der Anwendungsprozesse aus ``APP_PROCESSES``; leer heißt 1."""
    value = (raw or "").strip() or "1"
    try:
        processes = int(value)
    except ValueError:
        raise ValueError(f"APP_PROCESSES muss eine ganze Zahl sein, nicht {value!r}") from None
    if not 1 <= processes <= MAX_PROCESSES:
        raise ValueError(f"APP_PROCESSES muss zwischen 1 und {MAX_PROCESSES} liegen, nicht {processes}")
    return processes


def single_command(server: Sequence[str], bind: str, port: int, server_args: Sequence[str]) -> list[str]:
    """Befehl für genau einen Prozess: Daphne bindet den Port selbst, wie vor #718."""
    return [*server, "--bind", bind, "--port", str(port), *server_args]


def child_command(server: Sequence[str], fd: int, check_port: int, server_args: Sequence[str]) -> list[str]:
    """Befehl eines von mehreren Prozessen: geteilter Port über ``--fd``, eigener Prüfport auf 127.0.0.1."""
    return [*server, "--fd", str(fd), "-e", f"tcp:port={check_port}:interface=127.0.0.1", *server_args]


def listen_socket(bind: str, port: int) -> socket.socket:
    """Socket eines Prozesses am gemeinsamen Port (``SO_REUSEPORT``, nur IPv4 wie Daphnes Plugin „fd“)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind((bind, port))
        sock.listen(LISTEN_BACKLOG)
    except OSError:
        sock.close()
        raise
    return sock


def probe_live(port: int, timeout: float) -> bool:
    """``/health/live/`` eines einzelnen Prozesses über seinen Prüfport (Host ``localhost`` ist immer erlaubt)."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{CHECK_PATH}", headers={"Host": "localhost"})
    # Ohne Proxy aus der Umgebung: Der Prüfport liegt im Container
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        # HTTPError (503 bei festgefahrenem Pool) ist ein URLError
        return False


def memory_limit_bytes() -> int | None:
    """Speichergrenze des Containers (cgroup v2 bzw. v1) oder None ohne Grenze."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text(encoding="ascii").strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # cgroup v1 meldet „unbegrenzt“ als sehr große Zahl
        return value if value < 1 << 60 else None
    return None


def prepare_metrics_dir(env: dict[str, str]) -> str:
    """Verzeichnis für die Zähler aller Prozesse anlegen und leeren (Reste eines früheren Laufs)."""
    path = Path(env.get("PROMETHEUS_MULTIPROC_DIR") or Path(tempfile.gettempdir()) / METRICS_DIR_NAME)
    path.mkdir(parents=True, exist_ok=True)
    for old in path.glob("*.db"):
        old.unlink(missing_ok=True)
    return str(path)


@dataclass
class Child:
    """Ein Anwendungsprozess mit festem Platz (Nummer, Prüfport); der Prozess dahinter wechselt beim Neustart."""

    slot: int
    check_port: int
    process: subprocess.Popen[bytes] | None = None
    started: float = 0.0
    failures: int = 0
    # Neustart nach Fehlschlägen der Prüfung angestoßen: Ende ist gewollt, kein Startfehler
    restarting: bool = False
    kill_at: float | None = None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None


class Supervisor:
    """Startet, überwacht und beendet die Anwendungsprozesse an einem gemeinsamen Port."""

    def __init__(
        self,
        *,
        processes: int,
        bind: str,
        port: int,
        server_args: Sequence[str],
        server: Sequence[str] = DEFAULT_SERVER,
        check_port_base: int | None = None,
        env: dict[str, str] | None = None,
        timing: Timing | None = None,
        probe: Callable[[int, float], bool] = probe_live,
        clock: Callable[[], float] = time.monotonic,
        reap_orphans: bool | None = None,
    ) -> None:
        self.processes = processes
        self.bind = bind
        self.port = port
        self.server = list(server)
        self.server_args = list(server_args)
        base = port + CHECK_PORT_OFFSET if check_port_base is None else check_port_base
        self.children = [Child(slot=i, check_port=base + i) for i in range(1, processes + 1)]
        self.env = dict(os.environ if env is None else env)
        self.timing = timing or Timing()
        self.probe = probe
        self.clock = clock
        # Als PID 1 im Container auch verwaiste Enkelprozesse abräumen (sonst bleiben Zombies)
        self.reap_orphans = os.getpid() == 1 if reap_orphans is None else reap_orphans
        self.exit_code: int | None = None
        self._stop = threading.Event()
        self._checks: dict[int, tuple[int, Future[bool]]] = {}
        self._next_check = 0.0
        self._pool: ThreadPoolExecutor | None = None

    # -- Start ---------------------------------------------------------------------------------

    def start(self) -> None:
        self.env["PROMETHEUS_MULTIPROC_DIR"] = prepare_metrics_dir(self.env)
        self._pool = ThreadPoolExecutor(max_workers=self.processes, thread_name_prefix="pruefung")
        pool_max = self.env.get("DB_POOL_MAX") or str(DEFAULT_DB_POOL_MAX)
        ports = f"{self.children[0].check_port}–{self.children[-1].check_port}"
        log(
            f"starte {self.processes} Anwendungsprozesse auf {self.bind}:{self.port} (Prüfports {ports}); "
            f"je Prozess bis zu DB_POOL_MAX={pool_max} Datenbankverbindungen, zusammen {self.processes} × {pool_max}"
        )
        limit = memory_limit_bytes()
        if limit is not None and limit < self.processes * MIN_MEMORY_PER_PROCESS:
            log(
                f"WARNUNG: Speichergrenze {limit // (1024 * 1024)} MB für {self.processes} Prozesse; "
                "empfohlen ist rund 1 GB je Prozess (MANDARI_MEM_LIMIT, DEPLOYMENT.md)"
            )
        for child in self.children:
            self._spawn(child)
        self._next_check = self.clock() + self.timing.check_interval

    def _spawn(self, child: Child) -> None:
        sock = listen_socket(self.bind, self.port)
        try:
            fd = sock.fileno()
            command = child_command(self.server, fd, child.check_port, self.server_args)
            child.process = subprocess.Popen(command, pass_fds=(fd,), env=self.env)  # noqa: S603 – fester Befehl
        finally:
            # Nur der Prozess hält seinen Socket: Endet er, schließt der Kernel ihn, und neue Verbindungen
            # gehen an die übrigen Prozesse
            sock.close()
        child.started = self.clock()
        child.failures = 0
        child.restarting = False
        child.kill_at = None
        log(f"Prozess {child.slot} gestartet (PID {child.pid}, Prüfport {child.check_port})")

    # -- Lauf ----------------------------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        log(f"Signal {signal.Signals(signum).name}: beende alle Anwendungsprozesse")
        self.request_stop()

    def run(self, *, install_signal_handlers: bool = True) -> int:
        """Bis zum Signal oder Startfehler laufen; Rückgabe ist der Exit-Code des Containers."""
        if install_signal_handlers:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
        self.start()
        while not self._stop.is_set():
            self._handle_exits()
            if self._stop.is_set():
                break
            now = self.clock()
            self._enforce_kills(now)
            self._check(now)
            self._stop.wait(self.timing.poll)
        return self._shutdown()

    def _collect_exits(self) -> list[tuple[Child, int]]:
        exits: list[tuple[Child, int]] = []
        if self.reap_orphans:
            by_pid = {c.pid: c for c in self.children if c.process is not None and c.process.returncode is None}
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                child = by_pid.get(pid)
                if child is None or child.process is None:
                    continue  # verwaister Enkelprozess, nur abgeräumt
                code = os.waitstatus_to_exitcode(status)
                child.process.returncode = code
                exits.append((child, code))
        else:
            for child in self.children:
                if child.process is not None and child.process.returncode is None:
                    code_or_none = child.process.poll()
                    if code_or_none is not None:
                        exits.append((child, code_or_none))
        return exits

    def _handle_exits(self) -> None:
        for child, code in self._collect_exits():
            lived = self.clock() - child.started
            if self._stop.is_set():
                continue
            if not child.restarting and lived < self.timing.early_exit:
                log(f"Prozess {child.slot} (PID {child.pid}) endete nach {lived:.0f} s mit {code}: Startfehler")
                self.exit_code = code if code > 0 else 1
                self.request_stop()
                return
            grund = "nach Neustart wegen der Prüfung" if child.restarting else f"unerwartet mit {code}"
            log(f"Prozess {child.slot} (PID {child.pid}) endete {grund}, starte neu")
            self._checks.pop(child.slot, None)
            self._spawn(child)

    def _enforce_kills(self, now: float) -> None:
        for child in self.children:
            if child.kill_at is not None and now >= child.kill_at and child.process is not None:
                if child.process.returncode is None:
                    log(f"Prozess {child.slot} (PID {child.pid}) reagiert nicht auf SIGTERM, SIGKILL")
                    _send(child.process, SIGKILL)
                child.kill_at = None

    def _check(self, now: float) -> None:
        for slot, (pid, future) in list(self._checks.items()):
            if not future.done():
                continue
            del self._checks[slot]
            child = self.children[slot - 1]
            if child.pid != pid or child.restarting:
                continue
            if future.exception() is None and future.result():
                child.failures = 0
                continue
            child.failures += 1
            log(f"Prozess {child.slot} (PID {pid}): {CHECK_PATH} fehlgeschlagen ({child.failures}×)")
            if child.failures >= self.timing.check_failures and child.process is not None:
                log(f"Prozess {child.slot} (PID {pid}) wird neu gestartet")
                child.restarting = True
                child.kill_at = now + self.timing.stop_timeout
                _send(child.process, signal.SIGTERM)
        if now < self._next_check or self._pool is None:
            return
        self._next_check = now + self.timing.check_interval
        for child in self.children:
            if child.process is None or child.process.returncode is not None or child.restarting:
                continue
            if child.slot in self._checks or now - child.started < self.timing.start_grace:
                continue
            future = self._pool.submit(self.probe, child.check_port, self.timing.check_timeout)
            self._checks[child.slot] = (child.process.pid, future)

    # -- Ende ----------------------------------------------------------------------------------

    def _shutdown(self) -> int:
        alive = [c.process for c in self.children if c.process is not None and c.process.returncode is None]
        for process in alive:
            _send(process, signal.SIGTERM)
        deadline = self.clock() + self.timing.stop_timeout
        while self.clock() < deadline and any(p.poll() is None for p in alive):
            time.sleep(0.1)
        for process in alive:
            if process.poll() is None:
                log(f"PID {process.pid} reagiert nicht auf SIGTERM, SIGKILL")
                _send(process, SIGKILL)
                process.wait()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        log("alle Anwendungsprozesse beendet")
        return self.exit_code or 0


def _send(process: subprocess.Popen[bytes], signum: int) -> None:
    """Signal senden, ohne den Prozess abzuräumen.

    Nicht ``Popen.send_signal``: Es ruft vorher ``poll()`` auf und räumt einen gerade beendeten Prozess ab.
    ``_collect_exits`` sähe ihn danach nicht mehr (``returncode`` gesetzt), und sein Platz bliebe leer.
    Abgeräumt wird nur in ``_collect_exits`` und ``_shutdown``; ist ``returncode`` gesetzt, ist die PID frei
    und darf kein Signal mehr bekommen.
    """
    if process.returncode is not None:
        return
    # Schon beendet, aber noch nicht abgeräumt: Die PID gehört weiter ihm, das Signal geht ins Leere
    with contextlib.suppress(ProcessLookupError):
        os.kill(process.pid, signum)


def main(argv: Sequence[str] | None = None) -> int:
    """Aufruf: ``python -m mandari.app_processes [--processes N] [--bind H] [--port P] -- <Daphne-Argumente>``."""
    args_all = list(sys.argv[1:] if argv is None else argv)
    if "--" in args_all:
        split = args_all.index("--")
        own, server_args = args_all[:split], args_all[split + 1 :]
    else:
        own, server_args = args_all, []
    parser = argparse.ArgumentParser(
        prog="python -m mandari.app_processes",
        description="Mehrere Anwendungsprozesse (Daphne) an einem Port (Issue #718)",
    )
    parser.add_argument("--processes", default=os.environ.get("APP_PROCESSES", ""), help="Vorgabe: APP_PROCESSES")
    parser.add_argument("--bind", default="0.0.0.0")  # noqa: S104 – Container-Port, wie vor #718
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check-port-base", type=int, default=None, help="Vorgabe: Port + 100")
    args = parser.parse_args(own)

    try:
        processes = parse_processes(args.processes)
    except ValueError as error:
        log(str(error))
        return 2
    if processes == 1:
        command = single_command(DEFAULT_SERVER, args.bind, args.port, server_args)
        os.execvp(command[0], command)  # noqa: S606 – ersetzt diesen Prozess, kehrt nicht zurück
    if "REDIS_URL" in os.environ and not os.environ["REDIS_URL"].strip():
        log(
            "mehrere Anwendungsprozesse brauchen Redis für den Channel-Layer (WebSocket); "
            "REDIS_URL ist leer. APP_PROCESSES=1 setzen oder Redis konfigurieren"
        )
        return 2
    supervisor = Supervisor(
        processes=processes,
        bind=args.bind,
        port=args.port,
        server_args=server_args,
        check_port_base=args.check_port_base,
    )
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())

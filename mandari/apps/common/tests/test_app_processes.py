# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mehrere Anwendungsprozesse an einem Port (``mandari.app_processes``, Issue #718).

- Vorgabe ein Prozess: genau der Daphne-Aufruf von vorher, per ``exec``; Compose stellt die Zahl ein.
- Überwachung ohne echte Prozesse: Startfehler beenden alle, ein späterer Absturz startet nur den einen neu,
  Fehlschläge der Prüfung starten ihn neu.
- Mit echten Daphne-Prozessen (nur Linux, wie im Container): gemeinsamer Port, Prüfport je Prozess, Neustart
  nach Absturz und festgefahrener Prüfung, Herunterfahren; WebSocket über Prozessgrenzen hinweg über den
  Channel-Layer in Redis (nur mit erreichbarem ``REDIS_URL``, in der CI).
"""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import yaml

from mandari import app_processes
from mandari.app_processes import Child, Supervisor, Timing

MANDARI = Path(__file__).resolve().parents[3]
REPO = MANDARI.parent
BISHER = ["--verbosity", "1", "--access-log", "-", "--proxy-headers", "mandari.asgi:application"]

nur_linux = pytest.mark.skipif(sys.platform == "win32", reason="geerbte Sockets und Signale wie im Container")


# --- Einstellen und Vorgabe ------------------------------------------------------------------------------


def test_zahl_der_prozesse() -> None:
    assert app_processes.parse_processes(None) == 1
    assert app_processes.parse_processes(" ") == 1
    assert app_processes.parse_processes("4") == 4
    for falsch in ("0", "65", "vier", "-1"):
        with pytest.raises(ValueError, match="APP_PROCESSES"):
            app_processes.parse_processes(falsch)


class ExecError(Exception):
    pass


def test_ein_prozess_ist_der_bisherige_daphne_aufruf(monkeypatch: pytest.MonkeyPatch) -> None:
    aufrufe: list[list[str]] = []

    def execvp(datei: str, argv: list[str]) -> None:
        aufrufe.append([datei, *argv])
        raise ExecError

    monkeypatch.setattr(os, "execvp", execvp)
    monkeypatch.delenv("APP_PROCESSES", raising=False)
    with pytest.raises(ExecError):
        app_processes.main(["--bind", "0.0.0.0", "--port", "8000", "--", *BISHER])  # noqa: S104 – wie im Container
    assert aufrufe == [["daphne", "daphne", "--bind", "0.0.0.0", "--port", "8000", *BISHER]]  # noqa: S104


def test_entrypoint_und_compose_stellen_die_zahl_ein() -> None:
    entrypoint = (MANDARI / "docker-entrypoint.sh").read_text(encoding="utf-8").replace("\\\n", " ")
    zeile = next(z for z in entrypoint.splitlines() if z.startswith("exec "))
    assert zeile.split() == ["exec", "python", "-m", "mandari.app_processes", "--bind", "0.0.0.0", "--port", "8000",
                             "--", *BISHER]  # fmt: skip
    dienst = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["mandari"]
    assert dienst["environment"]["APP_PROCESSES"] == "${APP_PROCESSES:-1}"
    assert dienst["mem_limit"] == "${MANDARI_MEM_LIMIT:-1g}"


def test_falsche_einstellung_startet_nicht(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_PROCESSES", "viele")
    assert app_processes.main(["--", *BISHER]) == 2
    # Ohne Redis kein gemeinsamer Channel-Layer: WebSocket-Nachrichten blieben im Prozess
    monkeypatch.setenv("APP_PROCESSES", "2")
    monkeypatch.setenv("REDIS_URL", "")
    assert app_processes.main(["--", *BISHER]) == 2


def test_befehl_je_prozess() -> None:
    befehl = app_processes.child_command(["daphne"], 5, 8102, BISHER)
    assert befehl == ["daphne", "--fd", "5", "-e", "tcp:port=8102:interface=127.0.0.1", *BISHER]


# --- Überwachung ohne echte Prozesse ---------------------------------------------------------------------


class FakeProcess:
    """Wie ``subprocess.Popen``: ``returncode`` steht erst nach ``poll()`` fest; ``ende`` setzt der Test."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.ende: int | None = None
        self.returncode: int | None = None
        self.signale: list[int] = []

    def poll(self) -> int | None:
        self.returncode = self.ende
        return self.returncode

    def send_signal(self, signum: int) -> None:
        self.signale.append(signum)


class Uhr:
    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


def _supervisor(uhr: Uhr, probe: Callable[[int, float], bool] = lambda port, timeout: True) -> Supervisor:
    timing = Timing(check_interval=10, check_failures=3, start_grace=60, early_exit=30, stop_timeout=8)
    sup = Supervisor(
        processes=2, bind="127.0.0.1", port=8000, server_args=BISHER, timing=timing, probe=probe, clock=uhr,
        reap_orphans=False,
    )  # fmt: skip
    gestartet: list[int] = []

    def spawn(child: Child) -> None:
        child.process = FakeProcess(100 + len(gestartet))  # type: ignore[assignment]
        gestartet.append(child.slot)
        child.started = uhr()
        child.failures = 0
        child.restarting = False
        child.kill_at = None

    sup._spawn = spawn  # type: ignore[method-assign]
    for child in sup.children:
        sup._spawn(child)
    return sup


def _prozess(child: Child) -> FakeProcess:
    assert isinstance(child.process, FakeProcess)
    return child.process


def test_ports_je_prozess() -> None:
    sup = Supervisor(processes=3, bind="127.0.0.1", port=8000, server_args=BISHER)
    assert [c.check_port for c in sup.children] == [8101, 8102, 8103]


def test_startfehler_beendet_alle() -> None:
    uhr = Uhr()
    sup = _supervisor(uhr)
    uhr.jetzt += 5
    _prozess(sup.children[0]).ende = 1
    sup._handle_exits()
    assert sup.exit_code == 1
    assert sup._stop.is_set()


def test_spaeterer_absturz_startet_nur_diesen_prozess_neu() -> None:
    uhr = Uhr()
    sup = _supervisor(uhr)
    zweiter = _prozess(sup.children[1])
    uhr.jetzt += 120
    _prozess(sup.children[0]).ende = -9
    sup._handle_exits()
    assert sup.exit_code is None
    assert not sup._stop.is_set()
    assert _prozess(sup.children[0]).pid == 102
    assert _prozess(sup.children[1]) is zweiter


def test_fehlgeschlagene_pruefung_startet_den_prozess_neu() -> None:
    uhr = Uhr()
    krank = {8101}
    sup = _supervisor(uhr, probe=lambda port, timeout: port not in krank)
    sup._pool = ThreadPoolExecutor(max_workers=2)
    erster = _prozess(sup.children[0])
    try:
        uhr.jetzt += 61
        for _ in range(3):
            uhr.jetzt += 10
            sup._next_check = 0
            sup._check(uhr())
            for _slot, (_pid, future) in sup._checks.items():
                future.result(timeout=5)
            sup._check(uhr())
        assert erster.signale == [signal.SIGTERM]
        assert sup.children[0].restarting
        assert _prozess(sup.children[1]).signale == []
        # Reagiert er nicht, folgt SIGKILL; sein Ende ist dann gewollt, kein Startfehler
        uhr.jetzt += 9
        sup._enforce_kills(uhr())
        assert erster.signale == [signal.SIGTERM, app_processes.SIGKILL]
        erster.ende = -9
        sup._handle_exits()
        assert sup.exit_code is None
        assert _prozess(sup.children[0]).pid == 102
    finally:
        sup._pool.shutdown()


def test_pruefung_wartet_den_start_ab() -> None:
    uhr = Uhr()
    sup = _supervisor(uhr, probe=lambda port, timeout: False)
    sup._pool = ThreadPoolExecutor(max_workers=2)
    try:
        uhr.jetzt += 30
        sup._next_check = 0
        sup._check(uhr())
        assert sup._checks == {}
    finally:
        sup._pool.shutdown()


# --- Echte Prozesse (Linux) ------------------------------------------------------------------------------


def _freie_ports(anzahl: int) -> int:
    """Erster von *anzahl* aufeinanderfolgenden freien Ports auf 127.0.0.1."""
    for _ in range(200):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            basis = int(probe.getsockname()[1])
        if basis + anzahl > 65535:
            continue
        frei = True
        for port in range(basis, basis + anzahl):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", port))
                except OSError:
                    frei = False
                    break
        if frei:
            return basis
    raise RuntimeError("keine freien Ports")


def _get(port: int, pfad: str = "/") -> tuple[int, int | None]:
    """Status und PID der Antwort; (0, None), wenn niemand antwortet."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{pfad}", timeout=3) as antwort:
            return int(antwort.status), int(json.loads(antwort.read())["pid"])
    except urllib.error.HTTPError as fehler:
        return int(fehler.code), int(json.loads(fehler.read())["pid"])
    except OSError:
        return 0, None


def _antwortet(port: int) -> bool:
    return _get(port)[0] == 200


def _warte(bedingung: Callable[[], bool], sekunden: float = 30) -> None:
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        if bedingung():
            return
        time.sleep(0.1)
    raise AssertionError("Bedingung nicht erfüllt")


def _laeuft(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@contextmanager
def _anwendung(tmp_path: Path, *, prozesse: int = 2) -> Iterator[tuple[Supervisor, int, dict[str, int]]]:
    """Supervisor mit echten Daphne-Prozessen der Test-Anwendung in einem Faden; liefert ihn, Port, Ergebnis."""
    basis = _freie_ports(prozesse + 1)
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    env.update({"PYTHONPATH": str(MANDARI), "MEHRPROZESS_MARKER_DIR": str(tmp_path)})
    env["PROMETHEUS_MULTIPROC_DIR"] = str(tmp_path / "metriken")
    sup = Supervisor(
        processes=prozesse,
        bind="127.0.0.1",
        port=basis,
        check_port_base=basis,
        server=[sys.executable, "-m", "daphne"],
        server_args=["apps.common.tests.mehrprozess_asgi:application"],
        env=env,
        timing=Timing(
            check_interval=0.2,
            check_timeout=1,
            check_failures=2,
            start_grace=2,
            early_exit=0.5,
            stop_timeout=5,
            poll=0.05,
        ),  # fmt: skip
        reap_orphans=False,
    )
    ergebnis: dict[str, int] = {}
    faden = threading.Thread(target=lambda: ergebnis.update(code=sup.run(install_signal_handlers=False)))
    faden.start()
    try:
        for child in sup.children:
            _warte(partial(_antwortet, child.check_port))
        yield sup, basis, ergebnis
    finally:
        sup.request_stop()
        faden.join(30)
    assert not faden.is_alive()


@nur_linux
def test_prozesse_teilen_den_port_und_werden_einzeln_neu_gestartet(tmp_path: Path) -> None:
    with _anwendung(tmp_path) as (sup, port, ergebnis):
        pids: dict[int, int] = {}
        for c in sup.children:
            pid = _get(c.check_port)[1]
            assert pid is not None
            pids[c.slot] = pid
        assert len(set(pids.values())) == 2
        assert pids == {c.slot: c.pid for c in sup.children}
        # Den gemeinsamen Port bedienen alle Prozesse (neue Verbindungen verteilt der Kernel)
        antworten = {_get(port) for _ in range(30)}
        assert {status for status, _ in antworten} == {200}
        assert {pid for _, pid in antworten} == set(pids.values())

        # Festgefahren (503 auf /health/live/): nur dieser Prozess wird ersetzt
        (tmp_path / f"krank-{pids[1]}").touch()
        _warte(lambda: _get(sup.children[0].check_port)[1] not in (None, pids[1]))
        assert not _laeuft(pids[1])
        assert sup.children[1].pid == pids[2]

        # Abgestürzt: ebenso
        os.kill(pids[2], signal.SIGKILL)
        _warte(lambda: _get(sup.children[1].check_port)[1] not in (None, pids[2]))
        neu = [c.pid for c in sup.children]
        assert sup.exit_code is None
    assert ergebnis == {"code": 0}
    assert not any(_laeuft(pid) for pid in neu if pid is not None)


@nur_linux
def test_startfehler_beendet_den_container(tmp_path: Path) -> None:
    sup = Supervisor(
        processes=2, bind="127.0.0.1", port=_freie_ports(3), server=[sys.executable, "-c", "raise SystemExit(3)"],
        server_args=[], env=dict(os.environ, PROMETHEUS_MULTIPROC_DIR=str(tmp_path)), reap_orphans=False,
        timing=Timing(poll=0.05),
    )  # fmt: skip
    assert sup.run(install_signal_handlers=False) == 3


@nur_linux
def test_herunterfahren_beendet_auch_wer_sigterm_ignoriert(tmp_path: Path) -> None:
    stur = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)"
    sup = Supervisor(
        processes=2, bind="127.0.0.1", port=_freie_ports(3), server=[sys.executable, "-c", stur], server_args=[],
        env=dict(os.environ, PROMETHEUS_MULTIPROC_DIR=str(tmp_path)), reap_orphans=False,
        timing=Timing(early_exit=0, start_grace=60, stop_timeout=1, poll=0.05),
    )  # fmt: skip
    threading.Timer(1.0, sup.request_stop).start()
    beginn = time.monotonic()
    assert sup.run(install_signal_handlers=False) == 0
    assert time.monotonic() - beginn < 10
    assert all(c.process is not None and c.process.returncode == -signal.SIGKILL for c in sup.children)


# --- WebSocket über Prozessgrenzen (Redis) ---------------------------------------------------------------


def _redis_erreichbar() -> bool:
    url = urlparse(os.environ.get("REDIS_URL", ""))
    if not url.hostname:
        return False
    try:
        socket.create_connection((url.hostname, url.port or 6379), timeout=1).close()
    except OSError:
        return False
    return True


class WebSocket:
    """Kleinster WebSocket-Client (RFC 6455): Textnachrichten als JSON, maskiert wie vorgeschrieben."""

    def __init__(self, port: int, pfad: str) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        schluessel = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {pfad} HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {schluessel}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        self.datei = self.sock.makefile("rb")
        status = self.datei.readline()
        assert b" 101 " in status, status
        while self.datei.readline() not in (b"\r\n", b""):
            pass

    def senden(self, daten: dict[str, Any]) -> None:
        nutzlast = json.dumps(daten).encode()
        assert len(nutzlast) < 126
        maske = os.urandom(4)
        self.sock.sendall(
            bytes([0x81, 0x80 | len(nutzlast)]) + maske + bytes(b ^ maske[i % 4] for i, b in enumerate(nutzlast))
        )

    def empfangen(self) -> dict[str, Any]:
        while True:
            kopf = self.datei.read(2)
            laenge = kopf[1] & 0x7F
            if laenge == 126:
                laenge = int.from_bytes(self.datei.read(2), "big")
            elif laenge == 127:
                laenge = int.from_bytes(self.datei.read(8), "big")
            nutzlast = self.datei.read(laenge)
            if kopf[0] & 0x0F == 0x1:
                daten: dict[str, Any] = json.loads(nutzlast)
                return daten
            if kopf[0] & 0x0F == 0x8:
                raise ConnectionError("Verbindung geschlossen")

    def schliessen(self) -> None:
        self.datei.close()
        self.sock.close()


@nur_linux
@pytest.mark.skipif(not _redis_erreichbar(), reason="braucht Redis (REDIS_URL), läuft in der CI")
def test_websocket_nachrichten_erreichen_andere_prozesse(tmp_path: Path) -> None:
    with _anwendung(tmp_path) as (sup, port, _ergebnis):
        pfad = f"/ws/{uuid.uuid4().hex}/"
        # Je eine Verbindung gezielt an jeden Prozess (Prüfports), eine über den gemeinsamen Port
        a = WebSocket(sup.children[0].check_port, pfad)
        b = WebSocket(sup.children[1].check_port, pfad)
        c = WebSocket(port, pfad)
        try:
            pid_a, pid_b, pid_c = a.empfangen()["pid"], b.empfangen()["pid"], c.empfangen()["pid"]
            assert pid_a != pid_b
            assert pid_c in (pid_a, pid_b)
            a.senden({"text": "hallo"})
            for verbindung, pid in ((a, pid_a), (b, pid_b), (c, pid_c)):
                assert verbindung.empfangen() == {"text": "hallo", "von": pid_a, "pid": pid}
        finally:
            for verbindung in (a, b, c):
                verbindung.schliessen()


# --- Metriken: Dateien je Prozess ----------------------------------------------------------------------------


def test_prozesse_schreiben_ihre_zaehler_in_das_gemeinsame_verzeichnis(tmp_path: Path) -> None:
    """Der Supervisor gibt den Prozessen ein leeres gemeinsames Verzeichnis (Summe in apps.common.metrics)."""
    alt = tmp_path / "counter_1.db"
    alt.write_bytes(b"x")
    assert app_processes.prepare_metrics_dir({"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}) == str(tmp_path)
    assert not alt.exists()
    skript = (
        "from prometheus_client import Counter\n"
        "Counter('mandari_http_requests', 'Anfragen', ['view', 'status_class']).labels('v', '2xx').inc()\n"
    )
    for _ in range(2):
        subprocess.run(  # noqa: S603 – fester Befehl
            [sys.executable, "-c", skript], env=dict(os.environ, PROMETHEUS_MULTIPROC_DIR=str(tmp_path)), check=True
        )
    assert len(list(tmp_path.glob("counter_*.db"))) == 2

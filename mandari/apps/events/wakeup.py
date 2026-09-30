# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weckruf per ``LISTEN`` auf einer Direktverbindung, mit Abfrage als Rückfall (Issue #505).

Die Datenbank meldet neue Journalzeilen auf ``mandari_events`` (Trigger, weckt den Sequenzierer)
und vergebene Folgenummern auf ``mandari_events_seq`` (Sequenzierer, weckt die Zustellung). Der
``Listener`` hört auf einer eigenen Verbindung und ruft je Kanal Rückrufe auf, die wartende
Schleifen wecken. Die Schleifen fragen trotzdem weiter in festen Abständen ab (Zustellung alle 2 s,
Sequenzierer jede Sekunde); geht eine Meldung verloren, verzögert sich nur die Zustellung.

**PgBouncer:** Im Transaktionsmodus gehört eine Client-Verbindung nur für die Dauer einer
Transaktion zu einer Serververbindung; ``LISTEN`` bleibt an einer fremden Serververbindung hängen,
Meldungen kommen nie an. Deshalb nutzt der Listener ``EVENTS_DB_DIRECT_URL``, eine Verbindung an
PgBouncer vorbei direkt zu PostgreSQL. Ohne diese Einstellung nimmt er die Verbindungsdaten der
Standarddatenbank; das genügt ohne Pooler (Standardinstallation).

**Selbstprüfung:** Nach dem Verbinden und danach alle 30 s schickt der Listener über die
Standardverbindung der Anwendung ein ``NOTIFY`` an sich selbst und erwartet es auf der
Lauschverbindung. Kommt es nicht an (Pooler, abgerissene Verbindung hinter einer Firewall), meldet
er das, baut die Verbindung neu auf bzw. versucht es später erneut; bis dahin tragen die Abfragen
allein. Lässt sich die Selbstprüfung gar nicht erst senden (Standardverbindung gestört, etwa kurz
nach einem Neustart der Datenbank), ist das kein Befund über das Lauschen: Dann folgt ein neuer
Versuch mit wachsender Pause wie nach einem Verbindungsfehler. ``mandari_events_listener_up``
zeigt den Zustand.

**Protokolle** nennen bei Datenbankfehlern nur Fehlerklasse und SQLSTATE, nie die Meldung: libpq
nennt darin bei Verbindungsfehlern Host, Port und Benutzernamen.

Leader-Leases und alle übrige Arbeit brauchen keine Direktverbindung: Die Lease-Tabelle und
``leases.fence()`` wirken innerhalb einer Transaktion, und die Arbeit muss in derselben
Transaktion laufen wie der Handler bzw. die Nummernvergabe. Sitzungsgebundene Sperren gibt es
nicht.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from django.conf import settings
from django.db import connection
from psycopg import sql
from psycopg.conninfo import make_conninfo

from .metrics import LISTENER_UP

logger = logging.getLogger(__name__)

#: Kanal der Selbstprüfung
PING_CHANNEL = "mandari_events_ping"
#: Abstand der Selbstprüfung und Wartezeit auf die eigene Meldung (Sekunden)
PING_INTERVAL = 30.0
PING_TIMEOUT = 5.0
#: Kommt die eigene Meldung nicht an, wird es nach dieser Zeit erneut versucht (Sekunden)
UNHEALTHY_RETRY = 300.0
#: Wartezeit nach einem Verbindungsfehler, wachsend bis zur Obergrenze (Sekunden)
RECONNECT_MIN = 1.0
RECONNECT_MAX = 60.0
APPLICATION_NAME = "mandari-events-listen"

# Django-eigene Optionen, die libpq nicht kennt
_KEINE_LIBPQ_OPTIONEN = {"pool", "server_side_binding", "isolation_level", "assume_role", "cursor_factory"}


def listen_conninfo() -> str:
    """Verbindungsdaten zum Lauschen: ``EVENTS_DB_DIRECT_URL`` oder die der Standarddatenbank."""
    url = (getattr(settings, "EVENTS_DB_DIRECT_URL", "") or "").strip()
    if url:
        return url.replace("postgresql+psycopg://", "postgresql://").replace("postgresql+asyncpg://", "postgresql://")
    einstellungen = connection.settings_dict
    optionen = {k: v for k, v in (einstellungen.get("OPTIONS") or {}).items() if k not in _KEINE_LIBPQ_OPTIONEN}
    parameter: dict[str, Any] = {
        "dbname": einstellungen.get("NAME"),
        "user": einstellungen.get("USER"),
        "password": einstellungen.get("PASSWORD"),
        "host": einstellungen.get("HOST"),
        "port": einstellungen.get("PORT"),
        **optionen,
    }
    return make_conninfo("", **{k: v for k, v in parameter.items() if v not in (None, "")})


def listen_source() -> str:
    """Für Protokolle: woher die Verbindungsdaten kommen (nie die Daten selbst, sie enthalten das Passwort)."""
    return "EVENTS_DB_DIRECT_URL" if (getattr(settings, "EVENTS_DB_DIRECT_URL", "") or "").strip() else "DATABASE_URL"


def ping_via_default_connection(kanal: str, token: str) -> None:
    """Schickt die Selbstprüfung über die Standardverbindung der Anwendung (nicht über die Lauschverbindung).

    Über dieselbe Verbindung käme die Meldung auch hinter einem Pooler an, wenn Senden und Lauschen
    zufällig dieselbe Serververbindung erwischen; die Prüfung wäre dann wertlos.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, %s)", [kanal, token])
    finally:
        connection.close()  # die Verbindung nicht dauerhaft aus dem Pool nehmen


def error_summary(exc: BaseException) -> str:
    """Fehlerklasse und SQLSTATE für Protokolle.

    Nie die Meldung und kein Traceback: libpq nennt bei Verbindungsfehlern Host, Port und
    Benutzernamen. Djangos Datenbankfehler tragen den psycopg-Fehler als Ursache.
    """
    klasse = type(exc)
    name = f"{klasse.__module__}.{klasse.__qualname__}"
    ursache = exc if isinstance(exc, psycopg.Error) else exc.__cause__
    sqlstate = ursache.sqlstate if isinstance(ursache, psycopg.Error) else None
    return f"{name}, SQLSTATE {sqlstate}" if sqlstate else name


class PingNotSentError(Exception):
    """Die Selbstprüfung ließ sich über die Standardverbindung nicht senden; kein Befund über das Lauschen."""


@dataclass
class Listener:
    """Hört auf ``LISTEN``-Kanälen und ruft je Meldung die Rückrufe des Kanals auf."""

    callbacks: Mapping[str, Sequence[Callable[[], None]]]
    conninfo: str = ""
    send_ping: Callable[[str, str], None] = ping_via_default_connection
    #: Herkunft der Verbindungsdaten für Protokolle
    source: str = ""
    #: ``True``, solange die Selbstprüfung ankommt
    healthy: bool = field(default=False, init=False)
    _backend_pid: int | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.conninfo:
            self.conninfo = listen_conninfo()
            self.source = self.source or listen_source()
        self.source = self.source or "angegebene Verbindung"

    # --- Rückrufe ---

    def wake_all(self) -> None:
        """Weckt alle Schleifen, etwa nach einem Neuaufbau, bei dem Meldungen verloren sein können."""
        for rueckrufe in self.callbacks.values():
            for rueckruf in rueckrufe:
                rueckruf()

    def _melden(self, kanal: str) -> None:
        for rueckruf in self.callbacks.get(kanal, ()):
            rueckruf()

    def _zustand(self, gesund: bool) -> None:
        self.healthy = gesund
        LISTENER_UP.set(gesund)

    # --- Verbindung ---

    def _verbinden(self) -> psycopg.Connection[Any]:
        verbindung = psycopg.connect(
            self.conninfo,
            autocommit=True,
            application_name=APPLICATION_NAME,
            connect_timeout=10,
            # Abgerissene Verbindungen (Firewall, Neustart) fallen ohne Datenverkehr sonst lange nicht auf
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
        for kanal in [*self.callbacks, PING_CHANNEL]:
            verbindung.execute(sql.SQL("LISTEN {}").format(sql.Identifier(kanal)))
        self._backend_pid = verbindung.info.backend_pid
        return verbindung

    def _warten_auf(self, verbindung: psycopg.Connection[Any], sekunden: float, token: str | None = None) -> bool:
        """Liefert Meldungen bis zum Ablauf aus; ``True``, sobald die Selbstprüfung ``token`` ankommt."""
        ende = time.monotonic() + sekunden
        while (rest := ende - time.monotonic()) > 0:
            for meldung in verbindung.notifies(timeout=min(rest, 1.0)):
                if meldung.channel == PING_CHANNEL:
                    if token is not None and meldung.payload == token:
                        return True
                    continue
                self._melden(meldung.channel)
        return False

    def _pruefen(self, verbindung: psycopg.Connection[Any]) -> bool:
        """``True``, wenn die eigene Meldung ankommt; ``PingNotSentError``, wenn sie sich nicht senden lässt."""
        token = uuid.uuid4().hex
        try:
            self.send_ping(PING_CHANNEL, token)
        except Exception as exc:  # noqa: BLE001 – eine gestörte Standardverbindung sagt nichts über das Lauschen
            raise PingNotSentError(error_summary(exc)) from None
        return self._warten_auf(verbindung, PING_TIMEOUT, token)

    def _lauschen(self, stop: threading.Event) -> str:
        """Eine Verbindung lang lauschen.

        Ergebnis: ``"stop"``, ``"unwirksam"`` (schon die erste Selbstprüfung blieb aus) oder
        ``"neu"`` (eine spätere blieb aus; die Verbindung wird sofort neu aufgebaut). Lässt sich die
        Selbstprüfung nicht senden, endet das Lauschen mit ``PingNotSentError``.
        """
        with self._verbinden() as verbindung:
            if not self._pruefen(verbindung):
                return "unwirksam"
            if not self.healthy:
                logger.info("Weckruf: LISTEN aktiv (Verbindung aus %s)", self.source)
            self._zustand(True)
            self.wake_all()
            naechste_pruefung = time.monotonic() + PING_INTERVAL
            while not stop.is_set():
                rest = naechste_pruefung - time.monotonic()
                if rest > 0:
                    self._warten_auf(verbindung, min(1.0, rest))
                    continue
                if not self._pruefen(verbindung):
                    logger.warning("Weckruf: Selbstprüfung ausgeblieben, Verbindung wird neu aufgebaut")
                    self.wake_all()
                    return "neu"
                naechste_pruefung = time.monotonic() + PING_INTERVAL
        return "stop"

    def _gestoert(self, stop: threading.Event, pause: float, grund: str) -> float:
        """Neuer Versuch nach wachsender Pause; nach einer gesunden Phase beginnt sie von vorn."""
        if self.healthy:
            # Nach einem Abriss können Meldungen fehlen; die Abfragen holen sie nach
            self.wake_all()
            pause = RECONNECT_MIN
        self._zustand(False)
        logger.warning("Weckruf: %s (Verbindung aus %s), neuer Versuch in %.0f s", grund, self.source, pause)
        stop.wait(pause)
        return min(RECONNECT_MAX, pause * 2)

    def run(self, stop: threading.Event) -> None:
        """Dauerbetrieb bis ``stop``; baut die Verbindung nach Fehlern neu auf."""
        pause = RECONNECT_MIN
        try:
            while not stop.is_set():
                try:
                    ergebnis = self._lauschen(stop)
                except PingNotSentError as exc:
                    pause = self._gestoert(stop, pause, f"Selbstprüfung ließ sich nicht senden ({exc})")
                    continue
                except psycopg.Error as exc:
                    grund = f"Verbindung zum Lauschen verloren oder nicht möglich ({error_summary(exc)})"
                    pause = self._gestoert(stop, pause, grund)
                    continue
                except Exception:  # noqa: BLE001 – der Weckruf darf nicht sterben; die Abfragen tragen weiter
                    self._zustand(False)
                    logger.exception("Weckruf: unerwarteter Fehler, neuer Versuch in %.0f s", RECONNECT_MAX)
                    stop.wait(RECONNECT_MAX)
                    continue
                pause = RECONNECT_MIN
                if ergebnis != "unwirksam":
                    continue
                self._zustand(False)
                logger.warning(
                    "Weckruf: Meldungen erreichen die Lauschverbindung nicht (Verbindung aus %s), etwa hinter "
                    "PgBouncer im Transaktionsmodus. EVENTS_DB_DIRECT_URL auf eine Direktverbindung zu "
                    "PostgreSQL setzen. Bis dahin fragen die Schleifen regelmäßig ab; neuer Versuch in %.0f s.",
                    self.source,
                    UNHEALTHY_RETRY,
                )
                stop.wait(UNHEALTHY_RETRY)
        finally:
            self._zustand(False)
            connection.close()


def start_listener(
    callbacks: Mapping[str, Sequence[Callable[[], None]]], stop: threading.Event, **optionen: Any
) -> tuple[Listener, threading.Thread]:
    """Startet einen ``Listener`` in einem eigenen Faden (Dämon); endet mit ``stop``."""
    listener = Listener(callbacks, **optionen)
    faden = threading.Thread(target=listener.run, args=(stop,), name="events-listen", daemon=True)
    faden.start()
    return listener, faden

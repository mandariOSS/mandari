# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenbankverbindungen zuverlässig an den Pool zurückgeben (Issue #344).

Warum es das braucht
--------------------
Django gibt die Verbindung einer Anfrage über das Signal ``request_finished`` zurück,
das unter ASGI der Ereignis-Loop verschickt. Trennt der Client die Verbindung, bricht
asgiref die Aufgabe ab, während die synchrone View in ihrem Thread weiterläuft — ein
Thread lässt sich nicht abbrechen. Bei einem Teil dieser Anfragen verschickt Django
danach **nie** ``request_finished``, und die Verbindung, die die View bekommen hat, bleibt
ausgeliehen. Im Nachbau zu #344 (Produktions-Image, Pool mit drei Verbindungen, 30
Anfragen, die sofort auflegen) traf das 3 von 24 Anfragen mit Verbindung — genau die drei,
die danach im Pool fehlten. Daphne musste dafür nichts abwürgen, das Auflegen genügt.

Ohne Pool war das harmlos: Der Garbage Collector schloss die Verbindung irgendwann.
Mit dem psycopg-Pool ist der Platz dagegen für immer verloren, denn der Pool zählt die
Verbindung weiter als ausgeliehen. Nach ``max_size`` solchen Abbrüchen bekommt der
Prozess keine Verbindung mehr, und jede Anfrage scheitert nach ``timeout`` mit
``PoolTimeout`` — bis zum Neustart. Genau so fiel die Anwendung am 22.09.2026 für gut
25 Stunden aus, ausgelöst durch einen Schwachstellen-Scanner, der Dutzende Anfragen in
derselben Sekunde schickte und sofort wieder auflegte.

Unter ASGI bekommt jede Anfrage übrigens ihren *eigenen* Thread (asgiref legt je
Anfrage einen ``ThreadPoolExecutor(max_workers=1)`` an). Die Zahl gleichzeitiger
Threads ist also nicht begrenzt, eine Anfragespitze stellt beliebig viele Threads vor
die ``max_size`` Verbindungen des Pools.

Was hier geregelt ist
---------------------
- :class:`ReleaseDatabaseConnectionsMiddleware` schließt die Verbindungen am Ende jeder
  Anfrage *im Thread der View*, in einem ``finally``. Das läuft auch dann, wenn der
  Ereignis-Loop die Aufgabe längst abgebrochen hat. Deshalb ist sie bewusst synchron
  (``async_capable = False``): Eine asynchrone Variante liefe im Ereignis-Loop und käme
  nach dem Abbruch gar nicht mehr zum Zug.
- :func:`releases_db_connections` tut dasselbe für eigene Hintergrund-Threads.
- :func:`is_pool_exhausted` erkennt einen erschöpften Pool an der Ausnahme.
- :func:`pool_starved` erkennt einen *festgefahrenen* Pool für die Liveness-Prüfung:
  alle Verbindungen verliehen, seit einer Weile kein Fortschritt, die Datenbank selbst
  aber erreichbar. Das ist ein Fehler dieses Prozesses, den ein Neustart behebt.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections
from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)


def _aliase_in_transaktion() -> frozenset[str]:
    """Verbindungen dieses Threads, die gerade in einer Transaktion stecken.

    Die gehören einem Aufrufer außerhalb — in Tests der Testtransaktion — und dürfen nicht
    geschlossen werden, sonst wäre dessen Transaktion zerstört. Djangos Test-Client schaltet
    ``close_old_connections`` aus demselben Grund während einer Testanfrage ab. In
    Produktion steckt zu Beginn einer Anfrage oder eines Threads nie etwas in einer
    Transaktion.
    """
    return frozenset(v.alias for v in connections.all(initialized_only=True) if v.in_atomic_block)


class ReleaseDatabaseConnectionsMiddleware:
    """Gibt die Verbindungen einer Anfrage zurück — auch wenn der Server die Anfrage abbricht.

    Muss in ``MIDDLEWARE`` ganz vorn stehen, damit alles, was darunter noch die Datenbank
    anfasst (Sitzung speichern, Nachrichten), vor dem Zurückgeben erledigt ist.
    """

    sync_capable = True
    async_capable = False

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        fremd = _aliase_in_transaktion()
        try:
            return self.get_response(request)
        finally:
            # Dieselbe Aufräumroutine, die Django sonst bei request_finished aufruft
            # (close_old_connections) — hier aber garantiert im Thread, dem die Verbindung
            # gehört, und nur für das, was die Anfrage selbst geöffnet hat.
            for verbindung in connections.all(initialized_only=True):
                if verbindung.alias not in fremd:
                    verbindung.close_if_unusable_or_obsolete()


def close_thread_connections(ausser: frozenset[str] = frozenset()) -> None:
    """Schließt die Verbindungen des aktuellen Threads (mit Pool: gibt sie zurück)."""
    for verbindung in connections.all(initialized_only=True):
        if verbindung.alias not in ausser:
            verbindung.close()


def releases_db_connections[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Für Funktionen, die in einem eigenen Thread laufen und die Datenbank benutzen.

    Ein Thread, der seine Verbindung nicht selbst schließt, nimmt sie mit ins Grab — mit
    Pool ist der Platz dann verloren. Wird die Funktion ausnahmsweise im Thread eines
    Aufrufers mit offener Transaktion aufgerufen (Tests), bleibt dessen Verbindung offen.
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        fremd = _aliase_in_transaktion()
        try:
            return func(*args, **kwargs)
        finally:
            close_thread_connections(ausser=fremd)

    return wrapper


def is_pool_exhausted(exc: BaseException | None) -> bool:
    """True, wenn *exc* (oder eine Ursache davon) ein erschöpfter Verbindungspool ist."""
    try:
        from psycopg_pool import PoolTimeout, TooManyRequests
    except ImportError:  # ohne psycopg-Pool (SQLite, Tests) gibt es den Fall nicht
        return False
    gesehen: set[int] = set()
    while exc is not None and id(exc) not in gesehen:
        if isinstance(exc, (PoolTimeout, TooManyRequests)):
            return True
        gesehen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


# --- Liveness: festgefahrenen Pool erkennen --------------------------------------------

#: So lange muss der Pool ohne Fortschritt voll verliehen sein, bevor die Liveness rot wird.
#: Eine echte Lastspitze gibt in dieser Zeit laufend Verbindungen zurück und fällt nicht auf.
STARVATION_GRACE_SECONDS = 60.0


@dataclass
class _Beobachtung:
    #: Gelungene Ausleihen laut Pool-Statistik bei der letzten Prüfung.
    erfolgreich: int | None = None
    #: Seit wann der Pool ohne Fortschritt voll verliehen ist (``None``: gerade nicht).
    seit: float | None = None


_zustand_lock = threading.Lock()
_zustand = _Beobachtung()


def _pool(alias: str = DEFAULT_DB_ALIAS) -> Any:
    """Den Pool nur ansehen, wenn es ihn schon gibt.

    ``connection.pool`` würde nebenbei einen anlegen. Django hält die Pools je Alias in
    ``_connection_pools`` und öffnet sie erst bei der ersten Verbindung — ein Pool, den es
    noch nicht gibt oder der geschlossen ist, kann auch nicht festgefahren sein.
    """
    pools = getattr(type(connections[alias]), "_connection_pools", None)
    pool = pools.get(alias) if pools else None
    if pool is None or getattr(pool, "closed", True):
        return None
    return pool


def _datenbank_erreichbar(alias: str, wartezeit: float) -> bool:
    """Direkte Verbindung am Pool vorbei: Liegt es am Prozess oder an der Datenbank?"""
    import psycopg

    parameter = dict(connections[alias].get_connection_params())
    parameter["connect_timeout"] = max(1, int(wartezeit))
    try:
        with psycopg.connect(**parameter) as verbindung:
            verbindung.execute("SELECT 1")
    except Exception:  # noqa: BLE001 - jede Ausnahme heißt hier „nicht erreichbar“
        return False
    return True


def pool_starved(
    alias: str = DEFAULT_DB_ALIAS,
    *,
    wartezeit: float = 1.0,
    frist: float = STARVATION_GRACE_SECONDS,
    jetzt: Callable[[], float] = time.monotonic,
) -> bool:
    """True, wenn der Pool festgefahren ist und nur ein Neustart hilft.

    Festgefahren heißt: Alle Verbindungen sind verliehen, seit mindestens *frist* Sekunden
    ist keine einzige Ausleihe mehr gelungen, auch ein eigener Versuch scheitert — und die
    Datenbank ist direkt erreichbar. Ist die Datenbank selbst weg, liefert die Funktion
    False: Dann hilft kein Neustart, und das meldet die Readiness.
    """
    pool = _pool(alias)
    if pool is None:
        return False
    stats = pool.get_stats()
    erfolgreich = int(stats.get("requests_num", 0)) - int(stats.get("requests_errors", 0))
    voll_verliehen = stats.get("pool_available", 0) == 0 and stats.get("pool_size", 0) >= stats.get("pool_max", 0)

    with _zustand_lock:
        vorher = _zustand.erfolgreich
        _zustand.erfolgreich = erfolgreich
        fortschritt = vorher is None or erfolgreich > vorher
        if not voll_verliehen or fortschritt:
            _zustand.seit = None
            return False

    # Alles verliehen, seit der letzten Prüfung nichts zurückgekommen: selbst versuchen.
    from psycopg_pool import PoolTimeout, TooManyRequests

    try:
        verbindung = pool.getconn(timeout=wartezeit)
    except (PoolTimeout, TooManyRequests):
        pass
    else:
        pool.putconn(verbindung)
        with _zustand_lock:
            _zustand.seit = None
        return False

    with _zustand_lock:
        if _zustand.seit is None:
            _zustand.seit = jetzt()
        seit = _zustand.seit
    if jetzt() - seit < frist:
        return False
    if not _datenbank_erreichbar(alias, wartezeit):
        return False
    stats = pool.get_stats()
    logger.critical(
        "Datenbank-Pool festgefahren: %s von %s Verbindungen verliehen, %s Anfragen warten, "
        "seit %.0f s keine Ausleihe gelungen, Datenbank erreichbar — Prozess muss neu starten",
        stats.get("pool_size"),
        stats.get("pool_max"),
        stats.get("requests_waiting"),
        jetzt() - seit,
    )
    return True


def _reset_starvation_state() -> None:
    """Nur für Tests."""
    with _zustand_lock:
        _zustand.erfolgreich = None
        _zustand.seit = None

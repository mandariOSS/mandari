# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verbindungen kommen zurück in den Pool — auch nach abgebrochenen Anfragen (Issue #344).

Am 22.09.2026 räumte eine Scanner-Welle den Datenbank-Pool leer: Legte ein Client auf,
endete Djangos ``ASGIHandler.handle`` bei einem Teil der Anfragen mit ``CancelledError``,
``request_finished`` wurde nie verschickt, und die Verbindung der View blieb für immer
ausgeliehen. Der Regressionstest stellt genau diesen Abbruch nach — deterministisch, gegen
einen echten psycopg-Pool (CI mit PostgreSQL; lokal mit SQLite wird er übersprungen).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Mapping
from typing import Any, cast
from unittest import mock

import pytest
from django.conf import settings
from django.db import OperationalError, connection, connections
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path

from apps.common import db_connections, health
from apps.common.db_connections import (
    ReleaseDatabaseConnectionsMiddleware,
    is_pool_exhausted,
    pool_starved,
    releases_db_connections,
)
from apps.common.middleware import DatabaseErrorMiddleware, database_unavailable_response

# --- Hilfen ---------------------------------------------------------------------------

_view_haelt_verbindung = threading.Event()
_view_fertig = threading.Event()
#: Rohe psycopg-Verbindungen der View, damit die Gegenprobe ihre verlorene wieder schließen kann.
_gehaltene_verbindungen: list[Any] = []


def _haltende_view(request: Any) -> HttpResponse:
    """Holt sich eine Verbindung und hält sie, während draußen die Anfrage abgebrochen wird."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    _gehaltene_verbindungen.append(connection.connection)
    _view_haelt_verbindung.set()
    time.sleep(0.3)
    _view_fertig.set()
    return HttpResponse("ok")


def _pool_voll(request: Any) -> HttpResponse:
    from psycopg_pool import PoolTimeout

    try:
        raise PoolTimeout("couldn't get a connection after 5.00 sec")
    except PoolTimeout as exc:
        raise OperationalError(str(exc)) from exc


urlpatterns = [path("haelt/", _haltende_view), path("pool-voll/", _pool_voll)]
handler500 = "mandari.urls.handler_500"


def _pool_oder_skip() -> Any:
    if connection.vendor != "postgresql" or not connection.settings_dict.get("OPTIONS", {}).get("pool"):
        pytest.skip("Nachweis nur gegen einen echten psycopg-Pool (CI mit PostgreSQL)")
    return cast(Any, connections["default"]).pool  # Stubs kennen den Pool nicht


def _ausgeliehen(pool: Any) -> int:
    stats = pool.get_stats()
    return int(stats.get("pool_size", 0)) - int(stats.get("pool_available", 0))


def _anfrage_abbrechen_waehrend_view_verbindung_haelt() -> None:
    """Wie im Nachbau: Die ASGI-Aufgabe endet mit CancelledError, die View läuft im Thread weiter."""
    from django.core.handlers.asgi import ASGIHandler

    _view_haelt_verbindung.clear()
    _view_fertig.clear()

    async def ablauf() -> None:
        handler = ASGIHandler()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/haelt/",
            "raw_path": b"/haelt/",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 40000),
            "server": ("testserver", 80),
        }
        nachrichten: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        await nachrichten.put({"type": "http.request", "body": b"", "more_body": False})

        async def receive() -> dict[str, Any]:
            return await nachrichten.get()

        async def send(message: Mapping[str, Any]) -> None:
            return None

        aufgabe = asyncio.create_task(handler(scope, receive, send))
        for _ in range(500):
            if _view_haelt_verbindung.is_set():
                break
            await asyncio.sleep(0.01)
        aufgabe.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await aufgabe

    asyncio.run(ablauf())
    assert _view_fertig.wait(5), "Die View ist nicht zu Ende gelaufen"
    time.sleep(0.2)  # Aufräumen im View-Thread abwarten


# --- Regressionstest gegen einen echten Pool ------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestAbgebrocheneAnfrageGibtVerbindungZurueck:
    def test_mit_middleware_kommt_die_verbindung_zurueck(self) -> None:
        pool = _pool_oder_skip()
        with override_settings(
            ROOT_URLCONF=__name__,
            MIDDLEWARE=["apps.common.db_connections.ReleaseDatabaseConnectionsMiddleware"],
        ):
            vorher = _ausgeliehen(pool)
            _anfrage_abbrechen_waehrend_view_verbindung_haelt()
            nachher = _ausgeliehen(pool)

        assert nachher == vorher, (
            f"Nach der abgebrochenen Anfrage sind {nachher - vorher} Verbindung(en) mehr verliehen — "
            "genau so lief der Pool am 22.09.2026 leer"
        )

    def test_gegenprobe_ohne_middleware_geht_die_verbindung_verloren(self) -> None:
        """Beweist, dass der Test den Fehlerfall wirklich trifft.

        Schlägt diese Gegenprobe eines Tages fehl, gibt Django die Verbindung inzwischen
        selbst zurück — dann ist die Middleware ein Sicherheitsnetz, keine Notwendigkeit mehr.
        """
        pool = _pool_oder_skip()
        try:
            with override_settings(ROOT_URLCONF=__name__, MIDDLEWARE=[]):
                vorher = _ausgeliehen(pool)
                _anfrage_abbrechen_waehrend_view_verbindung_haelt()
                nachher = _ausgeliehen(pool)
            assert nachher == vorher + 1, "Ohne Middleware sollte die Verbindung der View ausgeliehen bleiben"
        finally:
            # Von allein kommt der Platz nie zurück — die Verbindung von Hand an denselben Pool
            # zurückgeben. Den Pool neu aufzubauen (close_pool) wäre gefährlicher: Den neuen
            # baut der Thread, der ihn als Nächstes anfasst, mit *seinen* Verbindungsdaten.
            for roh in _gehaltene_verbindungen:
                if not roh.closed and roh._pool is pool:
                    pool.putconn(roh)
            _gehaltene_verbindungen.clear()


# --- Middleware und Konfiguration -----------------------------------------------------


def test_middleware_steht_ganz_vorn() -> None:
    assert settings.MIDDLEWARE[0] == "apps.common.db_connections.ReleaseDatabaseConnectionsMiddleware", (
        "Sie muss außen stehen, damit Sitzung und Nachrichten vor dem Zurückgeben gespeichert sind"
    )


def test_middleware_ist_rein_synchron() -> None:
    """Nur synchron läuft ihr ``finally`` im Thread der View — eine async-Variante käme nach dem Abbruch nie dran."""
    assert ReleaseDatabaseConnectionsMiddleware.sync_capable is True
    assert ReleaseDatabaseConnectionsMiddleware.async_capable is False


class _FalscheVerbindung:
    def __init__(self, alias: str, in_atomic_block: bool = False) -> None:
        self.alias = alias
        self.in_atomic_block = in_atomic_block
        self.aufgeraeumt = 0
        self.geschlossen = 0

    def close_if_unusable_or_obsolete(self) -> None:
        self.aufgeraeumt += 1

    def close(self) -> None:
        self.geschlossen += 1


class _FalscheVerbindungen:
    def __init__(self, *verbindungen: _FalscheVerbindung) -> None:
        self.verbindungen = list(verbindungen)

    def all(self, initialized_only: bool = False) -> list[_FalscheVerbindung]:
        return list(self.verbindungen)


def test_middleware_raeumt_auf_auch_wenn_die_view_scheitert(monkeypatch: pytest.MonkeyPatch) -> None:
    verbindung = _FalscheVerbindung("default")
    monkeypatch.setattr(db_connections, "connections", _FalscheVerbindungen(verbindung))

    def kaputte_view(request: Any) -> HttpResponse:
        raise RuntimeError("View gescheitert")

    with pytest.raises(RuntimeError):
        ReleaseDatabaseConnectionsMiddleware(kaputte_view)(RequestFactory().get("/"))
    assert verbindung.aufgeraeumt == 1


def test_middleware_raeumt_nach_normaler_antwort_auf(monkeypatch: pytest.MonkeyPatch) -> None:
    verbindung = _FalscheVerbindung("default")
    monkeypatch.setattr(db_connections, "connections", _FalscheVerbindungen(verbindung))

    antwort = ReleaseDatabaseConnectionsMiddleware(lambda request: HttpResponse("ok"))(RequestFactory().get("/"))

    assert antwort.status_code == 200
    assert verbindung.aufgeraeumt == 1


def test_middleware_laesst_eine_umschliessende_transaktion_offen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Was schon vor der Anfrage in einer Transaktion steckte (Tests), gehört nicht der Anfrage."""
    testtransaktion = _FalscheVerbindung("default", in_atomic_block=True)
    eigene = _FalscheVerbindung("andere")
    monkeypatch.setattr(db_connections, "connections", _FalscheVerbindungen(testtransaktion, eigene))

    ReleaseDatabaseConnectionsMiddleware(lambda request: HttpResponse("ok"))(RequestFactory().get("/"))

    assert testtransaktion.aufgeraeumt == 0
    assert eigene.aufgeraeumt == 1


@pytest.mark.django_db
def test_testdaten_ueberstehen_eine_anfrage_durch_den_ganzen_stapel(client: Client) -> None:
    """Ende zu Ende: Die Middleware darf die Testtransaktion nicht schließen, sonst wären
    Testdaten nach der ersten Anfrage verschwunden — in der ganzen Testsuite."""
    from django.contrib.auth import get_user_model

    nutzer = get_user_model().objects.create(email="pool-probe@example.org")

    assert client.get("/health/live/").status_code == 200

    assert connection.connection is not None
    assert not connection.closed_in_transaction
    assert get_user_model().objects.filter(pk=nutzer.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_dekorator_schliesst_die_verbindung_des_threads() -> None:
    ergebnis: dict[str, bool] = {}

    @releases_db_connections
    def arbeit() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        ergebnis["offen_waehrenddessen"] = connection.connection is not None

    def im_thread() -> None:
        arbeit()
        ergebnis["offen_danach"] = connection.connection is not None

    t = threading.Thread(target=im_thread)
    t.start()
    t.join(10)

    assert ergebnis == {"offen_waehrenddessen": True, "offen_danach": False}


def test_dekorator_schliesst_auch_bei_ausnahme() -> None:
    @releases_db_connections
    def scheitert() -> None:
        raise ValueError("kaputt")

    with mock.patch.object(db_connections, "close_thread_connections") as schliessen, pytest.raises(ValueError):
        scheitert()
    schliessen.assert_called_once()


@pytest.mark.django_db(transaction=True)
def test_readiness_datenbankpruefung_gibt_ihre_verbindung_zurueck() -> None:
    """``check_database`` läuft in einem Wegwerf-Thread; früher kostete jeder Aufruf einen Pool-Platz."""
    ergebnis: dict[str, bool] = {}

    def im_thread() -> None:
        health.check_database()
        ergebnis["offen_danach"] = connection.connection is not None

    t = threading.Thread(target=im_thread)
    t.start()
    t.join(10)

    assert ergebnis == {"offen_danach": False}


def test_sync_watchdog_gibt_seine_verbindung_nach_jedem_lauf_zurueck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Der Watchdog lebt so lange wie der Webprozess; ohne Aufräumen belegte er dauerhaft
    einen Pool-Platz (gemessen in Produktion am 23.09.2026)."""
    from insight_sync import daemon

    aufrufe: list[frozenset[str]] = []
    monkeypatch.setattr(db_connections, "close_thread_connections", lambda ausser=frozenset(): aufrufe.append(ausser))
    for name in (
        "_cleanup_stale_syncs",
        "_run_periodic_georef",
        "_run_periodic_faction_reminders",
        "_run_periodic_faction_schedule",
        "_run_periodic_faction_invitations",
    ):
        monkeypatch.setattr(daemon, name, lambda: None)
    # Eigenes Stop-Signal, damit der echte Watchdog im Testprozess unberührt bleibt.
    stop = threading.Event()
    monkeypatch.setattr(daemon, "_stop_event", stop)

    def warten(sekunden: float) -> bool:
        if sekunden == 60:  # Ende des ersten Durchlaufs
            stop.set()
        return stop.is_set()

    monkeypatch.setattr(daemon, "_wait", warten)

    daemon._watchdog_loop()

    assert len(aufrufe) == 1, "Nach dem Durchlauf muss die Verbindung zurück in den Pool"


# --- Erschöpfter Pool: 503 ohne Datenbank ---------------------------------------------


def _pool_timeout_als_django_fehler() -> OperationalError:
    psycopg_pool = pytest.importorskip("psycopg_pool")
    try:
        try:
            raise psycopg_pool.PoolTimeout("couldn't get a connection after 5.00 sec")
        except psycopg_pool.PoolTimeout as exc:
            raise OperationalError(str(exc)) from exc
    except OperationalError as fehler:
        return fehler


def test_pool_erschoepfung_wird_erkannt() -> None:
    assert is_pool_exhausted(_pool_timeout_als_django_fehler())
    assert not is_pool_exhausted(OperationalError('relation "x" does not exist'))
    assert not is_pool_exhausted(None)


@pytest.mark.django_db
def test_process_exception_liefert_503_ohne_datenbankzugriff() -> None:
    request = RequestFactory().get("/insight/")
    with CaptureQueriesContext(connection) as abfragen:
        antwort = DatabaseErrorMiddleware(lambda r: HttpResponse()).process_exception(
            request, _pool_timeout_als_django_fehler()
        )
    assert antwort is not None
    assert antwort.status_code == 503
    assert antwort["Retry-After"] == "30"
    assert len(abfragen) == 0, "Die Fehlerseite darf bei leerem Pool nicht selbst eine Verbindung anfordern"


def test_process_exception_laesst_andere_fehler_durch() -> None:
    antwort = DatabaseErrorMiddleware(lambda r: HttpResponse()).process_exception(
        RequestFactory().get("/"), OperationalError('relation "x" does not exist')
    )
    assert antwort is None


def test_api_pfade_bekommen_json() -> None:
    antwort = database_unavailable_response(RequestFactory().get("/api/stats/"), pool=True)
    assert antwort.status_code == 503
    assert antwort["Content-Type"] == "application/json"


def test_nicht_erreichbare_datenbank_bekommt_laengeres_retry_after() -> None:
    antwort = DatabaseErrorMiddleware(lambda r: HttpResponse()).process_exception(
        RequestFactory().get("/"), OperationalError("could not connect to server: Connection refused")
    )
    assert antwort is not None
    assert antwort.status_code == 503
    assert antwort["Retry-After"] == "300"


@pytest.mark.django_db
def test_ganzer_stapel_liefert_503_statt_500() -> None:
    """Ende zu Ende: Früher kam hier eine 500 — die alte Middleware sah die Ausnahme der View nie."""
    pytest.importorskip("psycopg_pool")
    with override_settings(ROOT_URLCONF=__name__):
        antwort = Client(raise_request_exception=False).get("/pool-voll/")
    assert antwort.status_code == 503
    assert antwort["Retry-After"] == "30"


def test_500_handler_erkennt_pool_erschoepfung_aus_middlewares() -> None:
    from mandari.urls import handler_500

    request = RequestFactory().get("/work/")
    try:
        raise _pool_timeout_als_django_fehler()
    except OperationalError:
        antwort = handler_500(request)
    assert antwort.status_code == 503


# --- Liveness: festgefahrener Pool ------------------------------------------------------


class _FalscherPool:
    def __init__(self, stats: dict[str, int], getconn_gelingt: bool = False) -> None:
        self.stats = stats
        self.getconn_gelingt = getconn_gelingt
        self.zurueckgegeben = 0

    def get_stats(self) -> dict[str, int]:
        return dict(self.stats)

    def getconn(self, timeout: float) -> object:
        from psycopg_pool import PoolTimeout

        if self.getconn_gelingt:
            return object()
        raise PoolTimeout("zu")

    def putconn(self, verbindung: object) -> None:
        self.zurueckgegeben += 1


@pytest.fixture
def frischer_zustand() -> Any:
    pytest.importorskip("psycopg_pool")
    db_connections._reset_starvation_state()
    yield
    db_connections._reset_starvation_state()


def _voll(erfolgreich: int, fehler: int = 0) -> dict[str, int]:
    return {
        "pool_max": 10,
        "pool_size": 10,
        "pool_available": 0,
        "requests_num": erfolgreich + fehler,
        "requests_errors": fehler,
    }


def test_liveness_bleibt_gruen_solange_verbindungen_frei_sind(
    frischer_zustand: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    stats = {"pool_max": 10, "pool_size": 10, "pool_available": 3, "requests_num": 5}
    monkeypatch.setattr(db_connections, "_pool", lambda alias="default": _FalscherPool(stats))
    assert pool_starved() is False


def test_liveness_bleibt_gruen_bei_last_mit_fortschritt(
    frischer_zustand: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alle Verbindungen verliehen, aber es kommen laufend welche zurück: Lastspitze, kein Defekt."""
    uhr = iter([0.0, 100.0, 200.0, 300.0])
    pools = iter([_FalscherPool(_voll(10)), _FalscherPool(_voll(20)), _FalscherPool(_voll(30))])
    monkeypatch.setattr(db_connections, "_pool", lambda alias="default": next(pools))
    for _ in range(3):
        assert pool_starved(jetzt=lambda: next(uhr)) is False


def test_liveness_wird_rot_wenn_der_pool_festgefahren_ist(
    frischer_zustand: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _FalscherPool(_voll(10, fehler=4))
    monkeypatch.setattr(db_connections, "_pool", lambda alias="default": pool)
    monkeypatch.setattr(db_connections, "_datenbank_erreichbar", lambda alias, wartezeit: True)
    zeit = {"t": 0.0}

    assert pool_starved(jetzt=lambda: zeit["t"]) is False  # erste Beobachtung
    zeit["t"] = 30.0
    assert pool_starved(jetzt=lambda: zeit["t"]) is False  # noch in der Frist
    zeit["t"] = 95.0
    assert pool_starved(jetzt=lambda: zeit["t"]) is True


def test_liveness_bleibt_gruen_wenn_die_datenbank_selbst_weg_ist(
    frischer_zustand: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dann hilft kein Neustart — das meldet die Readiness, nicht die Liveness."""
    pool = _FalscherPool(_voll(10, fehler=4))
    monkeypatch.setattr(db_connections, "_pool", lambda alias="default": pool)
    monkeypatch.setattr(db_connections, "_datenbank_erreichbar", lambda alias, wartezeit: False)
    zeit = {"t": 0.0}

    pool_starved(jetzt=lambda: zeit["t"])
    zeit["t"] = 200.0
    pool_starved(jetzt=lambda: zeit["t"])
    zeit["t"] = 400.0
    assert pool_starved(jetzt=lambda: zeit["t"]) is False


def test_eigene_ausleihe_gelingt_dann_kein_befund(frischer_zustand: None, monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _FalscherPool(_voll(10), getconn_gelingt=True)
    monkeypatch.setattr(db_connections, "_pool", lambda alias="default": pool)

    pool_starved()
    assert pool_starved() is False
    assert pool.zurueckgegeben == 1, "Die Probe-Verbindung muss zurück in den Pool"


def test_ohne_pool_nie_festgefahren(frischer_zustand: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_connections, "_pool", lambda alias="default": None)
    assert pool_starved() is False


def test_liveness_endpunkt_meldet_festgefahrenen_pool(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "pool_starved", lambda: True)
    antwort = client.get("/health/live/")
    assert antwort.status_code == 503
    assert antwort.json()["status"] == "error"


def test_start_gibt_verbindungen_zurueck_ausser_in_transaktion(monkeypatch: pytest.MonkeyPatch) -> None:
    """``asgi.py`` gibt nach dem Start zurück, was der Hauptthread geöffnet hat — eine
    Testtransaktion (Import von asgi.py in Tests) bleibt dabei unberührt."""
    frei = _FalscheVerbindung("default")
    in_transaktion = _FalscheVerbindung("andere", in_atomic_block=True)
    monkeypatch.setattr(db_connections, "connections", _FalscheVerbindungen(frei, in_transaktion))

    db_connections.release_idle_thread_connections()

    assert frei.geschlossen == 1
    assert in_transaktion.geschlossen == 0


# --- Fehlerseiten im Standard-Executor ------------------------------------------------
#
# Unter ASGI rendert Django Fehlerseiten über response_for_exception mit
# thread_sensitive=False, also in den langlebigen Threads des Standard-Executors. Ohne
# Rückgabe behielt jeder Thread, der einmal eine Fehlerseite mit Datenbankzugriff gerendert
# hatte, seine Verbindung — zehn Executor-Threads, zehn Pool-Plätze.


def _nicht_da(request: Any) -> HttpResponse:
    from django.http import Http404

    raise Http404("gibt es nicht")


def _fehlerseite_mit_datenbank(request: Any, exception: Exception | None = None) -> HttpResponse:
    """Wie die echte 404-Seite: fragt beim Rendern die Datenbank ab."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    _gehaltene_verbindungen.append(connection.connection)
    return HttpResponse("nicht gefunden", status=404)


def _urlconf_mit(fehlerseite: Any) -> Any:
    import types

    modul = types.ModuleType("urlconf_fehlerseite_probe")
    modul.urlpatterns = [path("nicht-da/", _nicht_da)]  # type: ignore[attr-defined]
    modul.handler404 = fehlerseite  # type: ignore[attr-defined]
    return modul


def _404_ueber_asgi() -> None:
    from django.core.handlers.asgi import ASGIHandler

    async def ablauf() -> None:
        handler = ASGIHandler()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/nicht-da/",
            "raw_path": b"/nicht-da/",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 40001),
            "server": ("testserver", 80),
        }
        nachrichten: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        await nachrichten.put({"type": "http.request", "body": b"", "more_body": False})

        async def receive() -> dict[str, Any]:
            return await nachrichten.get()

        async def send(message: Mapping[str, Any]) -> None:
            return None

        await handler(scope, receive, send)

    asyncio.run(ablauf())


@pytest.mark.django_db(transaction=True)
class TestFehlerseitenGebenVerbindungZurueck:
    def test_mit_dekorator_kommt_die_verbindung_zurueck(self) -> None:
        pool = _pool_oder_skip()
        _gehaltene_verbindungen.clear()
        with override_settings(
            ROOT_URLCONF=_urlconf_mit(releases_db_connections(_fehlerseite_mit_datenbank)), MIDDLEWARE=[]
        ):
            vorher = _ausgeliehen(pool)
            _404_ueber_asgi()
            nachher = _ausgeliehen(pool)
        assert _gehaltene_verbindungen, (
            "Die Fehlerseite muss die Datenbank benutzt haben, sonst beweist der Test nichts"
        )
        assert nachher == vorher, f"Nach der 404 sind {nachher - vorher} Verbindung(en) mehr verliehen"

    def test_gegenprobe_ohne_dekorator_bleibt_die_verbindung_im_executor_thread(self) -> None:
        """Beweist, dass der Test den Executor-Pfad wirklich trifft."""
        pool = _pool_oder_skip()
        _gehaltene_verbindungen.clear()
        try:
            with override_settings(ROOT_URLCONF=_urlconf_mit(_fehlerseite_mit_datenbank), MIDDLEWARE=[]):
                vorher = _ausgeliehen(pool)
                _404_ueber_asgi()
                nachher = _ausgeliehen(pool)
            assert nachher == vorher + 1, "Ohne Dekorator sollte der Executor-Thread seine Verbindung behalten"
        finally:
            for roh in _gehaltene_verbindungen:
                if not roh.closed and roh._pool is pool:
                    pool.putconn(roh)
            _gehaltene_verbindungen.clear()


@pytest.mark.parametrize("name", ["handler_400", "handler_403", "handler_404", "handler_500"])
def test_alle_fehler_handler_geben_ihre_verbindung_zurueck(name: str) -> None:
    import mandari.urls as urls

    assert hasattr(getattr(urls, name), "__wrapped__"), f"{name} muss mit releases_db_connections dekoriert sein"

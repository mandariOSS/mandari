# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Weckruf per ``LISTEN`` auf einer Direktverbindung mit Abfrage als Rückfall (Issue #505).

- Der Listener weckt je Kanal, prüft sich selbst und baut abgerissene Verbindungen neu auf.
- Kommen Meldungen nicht an (etwa hinter PgBouncer im Transaktionsmodus), erkennt er das, meldet
  es und die Schleifen fragen allein ab.
- Sequenzierer und Zustellung werden geweckt, obwohl ihr Abfrageabstand hier 30 s beträgt.
- Latenz Commit → Sicht: p95 ≤ 5 s. Gemessen nur ohne parallele Testprozesse: In der CI halten
  offene Transaktionen anderer pytest-Worker den Sequenzierer clusterweit auf (siehe
  test_sequencer.py), das ist kein Normalbetrieb. Die CI misst deshalb im Job „Ereignistechnik
  hinter PgBouncer“ (ein Prozess, Anwendung über PgBouncer, Weckruf über ``EVENTS_DB_DIRECT_URL``).

Tests mit ``EVENTS_TEST_PGBOUNCER=1`` laufen nur in diesem Job.
"""

from __future__ import annotations

import functools
import logging
import math
import operator
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from django.db import connection, transaction
from prometheus_client import REGISTRY
from psycopg.conninfo import conninfo_to_dict

from apps.events import Delivery, subscriber, wakeup
from apps.events.dispatch import Dispatcher
from apps.events.models import NOTIFY_CHANNEL, Event
from apps.events.registry import Subscriber
from apps.events.sequencer import SEQUENCED_CHANNEL, Sequencer
from apps.events.tests.hilfen import ereignis_anlegen, nummeriert, nur_postgres, roh_einfuegen
from apps.events.wakeup import Listener, listen_conninfo, listen_source, start_listener

Verbindungen = Callable[..., psycopg.Connection[Any]]

KANAL = "mandari_test_weckruf"
#: Frist für Zustände, die sich in Sekundenbruchteilen einstellen sollten (großzügig für die CI)
FRIST = 30.0
HINTER_PGBOUNCER = os.environ.get("EVENTS_TEST_PGBOUNCER") == "1"


def _bis(bedingung: Callable[[], bool], frist: float = FRIST, hinweis: str = "") -> None:
    ende = time.monotonic() + frist
    while not bedingung():
        assert time.monotonic() < ende, hinweis or "Bedingung nicht erreicht"
        time.sleep(0.02)


def _p95(werte: list[float]) -> float:
    geordnet = sorted(werte)
    return geordnet[max(0, math.ceil(0.95 * len(geordnet)) - 1)]


@pytest.fixture
def stopp() -> Iterator[threading.Event]:
    """Stoppsignal für Hintergrundfäden; wird am Testende gesetzt."""
    ereignis = threading.Event()
    yield ereignis
    ereignis.set()


@pytest.fixture
def protokoll_sichtbar(monkeypatch: pytest.MonkeyPatch) -> None:
    # Die App-Logger geben nicht an die Wurzel weiter (apps.common.observability); caplog hängt dort
    monkeypatch.setattr(logging.getLogger("apps"), "propagate", True)


# --- Verbindungsdaten (ohne Datenbank) ---------------------------------------------------------


def test_direktverbindung_hat_vorrang(settings: Any) -> None:
    settings.EVENTS_DB_DIRECT_URL = "postgresql://mandari:geheim@db-direkt:5432/mandari"

    assert listen_conninfo() == "postgresql://mandari:geheim@db-direkt:5432/mandari"
    # Protokolle nennen nur die Herkunft, nie Verbindungsdaten (Passwort)
    assert listen_source() == "EVENTS_DB_DIRECT_URL"
    assert Listener({}).source == "EVENTS_DB_DIRECT_URL"


def test_ohne_direktverbindung_gelten_die_daten_der_standarddatenbank(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.EVENTS_DB_DIRECT_URL = ""
    monkeypatch.setattr(
        connection,
        "settings_dict",
        {
            "NAME": "mandari",
            "USER": "mandari",
            "PASSWORD": "geheim",
            "HOST": "db",
            "PORT": 5432,
            "OPTIONS": {"pool": {"min_size": 2}, "sslmode": "require"},
        },
    )

    teile = conninfo_to_dict(listen_conninfo())
    assert listen_source() == "DATABASE_URL"

    assert teile == {
        "dbname": "mandari",
        "user": "mandari",
        "password": "geheim",
        "host": "db",
        "port": "5432",
        "sslmode": "require",
    }


# --- Listener (PostgreSQL) -----------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_listener_weckt_je_kanal_und_prueft_sich_selbst(pg_verbindungen: Verbindungen, stopp: threading.Event) -> None:
    nur_postgres()
    geweckt, fremd = threading.Event(), threading.Event()
    listener, faden = start_listener({KANAL: [geweckt.set], "mandari_test_anderer": [fremd.set]}, stopp)

    _bis(lambda: listener.healthy, hinweis="Selbstprüfung nicht angekommen")
    assert geweckt.wait(FRIST), "nach dem Verbinden weckt der Listener einmal alle (verlorene Meldungen)"
    assert REGISTRY.get_sample_value("mandari_events_listener_up") == 1.0
    geweckt.clear()
    fremd.clear()

    pg_verbindungen().execute("SELECT pg_notify(%s, '')", (KANAL,))

    assert geweckt.wait(FRIST)
    assert not fremd.is_set(), "nur die Rückrufe des Kanals"
    stopp.set()
    faden.join(timeout=FRIST)
    assert not faden.is_alive()
    assert not listener.healthy
    assert REGISTRY.get_sample_value("mandari_events_listener_up") == 0.0


@pytest.mark.django_db(transaction=True)
def test_ohne_ankommende_meldungen_bleibt_es_bei_der_abfrage(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    protokoll_sichtbar: None,
    stopp: threading.Event,
) -> None:
    """Wie hinter PgBouncer im Transaktionsmodus: Die eigene Meldung erreicht die Lauschverbindung nie."""
    nur_postgres()
    monkeypatch.setattr(wakeup, "PING_TIMEOUT", 0.2)
    monkeypatch.setattr(wakeup, "UNHEALTHY_RETRY", 0.1)
    pruefungen: list[str] = []

    def verschluckt(kanal: str, token: str) -> None:
        pruefungen.append(token)

    with caplog.at_level(logging.WARNING, logger="apps.events.wakeup"):
        listener, faden = start_listener({KANAL: []}, stopp, send_ping=verschluckt)
        _bis(lambda: len(pruefungen) >= 3, hinweis="nach einer ausgebliebenen Selbstprüfung wird es erneut versucht")
        stopp.set()
        faden.join(timeout=FRIST)

    assert not listener.healthy
    assert any("EVENTS_DB_DIRECT_URL" in eintrag.getMessage() for eintrag in caplog.records)


@pytest.mark.django_db(transaction=True)
def test_nicht_gesendete_selbstpruefung_ist_kein_pgbouncer_befund(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    protokoll_sichtbar: None,
    stopp: threading.Event,
) -> None:
    """Etwa kurz nach einem Neustart der Datenbank: Die Standardverbindung kann die Prüfung nicht senden.

    Das sagt nichts über das Lauschen; statt fünf Minuten Pause mit PgBouncer-Hinweis folgt der
    nächste Versuch mit der wachsenden Pause wie nach einem Verbindungsfehler.
    """
    nur_postgres()
    monkeypatch.setattr(wakeup, "RECONNECT_MIN", 0.05)
    fehlschlaege = [2]

    def anfangs_gestoert(kanal: str, token: str) -> None:
        if fehlschlaege[0] > 0:
            fehlschlaege[0] -= 1
            raise psycopg.OperationalError('connection to server at "db-geheim", port 6432 failed')
        wakeup.ping_via_default_connection(kanal, token)

    with caplog.at_level(logging.WARNING, logger="apps.events.wakeup"):
        listener, faden = start_listener({KANAL: []}, stopp, send_ping=anfangs_gestoert)
        _bis(lambda: listener.healthy, hinweis="nach der Störung nicht wieder gesund (fünf Minuten Pause?)")
        stopp.set()
        faden.join(timeout=FRIST)

    meldungen = [eintrag.getMessage() for eintrag in caplog.records]
    assert sum("Selbstprüfung ließ sich nicht senden" in meldung for meldung in meldungen) == 2
    assert any("psycopg.OperationalError" in meldung for meldung in meldungen)
    # Die Herkunft darf EVENTS_DB_DIRECT_URL heißen (CI-Job hinter PgBouncer), der Hinweis darauf fehlt
    assert not any("PgBouncer" in meldung for meldung in meldungen), "kein Hinweis auf PgBouncer"
    assert "db-geheim" not in caplog.text, "keine Verbindungsdaten im Protokoll"


def test_protokoll_nennt_bei_verbindungsfehlern_keine_verbindungsdaten(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    protokoll_sichtbar: None,
    stopp: threading.Event,
) -> None:
    """libpq nennt in seinen Meldungen Host und Port; ins Protokoll gehören nur Fehlerklasse und SQLSTATE."""
    monkeypatch.setattr(wakeup, "RECONNECT_MIN", 0.05)
    listener = Listener(
        {KANAL: []}, conninfo="host=127.0.0.1 port=1 user=geheimnutzer dbname=nirgends connect_timeout=2"
    )

    with caplog.at_level(logging.WARNING, logger="apps.events.wakeup"):
        faden = threading.Thread(target=listener.run, args=(stopp,), daemon=True)
        faden.start()
        _bis(lambda: len(caplog.records) >= 2, hinweis="Verbindungsfehler nicht protokolliert")
        stopp.set()
        faden.join(timeout=FRIST)

    eigene = [eintrag for eintrag in caplog.records if eintrag.name == "apps.events.wakeup"]
    assert not listener.healthy
    # Fehlerklasse je nach System: psycopg.OperationalError oder eine Unterklasse (Zeitüberschreitung)
    assert eigene and all("(psycopg." in eintrag.getMessage() for eintrag in eigene)
    assert "127.0.0.1" not in caplog.text and "geheimnutzer" not in caplog.text
    assert all(eintrag.exc_info is None for eintrag in eigene), "kein Traceback mit der Meldung"


@pytest.mark.django_db(transaction=True)
def test_abgerissene_verbindung_wird_neu_aufgebaut(
    pg_verbindungen: Verbindungen, monkeypatch: pytest.MonkeyPatch, stopp: threading.Event
) -> None:
    nur_postgres()
    monkeypatch.setattr(wakeup, "RECONNECT_MIN", 0.05)
    geweckt = threading.Event()
    listener, _ = start_listener({KANAL: [geweckt.set]}, stopp)
    _bis(lambda: listener.healthy)
    alte_pid = listener._backend_pid
    assert alte_pid is not None
    geweckt.clear()

    pg_verbindungen().execute("SELECT pg_terminate_backend(%s)", (alte_pid,))

    assert geweckt.wait(FRIST), "nach dem Abriss weckt er alle, weil Meldungen fehlen können"
    _bis(lambda: listener.healthy and listener._backend_pid not in (None, alte_pid), hinweis="kein Neuaufbau")
    geweckt.clear()
    pg_verbindungen().execute("SELECT pg_notify(%s, '')", (KANAL,))
    assert geweckt.wait(FRIST)


@pytest.mark.django_db(transaction=True)
def test_sequenzierer_wird_von_neuen_journalzeilen_geweckt(
    monkeypatch: pytest.MonkeyPatch, stopp: threading.Event
) -> None:
    """Abfrageabstand 30 s: Läuft er nach dem Schreiben sofort, hat ihn der Weckruf geholt.

    Geprüft wird der Lauf, nicht die Vergabe: Ob er schon nummerieren darf, hängt in der CI auch an
    Transaktionen anderer Testprozesse (clusterweites ``xmin``).
    """
    nur_postgres()
    laeufe: list[float] = []
    echter_lauf = Sequencer.drain

    def mitschreiben(self: Sequencer) -> int:
        laeufe.append(time.monotonic())
        return echter_lauf(self)

    monkeypatch.setattr(Sequencer, "drain", mitschreiben)
    wecker = threading.Event()
    sequenzierer = Sequencer()

    def laufen() -> None:
        try:
            sequenzierer.run(stopp, interval=30.0, wake=wecker)
        finally:
            connection.close()

    faden = threading.Thread(target=laufen)
    faden.start()
    try:
        _bis(lambda: len(laeufe) >= 1, hinweis="erster Lauf")
        listener, _ = start_listener({NOTIFY_CHANNEL: [wecker.set]}, stopp)
        _bis(lambda: listener.healthy)
        _bis(lambda: len(laeufe) >= 2, hinweis="Lauf nach dem Wecken beim Verbinden")
        time.sleep(0.2)
        vorher = len(laeufe)
        geschrieben = time.monotonic()
        with transaction.atomic():
            ereignis_anlegen()

        _bis(lambda: len(laeufe) > vorher, frist=5.0, hinweis="Sequenzierer nicht geweckt")
        assert laeufe[vorher] - geschrieben < 5.0
    finally:
        stopp.set()
        wecker.set()
        faden.join(timeout=FRIST)
    assert not faden.is_alive()


@pytest.mark.django_db(transaction=True)
def test_zustellung_wird_von_neuen_folgenummern_geweckt(
    leeres_register: dict[str, Subscriber], pg_verbindungen: Verbindungen, stopp: threading.Event
) -> None:
    """Abfrageabstand der Zustellung 30 s; die Meldung ``mandari_events_seq`` holt sie sofort."""
    nur_postgres()
    angekommen: dict[int, float] = {}

    def handler(events: list[Event], delivery: Delivery) -> None:
        for ereignis in events:
            angekommen[ereignis.seq or 0] = time.monotonic()

    subscriber("test.weckruf", types=["test.*"], from_beginning=True)(handler)
    zusteller = Dispatcher(list(leeres_register.values()))
    faden = threading.Thread(target=zusteller.run, args=(stopp, 30.0))
    faden.start()
    melder = pg_verbindungen()
    dauer: list[float] = []
    try:
        listener, _ = start_listener({SEQUENCED_CHANNEL: [zusteller.wake]}, stopp)
        _bis(lambda: listener.healthy)
        time.sleep(0.2)
        for _ in range(10):
            ereignis = nummeriert()  # wie nach einem Lauf des Sequenzierers
            gemeldet = time.monotonic()
            melder.execute("SELECT pg_notify(%s, '')", (SEQUENCED_CHANNEL,))
            seq = ereignis.seq or 0
            _bis(functools.partial(operator.contains, angekommen, seq), frist=10.0, hinweis="nicht geweckt")
            dauer.append(angekommen[seq] - gemeldet)
    finally:
        stopp.set()
        faden.join(timeout=FRIST)

    assert _p95(dauer) < 5.0, dauer


@pytest.mark.skipif(
    bool(os.environ.get("PYTEST_XDIST_WORKER")),
    reason="Latenz nur ohne parallele Testprozesse (clusterweites xmin); in der CI im Job hinter PgBouncer",
)
@pytest.mark.django_db(transaction=True)
def test_latenz_vom_commit_bis_zur_sicht(
    leeres_register: dict[str, Subscriber], pg_verbindungen: Verbindungen, stopp: threading.Event
) -> None:
    """Spezifikation N4: Commit → Sicht p95 ≤ 5 s im Normalbetrieb, mit Weckruf und Standardabständen."""
    nur_postgres()
    angekommen: dict[str, float] = {}

    def handler(events: list[Event], delivery: Delivery) -> None:
        for ereignis in events:
            angekommen[str(ereignis.event_id)] = time.monotonic()

    subscriber("test.latenz", types=["test.*"], from_beginning=True)(handler)
    zusteller = Dispatcher(list(leeres_register.values()))
    sequenzierer = Sequencer()
    wecker = threading.Event()
    listener, _ = start_listener({NOTIFY_CHANNEL: [wecker.set], SEQUENCED_CHANNEL: [zusteller.wake]}, stopp)

    def sequenzieren() -> None:
        try:
            sequenzierer.run(stopp, wake=wecker)
        finally:
            connection.close()

    faeden = [threading.Thread(target=sequenzieren), threading.Thread(target=zusteller.run, args=(stopp,))]
    for faden in faeden:
        faden.start()
    schreiber = pg_verbindungen(autocommit=False)
    dauer: list[float] = []
    try:
        _bis(lambda: listener.healthy)
        for _ in range(20):
            event_id = roh_einfuegen(schreiber)
            schreiber.commit()
            festgeschrieben = time.monotonic()
            _bis(functools.partial(operator.contains, angekommen, str(event_id)), frist=60.0, hinweis=str(event_id))
            dauer.append(angekommen[str(event_id)] - festgeschrieben)
            time.sleep(0.05)
    finally:
        stopp.set()
        wecker.set()
        for faden in faeden:
            faden.join(timeout=FRIST)

    print(f"Latenz Commit → Sicht: p95 {_p95(dauer):.3f} s, max {max(dauer):.3f} s")
    assert _p95(dauer) <= 5.0, dauer


# --- hinter PgBouncer (nur im CI-Job „Ereignistechnik hinter PgBouncer“) ----------------------


pgbouncer = pytest.mark.skipif(not HINTER_PGBOUNCER, reason="nur mit DATABASE_URL über PgBouncer (CI-Job)")


@pgbouncer
@pytest.mark.django_db(transaction=True)
def test_hinter_pgbouncer_laeuft_die_anwendung_ueber_den_pooler(settings: Any) -> None:
    assert str(connection.settings_dict.get("PORT")) == "6432", "DATABASE_URL soll auf PgBouncer zeigen"
    assert settings.EVENTS_DB_DIRECT_URL, "EVENTS_DB_DIRECT_URL soll gesetzt sein"
    assert conninfo_to_dict(listen_conninfo()).get("port") == "5432"


@pgbouncer
@pytest.mark.django_db(transaction=True)
def test_hinter_pgbouncer_ist_listen_ueber_den_pooler_wirkungslos_und_wird_erkannt(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    protokoll_sichtbar: None,
    stopp: threading.Event,
) -> None:
    monkeypatch.setattr(wakeup, "PING_TIMEOUT", 2.0)
    monkeypatch.setattr(wakeup, "UNHEALTHY_RETRY", 60.0)
    settings.EVENTS_DB_DIRECT_URL = ""  # Lauschen über DATABASE_URL, also über PgBouncer
    ueber_pooler = listen_conninfo()
    assert conninfo_to_dict(ueber_pooler).get("port") == "6432"

    with caplog.at_level(logging.WARNING, logger="apps.events.wakeup"):
        listener = Listener({KANAL: []}, conninfo=ueber_pooler)
        faden = threading.Thread(target=listener.run, args=(stopp,), daemon=True)
        faden.start()
        _bis(
            lambda: any("EVENTS_DB_DIRECT_URL" in e.getMessage() for e in caplog.records),
            hinweis="LISTEN über PgBouncer hätte als wirkungslos erkannt werden müssen",
        )
    assert not listener.healthy
    stopp.set()
    faden.join(timeout=FRIST)


@pgbouncer
@pytest.mark.django_db(transaction=True)
def test_hinter_pgbouncer_kommen_meldungen_auf_der_direktverbindung_an(stopp: threading.Event) -> None:
    geweckt = threading.Event()
    listener, _ = start_listener({KANAL: [geweckt.set]}, stopp)
    _bis(lambda: listener.healthy, hinweis="Direktverbindung hinter PgBouncer ohne Meldungen")
    assert geweckt.wait(FRIST)
    geweckt.clear()

    # Gesendet über die Anwendung, also über PgBouncer: kommt beim Commit auf der Direktverbindung an
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, '')", [KANAL])

    assert geweckt.wait(FRIST)

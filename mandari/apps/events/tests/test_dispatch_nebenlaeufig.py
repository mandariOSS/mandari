# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zustellung unter Nebenläufigkeit (Issue #504), nur gegen PostgreSQL (CI).

- Zwei Zusteller bedienen dasselbe Abonnement gleichzeitig (etwa während einer Lease-Übernahme):
  Die Zeilensperre des Abonnements lässt jeden Effekt einer Datenbank-Sicht genau einmal eintreten.
- Ein externer Zusteller, der beim Festschreiben einen fremden Fortschritt vorfindet, verwirft sein
  Ergebnis, statt Ereignisse doppelt zu parken.
- Parallele Schreiber, Sequenzierer und Zustellung mit wiederholt scheiterndem Handler: Jedes
  festgeschriebene Ereignis wirkt genau einmal, verworfene nie, und je Objekt in Folgenummer-
  Reihenfolge.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.db import connection

from apps.events import Delivery, dispatch, registry, subscriber
from apps.events.dispatch import Dispatcher, RunResult, SubscriptionLoop, deliver_batch, ensure_subscription
from apps.events.models import Event, Lease, ParkedEvent, Subscription
from apps.events.registry import Subscriber
from apps.events.sequencer import Sequencer
from apps.events.tests.hilfen import Sicht, folgenummern, nummeriert, nur_postgres, roh_einfuegen

Verbindungen = Callable[..., psycopg.Connection[Any]]

NAME = "test.nebenlaeufig"
#: Frist für alles, was auf den Sequenzierer wartet (Cluster-Last in der CI, siehe test_sequencer.py)
FRIST = 120.0


def _abo(handler: Callable[[list[Event], Delivery], None], name: str = NAME, **optionen: Any) -> Subscriber:
    optionen.setdefault("types", ["test.*"])
    optionen.setdefault("from_beginning", True)
    subscriber(name, **optionen)(handler)
    return registry.get(name)


def _cursor(name: str = NAME) -> int:
    return Subscription.objects.get(name=name).cursor_seq


@pytest.mark.django_db(transaction=True)
def test_zwei_zusteller_ohne_lease_wirken_genau_einmal(leeres_register: dict[str, Subscriber], sicht: Sicht) -> None:
    nur_postgres()
    beteiligt: set[str] = set()

    def handler(events: list[Event], delivery: Delivery) -> None:
        beteiligt.add(threading.current_thread().name)
        sicht.schreiben(events)  # scheitert bei doppelter Zustellung (event_id eindeutig)
        time.sleep(0.005)

    spec = _abo(handler, batch=5)
    ereignisse = [nummeriert() for _ in range(60)]
    ensure_subscription(spec)
    fehler: list[BaseException] = []
    start = threading.Barrier(2)

    def zustellen() -> None:
        try:
            start.wait()
            ende = time.monotonic() + FRIST
            while _cursor() < (ereignisse[-1].seq or 0) and time.monotonic() < ende:
                deliver_batch(spec)
        except BaseException as exc:  # noqa: BLE001 – im Hauptfaden prüfen
            fehler.append(exc)
        finally:
            connection.close()

    faeden = [threading.Thread(target=zustellen, name=f"zusteller-{i}") for i in range(2)]
    for faden in faeden:
        faden.start()
    for faden in faeden:
        faden.join(timeout=FRIST)

    assert not fehler, fehler
    assert sicht.seqs() == [ereignis.seq for ereignis in ereignisse], "jeder Effekt genau einmal, in Reihenfolge"
    assert not ParkedEvent.objects.exists(), "keine doppelte Zustellung, die am Handler gescheitert wäre"
    assert beteiligt == {"zusteller-0", "zusteller-1"}, "beide Zusteller haben gearbeitet"


@pytest.mark.django_db(transaction=True)
def test_externer_zusteller_verwirft_sein_ergebnis_wenn_ein_anderer_schneller_war(
    leeres_register: dict[str, Subscriber],
) -> None:
    nur_postgres()
    a_im_handler, a_weiter = threading.Event(), threading.Event()
    zugestellt: list[int] = []

    def handler(events: list[Event], delivery: Delivery) -> None:
        if threading.current_thread().name == "a":
            a_im_handler.set()
            a_weiter.wait(FRIST)
            raise RuntimeError("a scheitert und würde parken")
        zugestellt.extend(ereignis.seq or 0 for ereignis in events)

    spec = _abo(handler, transactional=False)
    ereignisse = [nummeriert() for _ in range(3)]
    ensure_subscription(spec)
    ergebnis_a: list[RunResult] = []
    fehler: list[BaseException] = []

    def a() -> None:
        try:
            ergebnis_a.append(deliver_batch(spec))
        except BaseException as exc:  # noqa: BLE001 – im Hauptfaden prüfen
            fehler.append(exc)
        finally:
            connection.close()

    faden = threading.Thread(target=a, name="a")
    faden.start()
    try:
        assert a_im_handler.wait(FRIST)
        deliver_batch(spec)  # b schließt denselben Batch ab, während a noch im Handler steckt
    finally:
        a_weiter.set()
        faden.join(timeout=FRIST)

    assert not fehler, fehler
    assert ergebnis_a == [RunResult(more=True)], "a verwirft sein Ergebnis"
    assert zugestellt == [ereignis.seq for ereignis in ereignisse]
    assert not ParkedEvent.objects.exists()
    assert _cursor() == ereignisse[-1].seq


@pytest.mark.django_db(transaction=True)
def test_dauerbetrieb_stellt_laufend_zu_und_gibt_die_leases_frei(leeres_register: dict[str, Subscriber]) -> None:
    nur_postgres()
    erhalten: dict[str, list[int]] = defaultdict(list)

    def handler(events: list[Event], delivery: Delivery) -> None:
        erhalten[delivery.subscription].extend(ereignis.seq or 0 for ereignis in events)

    specs = [_abo(handler, name="test.eins"), _abo(handler, name="test.zwei", transactional=False)]
    stop = threading.Event()
    zusteller = Dispatcher(specs)
    faden = threading.Thread(target=zusteller.run, args=(stop, 0.05))
    faden.start()
    try:
        ereignisse = [nummeriert() for _ in range(3)]
        erwartet = [ereignis.seq for ereignis in ereignisse]
        ende = time.monotonic() + FRIST
        while erhalten["test.eins"] != erwartet or erhalten["test.zwei"] != erwartet:
            assert time.monotonic() < ende, dict(erhalten)
            time.sleep(0.05)
    finally:
        stop.set()
        faden.join(timeout=60)

    assert not faden.is_alive()
    assert not Lease.objects.filter(name__startswith="dispatch:").exists()


@pytest.mark.django_db(transaction=True)
def test_parallele_schreiber_mit_sequenzierer_und_fehlern_halten_die_reihenfolge_je_objekt(
    leeres_register: dict[str, Subscriber],
    sicht: Sicht,
    pg_verbindungen: Verbindungen,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nur_postgres()
    monkeypatch.setattr(dispatch, "BACKOFF", (timedelta(milliseconds=20),) * len(dispatch.BACKOFF))
    saat = random.randrange(1_000_000)
    zufall = random.Random(saat)
    hinweis = f"Saat {saat}"
    objekte = [uuid.uuid4() for _ in range(4)]

    # Jedes vierte neu gesehene Ereignis scheitert zweimal: im Batch und beim ersten Einzelversuch,
    # es wird also geparkt; spätere Ereignisse seines Objekts werden mitgeparkt.
    sperre = threading.Lock()
    noch_scheitern: dict[uuid.UUID, int] = {}
    wiederholungen: list[int] = []

    def handler(events: list[Event], delivery: Delivery) -> None:
        with sperre:
            if delivery.retry:
                wiederholungen.extend(ereignis.seq or 0 for ereignis in events)
            for ereignis in events:
                if ereignis.event_id not in noch_scheitern:
                    noch_scheitern[ereignis.event_id] = 2 if len(noch_scheitern) % 4 == 1 else 0
                if noch_scheitern[ereignis.event_id] > 0:
                    noch_scheitern[ereignis.event_id] -= 1
                    raise RuntimeError("vorübergehend")
        sicht.schreiben(events)

    spec = _abo(handler, batch=6)
    festgeschrieben: list[uuid.UUID] = []
    verworfen: list[uuid.UUID] = []
    verwerfer = set(zufall.sample(range(12), k=2))

    def schreiben(nummer: int, verbindung: psycopg.Connection[Any], start: threading.Barrier) -> None:
        eigener_zufall = random.Random(saat + nummer)
        start.wait()
        time.sleep(eigener_zufall.uniform(0.0, 0.5))
        eigene = [
            roh_einfuegen(verbindung, eigener_zufall.choice(objekte)) for _ in range(eigener_zufall.randint(1, 4))
        ]
        time.sleep(eigener_zufall.uniform(0.0, 0.3))
        if nummer in verwerfer:
            verbindung.rollback()
            with sperre:
                verworfen.extend(eigene)
        else:
            verbindung.commit()
            with sperre:
                festgeschrieben.extend(eigene)

    stop = threading.Event()
    sequenzierer = Sequencer(batch_size=5)
    schleife = SubscriptionLoop(spec)
    fehler: list[BaseException] = []

    def sequenzieren() -> None:
        try:
            while not stop.is_set():
                sequenzierer.drain()
                time.sleep(0.01)
        except BaseException as exc:  # noqa: BLE001 – im Hauptfaden prüfen
            fehler.append(exc)
        finally:
            sequenzierer.release()
            connection.close()

    start = threading.Barrier(12)
    schreiber = [
        threading.Thread(target=schreiben, args=(i, pg_verbindungen(autocommit=False), start)) for i in range(12)
    ]
    hintergrund = [
        threading.Thread(target=sequenzieren),
        threading.Thread(target=schleife.run, args=(stop, None, 0.02)),
    ]
    for faden in hintergrund + schreiber:
        faden.start()
    try:
        for faden in schreiber:
            faden.join(timeout=60)
        ende = time.monotonic() + FRIST
        while {event_id for event_id, _, _, _ in sicht.zeilen()} != set(festgeschrieben):
            assert time.monotonic() < ende, f"nicht alle Ereignisse zugestellt ({hinweis})"
            time.sleep(0.05)
    finally:
        stop.set()
        for faden in hintergrund:
            faden.join(timeout=60)

    assert not fehler, (fehler, hinweis)
    assert all(not faden.is_alive() for faden in schreiber + hintergrund), hinweis
    zeilen = sicht.zeilen()
    assert len(zeilen) == len(festgeschrieben), f"jeder Effekt genau einmal ({hinweis})"
    assert not set(verworfen) & {event_id for event_id, _, _, _ in zeilen}, hinweis
    je_objekt: dict[uuid.UUID, list[int]] = defaultdict(list)
    for _, objekt, seq, _ in zeilen:
        je_objekt[objekt].append(seq)
    for objekt, seqs in je_objekt.items():
        assert seqs == sorted(seqs), f"Reihenfolge je Objekt verletzt: {objekt} {seqs} ({hinweis})"
    assert wiederholungen, f"der Test soll geparkte Ereignisse enthalten ({hinweis})"
    assert not ParkedEvent.objects.exists(), hinweis
    assert None not in folgenummern(festgeschrieben), hinweis

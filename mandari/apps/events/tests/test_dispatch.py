# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zustellung an Abonnements (Issue #504, ``docs/adr/20260929-ereignistechnik-postgres.md`` Punkt 7).

Die Tests hier laufen mit SQLite und PostgreSQL; Folgenummern vergibt ``nummeriert()`` so, wie es
der Sequenzierer täte. Nebenläufigkeit (zwei Zusteller, parallele Schreiber mit Sequenzierer)
prüft ``test_dispatch_nebenlaeufig.py`` gegen PostgreSQL.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DEFAULT_DB_ALIAS, connection, connections
from django.utils import timezone
from prometheus_client import REGISTRY

from apps.events import Delivery, TargetUnavailableError, dispatch, leases, registry, subscriber
from apps.events.dispatch import (
    BACKOFF,
    MAX_ATTEMPTS,
    RunResult,
    SubscriptionLoop,
    deliver_batch,
    discard_parked,
    repair_chains,
    retry_parked,
)
from apps.events.models import Event, ParkedEvent, ParkedState, Subscription, SubscriptionState
from apps.events.registry import Subscriber, type_matches
from apps.events.tests.hilfen import Sicht, nummeriert, nur_postgres

NAME = "test.abo"


class Protokoll:
    """Handler, der Aufrufe mitschreibt und für ausgewählte Ereignisse scheitert."""

    def __init__(self, scheitert: Callable[[Event], bool] | None = None) -> None:
        self.scheitert = scheitert
        self.aufrufe: list[tuple[list[int], Delivery]] = []
        self.zugestellt: list[int] = []

    def __call__(self, events: list[Event], delivery: Delivery) -> None:
        folgenummern = [_nr(ereignis) for ereignis in events]
        self.aufrufe.append((folgenummern, delivery))
        if self.scheitert is not None and any(self.scheitert(ereignis) for ereignis in events):
            raise RuntimeError("Handler scheitert")
        self.zugestellt += folgenummern


def _abo(handler: Callable[[list[Event], Delivery], None], **optionen: Any) -> Subscriber:
    optionen.setdefault("types", ["test.*"])
    optionen.setdefault("from_beginning", True)
    subscriber(NAME, **optionen)(handler)
    return registry.get(NAME)


def _alles(spec: Subscriber) -> RunResult:
    """Läufe, bis nichts sofort Fälliges mehr übrig ist."""
    gesamt = RunResult()
    for _ in range(100):
        ergebnis = deliver_batch(spec)
        gesamt = gesamt + ergebnis
        if not ergebnis.more:
            return gesamt
    raise AssertionError("Zustellung endet nicht")


def _cursor(name: str = NAME) -> int:
    return Subscription.objects.get(name=name).cursor_seq


def _faellig_machen() -> None:
    ParkedEvent.objects.filter(state=ParkedState.WIEDERHOLEN).update(
        next_attempt_at=timezone.now() - timedelta(seconds=1)
    )


def _geparkt() -> dict[int, tuple[str, int]]:
    return {
        seq: (zustand, versuche)
        for seq, zustand, versuche in ParkedEvent.objects.values_list("event_seq", "state", "attempts")
    }


def _nr(ereignis: Event) -> int:
    assert ereignis.seq is not None
    return ereignis.seq


def _seqs(*ereignisse: Event) -> list[int]:
    return [_nr(ereignis) for ereignis in ereignisse]


# --- Register ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("muster", "typ", "trifft"),
    [
        ("ris.paper.*", "ris.paper.released", True),
        ("ris.paper.*", "ris.paper.file.added", True),
        ("ris.paper.*", "ris.paper", False),
        ("ris.paper.*", "ris.papers.released", False),
        ("ris.paper.released", "ris.paper.released", True),
        ("ris.paper.released", "ris.paper.released2", False),
        ("*", "egal.was", True),
    ],
)
def test_typmuster(muster: str, typ: str, trifft: bool) -> None:
    assert type_matches(muster, typ) is trifft


@pytest.mark.parametrize(
    "angaben",
    [
        {"name": "Gross"},
        {"types": []},
        {"types": ["ris.*.released"]},
        {"types": ["ris.paper*"]},
        {"batch": 0},
        {"queue": "Mail Queue"},
    ],
)
def test_register_lehnt_ungueltige_angaben_ab(leeres_register: dict[str, Subscriber], angaben: dict[str, Any]) -> None:
    werte: dict[str, Any] = {"name": "gut.name", "types": ["ris.*"]} | angaben
    with pytest.raises(ImproperlyConfigured):
        subscriber(werte.pop("name"), **werte)(Protokoll())
    assert leeres_register == {}


def test_name_nur_einmal_vergeben(leeres_register: dict[str, Subscriber]) -> None:
    def erster(events: list[Event], delivery: Delivery) -> None:
        pass

    def zweiter(events: list[Event], delivery: Delivery) -> None:
        pass

    subscriber("doppelt", types=["a.*"])(erster)
    subscriber("doppelt", types=["a.*"], batch=10)(erster)  # derselbe Handler erneut importiert
    with pytest.raises(ImproperlyConfigured, match="bereits registriert"):
        subscriber("doppelt", types=["a.*"])(zweiter)
    assert registry.get("doppelt").batch == 10
    assert [spec.name for spec in registry.load_subscribers()] == ["doppelt"]


# --- fortlaufende Zustellung ---------------------------------------------------------------------


@pytest.mark.django_db
def test_zustellung_in_folgenummer_reihenfolge_mit_typfilter(leeres_register: dict[str, Subscriber]) -> None:
    protokoll = Protokoll()
    spec = _abo(protokoll, types=["test.a.*", "test.b"], batch=2)
    a1 = nummeriert(type="test.a.x")
    nummeriert(type="anders.x")
    b = nummeriert(type="test.b")
    a2 = nummeriert(type="test.a.y")
    letztes = nummeriert(type="test.bx")

    ergebnis = _alles(spec)

    assert [folgenummern for folgenummern, _ in protokoll.aufrufe] == [_seqs(a1, b), _seqs(a2)]
    assert ergebnis.delivered == 3
    # Der Cursor überspringt am Ende auch nicht passende Ereignisse
    assert _cursor() == letztes.seq
    assert _alles(spec).delivered == 0


@pytest.mark.django_db
def test_neues_abonnement_beginnt_am_ende_des_journals(leeres_register: dict[str, Subscriber]) -> None:
    protokoll = Protokoll()
    vorher = nummeriert()
    spec = _abo(protokoll, from_beginning=False)
    deliver_batch(spec)
    assert _cursor() == vorher.seq
    neu = nummeriert()

    _alles(spec)

    assert protokoll.zugestellt == _seqs(neu)


@pytest.mark.django_db
def test_von_anfang_an_bekommt_auch_aeltere_ereignisse(leeres_register: dict[str, Subscriber]) -> None:
    protokoll = Protokoll()
    vorher = nummeriert()
    spec = _abo(protokoll, from_beginning=True)

    _alles(spec)

    assert protokoll.zugestellt == _seqs(vorher)


@pytest.mark.django_db
def test_pausiertes_abonnement_bekommt_nichts_und_der_cursor_bleibt(leeres_register: dict[str, Subscriber]) -> None:
    protokoll = Protokoll()
    spec = _abo(protokoll)
    deliver_batch(spec)
    Subscription.objects.filter(name=NAME).update(state=SubscriptionState.PAUSIERT)
    ereignis = nummeriert()

    assert _alles(spec) == RunResult()
    assert protokoll.aufrufe == [] and _cursor() == 0

    Subscription.objects.filter(name=NAME).update(state=SubscriptionState.AKTIV)
    _alles(spec)
    assert protokoll.zugestellt == _seqs(ereignis)


@pytest.mark.django_db
def test_schattenbetrieb_meldet_dem_handler_das_schattenziel(
    leeres_register: dict[str, Subscriber], sicht: Sicht
) -> None:
    def handler(events: list[Event], delivery: Delivery) -> None:
        sicht.schreiben(events, schatten=delivery.shadow)

    spec = _abo(handler, shadow=True)
    erstes = nummeriert()
    _alles(spec)
    assert Subscription.objects.get(name=NAME).state == SubscriptionState.SCHATTEN

    Subscription.objects.filter(name=NAME).update(state=SubscriptionState.AKTIV)
    zweites = nummeriert()
    _alles(spec)

    assert [(seq, schatten) for _, _, seq, schatten in sicht.zeilen()] == [(erstes.seq, True), (zweites.seq, False)]


@pytest.mark.django_db
def test_zustand_in_der_datenbank_gilt_nach_dem_anlegen(leeres_register: dict[str, Subscriber]) -> None:
    """Die Angabe ``shadow`` gilt nur für ein neues Abonnement; danach entscheidet die Zeile."""
    protokoll = Protokoll()
    Subscription.objects.create(name=NAME, cursor_seq=0, state=SubscriptionState.AKTIV)
    spec = _abo(protokoll, shadow=True)
    nummeriert()

    _alles(spec)

    assert [delivery.shadow for _, delivery in protokoll.aufrufe] == [False]


# --- Transaktionen -------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_sicht_handler_und_cursor_in_einer_transaktion(
    leeres_register: dict[str, Subscriber], sicht: Sicht, monkeypatch: pytest.MonkeyPatch
) -> None:
    in_transaktion: list[bool] = []

    def handler(events: list[Event], delivery: Delivery) -> None:
        in_transaktion.append(connection.in_atomic_block)
        sicht.schreiben(events)

    spec = _abo(handler)
    ereignisse = [nummeriert() for _ in range(3)]
    echter_cursor = dispatch._cursor_setzen

    def absturz(name: str, cursor: int) -> None:
        raise RuntimeError("Absturz vor dem Commit")

    monkeypatch.setattr(dispatch, "_cursor_setzen", absturz)
    with pytest.raises(RuntimeError, match="Absturz"):
        deliver_batch(spec)
    # Der Effekt des Handlers ist mit dem Cursor zurückgerollt
    assert sicht.zeilen() == []
    assert not Subscription.objects.filter(name=NAME, cursor_seq__gt=0).exists()

    monkeypatch.setattr(dispatch, "_cursor_setzen", echter_cursor)
    _alles(spec)
    _alles(spec)

    assert sicht.seqs() == _seqs(*ereignisse), "jeder Effekt genau einmal"
    assert _cursor() == ereignisse[-1].seq
    assert in_transaktion and all(in_transaktion)


@pytest.mark.django_db(transaction=True)
def test_externer_handler_ausserhalb_der_transaktion_mindestens_einmal(
    leeres_register: dict[str, Subscriber], monkeypatch: pytest.MonkeyPatch
) -> None:
    in_transaktion: list[bool] = []
    protokoll = Protokoll()

    def handler(events: list[Event], delivery: Delivery) -> None:
        in_transaktion.append(connection.in_atomic_block)
        protokoll(events, delivery)

    spec = _abo(handler, transactional=False)
    ereignisse = [nummeriert() for _ in range(2)]
    echter_cursor = dispatch._cursor_setzen

    def absturz(name: str, cursor: int) -> None:
        raise RuntimeError("Absturz vor dem Commit")

    monkeypatch.setattr(dispatch, "_cursor_setzen", absturz)
    with pytest.raises(RuntimeError):
        deliver_batch(spec)
    monkeypatch.setattr(dispatch, "_cursor_setzen", echter_cursor)
    _alles(spec)

    # Der Effekt trat vor dem Absturz ein und wird erneut zugestellt (mindestens einmal)
    assert protokoll.zugestellt == _seqs(*ereignisse) * 2
    assert in_transaktion == [False, False]
    assert _cursor() == ereignisse[-1].seq


@pytest.mark.django_db
def test_gescheiterter_batch_wird_einzeln_zugestellt_ohne_halbe_effekte(
    leeres_register: dict[str, Subscriber], sicht: Sicht
) -> None:
    ereignisse = [nummeriert() for _ in range(4)]
    kaputt = ereignisse[2].event_id

    def handler(events: list[Event], delivery: Delivery) -> None:
        for ereignis in events:
            if ereignis.event_id == kaputt:
                raise ValueError("kaputt")
            sicht.schreiben([ereignis])

    spec = _abo(handler)

    ergebnis = deliver_batch(spec)

    assert sicht.seqs() == _seqs(ereignisse[0], ereignisse[1], ereignisse[3])
    assert _geparkt() == {ereignisse[2].seq: (ParkedState.WIEDERHOLEN, 1)}
    assert ergebnis.delivered == 3 and ergebnis.parked == 1


# --- Parken, Wiederholen, tote Ereignisse --------------------------------------------------------


@pytest.mark.django_db
def test_fehler_parkt_das_objekt_samt_folgeereignissen_andere_laufen_weiter(
    leeres_register: dict[str, Subscriber],
) -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    kaputt = {a}
    protokoll = Protokoll(scheitert=lambda ereignis: ereignis.aggregate_id in kaputt)
    spec = _abo(protokoll)
    a1 = nummeriert(aggregate_id=a)
    b1 = nummeriert(aggregate_id=b)
    a2 = nummeriert(aggregate_id=a)
    c1 = nummeriert(aggregate_id=c)
    a3 = nummeriert(aggregate_id=a)
    vorher = timezone.now()

    deliver_batch(spec)

    assert protokoll.zugestellt == _seqs(b1, c1)
    assert _cursor() == a3.seq, "der Cursor läuft für andere Objekte weiter"
    assert _geparkt() == {
        a1.seq: (ParkedState.WIEDERHOLEN, 1),
        a2.seq: (ParkedState.BLOCKIERT, 0),
        a3.seq: (ParkedState.BLOCKIERT, 0),
    }
    erstes = ParkedEvent.objects.get(event_seq=_nr(a1))
    assert erstes.error_code == "builtins.RuntimeError"
    assert erstes.next_attempt_at is not None
    assert vorher + BACKOFF[0] - timedelta(seconds=5) <= erstes.next_attempt_at <= timezone.now() + BACKOFF[0]

    # Neue Ereignisse desselben Objekts werden mitgeparkt, andere zugestellt; vor der Wartezeit nichts
    a4 = nummeriert(aggregate_id=a)
    b2 = nummeriert(aggregate_id=b)
    _alles(spec)
    assert protokoll.zugestellt == _seqs(b1, c1, b2)
    assert _geparkt()[_nr(a4)] == (ParkedState.BLOCKIERT, 0)

    # Nach der Wartezeit: das Objekt in seiner Reihenfolge, danach ist nichts mehr geparkt
    kaputt.clear()
    _faellig_machen()
    _alles(spec)
    assert protokoll.zugestellt == _seqs(b1, c1, b2, a1, a2, a3, a4)
    assert _geparkt() == {}


@pytest.mark.django_db
def test_folgeereignis_scheitert_nach_dem_ersten_und_wird_selbst_zum_ersten(
    leeres_register: dict[str, Subscriber],
) -> None:
    a = uuid.uuid4()
    kaputt: set[uuid.UUID] = set()
    protokoll = Protokoll(scheitert=lambda ereignis: ereignis.event_id in kaputt)
    spec = _abo(protokoll)
    a1 = nummeriert(aggregate_id=a)
    a2 = nummeriert(aggregate_id=a)
    a3 = nummeriert(aggregate_id=a)
    kaputt.add(a1.event_id)
    deliver_batch(spec)

    kaputt.clear()
    kaputt.add(a2.event_id)
    _faellig_machen()
    _alles(spec)

    assert protokoll.zugestellt == _seqs(a1)
    assert _geparkt() == {a2.seq: (ParkedState.WIEDERHOLEN, 1), a3.seq: (ParkedState.BLOCKIERT, 0)}


@pytest.mark.django_db
def test_wartezeiten_wachsen_nach_acht_versuchen_tot_mit_alarm(
    leeres_register: dict[str, Subscriber],
    settings: Any,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,
) -> None:
    cache.clear()
    # Die App-Logger geben nicht an die Wurzel weiter (apps.common.observability); caplog hängt dort
    monkeypatch.setattr(logging.getLogger("apps"), "propagate", True)
    settings.INSIGHT_ALERT_EMAILS = ["betrieb@example.org"]
    a, b = uuid.uuid4(), uuid.uuid4()
    protokoll = Protokoll(scheitert=lambda ereignis: ereignis.aggregate_id == a)
    spec = _abo(protokoll)
    a1 = nummeriert(aggregate_id=a)
    a2 = nummeriert(aggregate_id=a)
    tote_vorher = REGISTRY.get_sample_value("mandari_events_dead_total", {"subscription": NAME}) or 0.0
    deliver_batch(spec)

    for versuch in range(2, MAX_ATTEMPTS + 1):
        geparkt = ParkedEvent.objects.get(event_seq=_nr(a1))
        assert (geparkt.state, geparkt.attempts) == (ParkedState.WIEDERHOLEN, versuch - 1)
        _faellig_machen()
        vorher = timezone.now()
        with django_capture_on_commit_callbacks(execute=True), caplog.at_level(logging.ERROR, "apps.events"):
            deliver_batch(spec)
        geparkt.refresh_from_db()
        if versuch < MAX_ATTEMPTS:
            assert geparkt.next_attempt_at is not None
            warten = BACKOFF[versuch - 1]
            assert vorher + warten - timedelta(seconds=5) <= geparkt.next_attempt_at <= timezone.now() + warten

    assert (geparkt.state, geparkt.attempts, geparkt.next_attempt_at) == (ParkedState.TOT, MAX_ATTEMPTS, None)
    assert geparkt.error_code == "builtins.RuntimeError"
    assert _geparkt()[_nr(a2)] == (ParkedState.BLOCKIERT, 0)
    einzeln = [folgenummern for folgenummern, _ in protokoll.aufrufe if folgenummern == _seqs(a1)]
    assert len(einzeln) == MAX_ATTEMPTS, "erster Versuch (nach dem gescheiterten Batch) und sieben Wiederholungen"
    # Alarm: Fehlerprotokoll, Zähler und Alarmmail
    assert any("tot" in eintrag.getMessage() and eintrag.levelno == logging.ERROR for eintrag in caplog.records)
    assert REGISTRY.get_sample_value("mandari_events_dead_total", {"subscription": NAME}) == tote_vorher + 1
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["betrieb@example.org"]
    assert f"Tote Ereignisse im Abonnement {NAME}" in mail.outbox[0].body

    # Tote Ereignisse halten nur ihr Objekt auf; wiederholt wird nichts mehr
    b1 = nummeriert(aggregate_id=b)
    a3 = nummeriert(aggregate_id=a)
    _faellig_machen()
    _alles(spec)
    assert protokoll.zugestellt == _seqs(b1)
    assert _geparkt()[_nr(a3)] == (ParkedState.BLOCKIERT, 0)
    assert len([f for f, _ in protokoll.aufrufe if f == _seqs(a1)]) == MAX_ATTEMPTS, "ein totes wird nicht wiederholt"


@pytest.mark.django_db
def test_ziel_nicht_erreichbar_parkt_nichts_und_zaehlt_keinen_versuch(
    leeres_register: dict[str, Subscriber],
) -> None:
    erreichbar = [False]
    zugestellt: list[int] = []

    def handler(events: list[Event], delivery: Delivery) -> None:
        if not erreichbar[0]:
            raise TargetUnavailableError
        zugestellt.extend(_nr(ereignis) for ereignis in events)

    spec = _abo(handler)
    ereignisse = [nummeriert() for _ in range(3)]
    ParkedEvent.objects.create(
        subscription=NAME,
        event_seq=_nr(ereignisse[0]),
        aggregate_id=ereignisse[0].aggregate_id,
        state=ParkedState.WIEDERHOLEN,
        attempts=2,
        next_attempt_at=timezone.now() - timedelta(seconds=1),
    )
    Subscription.objects.create(name=NAME, cursor_seq=_nr(ereignisse[0]))

    with pytest.raises(TargetUnavailableError):
        deliver_batch(spec)
    schleife = SubscriptionLoop(spec)
    assert schleife.drain() == 0
    assert schleife.paused
    assert _geparkt() == {ereignisse[0].seq: (ParkedState.WIEDERHOLEN, 2)}
    assert _cursor() == ereignisse[0].seq

    erreichbar[0] = True
    assert schleife.drain() == 0, "während der Pause wird nicht zugestellt"
    schleife._paused_until = 0.0
    assert schleife.drain() == 3
    assert zugestellt == _seqs(*ereignisse)
    assert _geparkt() == {}
    schleife.release()


@pytest.mark.django_db
def test_verwerfen_und_sofort_wiederholen(leeres_register: dict[str, Subscriber]) -> None:
    a = uuid.uuid4()
    kaputt = {a}
    protokoll = Protokoll(scheitert=lambda ereignis: ereignis.aggregate_id in kaputt)
    spec = _abo(protokoll)
    a1, a2, a3 = (nummeriert(aggregate_id=a) for _ in range(3))
    deliver_batch(spec)
    ParkedEvent.objects.filter(event_seq=_nr(a1)).update(state=ParkedState.TOT, attempts=MAX_ATTEMPTS)
    id_von = dict(ParkedEvent.objects.values_list("event_seq", "pk"))

    assert not retry_parked(id_von[_nr(a2)]), "ein Folgeereignis darf nicht vorgezogen werden"
    assert discard_parked(id_von[_nr(a1)])
    assert _geparkt() == {a2.seq: (ParkedState.WIEDERHOLEN, 0), a3.seq: (ParkedState.BLOCKIERT, 0)}
    _alles(spec)
    assert _geparkt()[_nr(a2)] == (ParkedState.WIEDERHOLEN, 1)

    ParkedEvent.objects.filter(event_seq=_nr(a2)).update(state=ParkedState.TOT, attempts=MAX_ATTEMPTS)
    kaputt.clear()
    assert retry_parked(id_von[_nr(a2)])
    _alles(spec)
    assert protokoll.zugestellt == _seqs(a2, a3)
    assert _geparkt() == {}
    assert not discard_parked(id_von[_nr(a1)]) and not retry_parked(id_von[_nr(a1)])


@pytest.mark.django_db
def test_kette_ohne_erstes_ereignis_wird_repariert(leeres_register: dict[str, Subscriber]) -> None:
    protokoll = Protokoll()
    spec = _abo(protokoll)
    a = uuid.uuid4()
    a1, a2 = nummeriert(aggregate_id=a), nummeriert(aggregate_id=a)
    Subscription.objects.create(name=NAME, cursor_seq=_nr(a2))
    # Das erste Ereignis wurde von Hand gelöscht, das zweite hängt als blockiert
    ParkedEvent.objects.create(subscription=NAME, event_seq=_nr(a2), aggregate_id=a, state=ParkedState.BLOCKIERT)

    assert repair_chains(NAME) == 1
    _alles(spec)

    assert protokoll.zugestellt == _seqs(a2)
    assert a1.seq not in protokoll.zugestellt


@pytest.mark.django_db
def test_geparktes_ereignis_ohne_journaleintrag_haelt_die_kette_nicht_auf(
    leeres_register: dict[str, Subscriber],
) -> None:
    protokoll = Protokoll()
    spec = _abo(protokoll)
    a = uuid.uuid4()
    a2 = nummeriert(aggregate_id=a)
    Subscription.objects.create(name=NAME, cursor_seq=_nr(a2))
    ParkedEvent.objects.create(
        subscription=NAME,
        event_seq=_nr(a2) + 1000,  # längst aus dem Journal gelöscht
        aggregate_id=a,
        state=ParkedState.WIEDERHOLEN,
        attempts=3,
        next_attempt_at=timezone.now() - timedelta(seconds=1),
    )
    ParkedEvent.objects.create(subscription=NAME, event_seq=_nr(a2), aggregate_id=a, state=ParkedState.BLOCKIERT)

    _alles(spec)

    assert protokoll.zugestellt == _seqs(a2)
    assert _geparkt() == {}


@pytest.mark.django_db
def test_extern_kopf_waehrend_der_zustellung_verworfen_die_kette_bleibt_zustellbar(
    leeres_register: dict[str, Subscriber],
) -> None:
    """Ein externer Lauf liest die geparkten Objekte ohne Sperre; ein Verwerfen dazwischen darf keine Kette verwaisen."""
    a, b = uuid.uuid4(), uuid.uuid4()
    zugestellt: list[int] = []
    eingriff: list[int] = []

    def handler(events: list[Event], delivery: Delivery) -> None:
        # Während der Batch läuft, verwirft jemand im Betrieb das tote erste Ereignis von a
        while eingriff:
            assert discard_parked(eingriff.pop())
        zugestellt.extend(_nr(ereignis) for ereignis in events)

    spec = _abo(handler, transactional=False)
    a1 = nummeriert(aggregate_id=a)
    Subscription.objects.create(name=NAME, cursor_seq=_nr(a1))
    tot = ParkedEvent.objects.create(
        subscription=NAME,
        event_seq=_nr(a1),
        aggregate_id=a,
        state=ParkedState.TOT,
        attempts=MAX_ATTEMPTS,
        error_code="builtins.RuntimeError",
    )
    b1 = nummeriert(aggregate_id=b)
    a2 = nummeriert(aggregate_id=a)
    eingriff.append(tot.pk)

    _alles(spec)

    assert zugestellt == _seqs(b1, a2), "a2 rückt nach, statt ohne erstes Ereignis liegen zu bleiben"
    assert _geparkt() == {}


# --- Leases, Metriken, Dienstgüte ----------------------------------------------------------------


@pytest.mark.django_db
def test_nur_der_inhaber_der_lease_stellt_zu(leeres_register: dict[str, Subscriber]) -> None:
    protokoll = Protokoll()
    spec = _abo(protokoll)
    nummeriert()
    assert leases.acquire(spec.lease_name, "anderer-prozess")

    schleife = SubscriptionLoop(spec)
    assert schleife.drain() == 0
    assert not schleife.is_leader and protokoll.aufrufe == []

    leases.release(spec.lease_name, "anderer-prozess")
    assert schleife.drain() == 1
    schleife.release()
    assert leases.acquire(spec.lease_name, "noch-einer"), "freigegeben"


@pytest.mark.django_db(transaction=True)  # ohne umschließende Testtransaktion, wie im Betrieb
@pytest.mark.parametrize("transaktional", [False, True])
def test_langer_batch_meldet_lebenszeichen_und_verlaengert_die_lease(
    leeres_register: dict[str, Subscriber], monkeypatch: pytest.MonkeyPatch, transaktional: bool
) -> None:
    """
    Issue #821: Ein Batch darf länger dauern als die Lease (30 s) und ``STALE_AFTER`` des Workers. Der Handler
    meldet über ``Delivery.alive`` Lebenszeichen; die Schleife gibt sie an den Worker weiter und verlängert
    die fällige Lease, aber nicht in der Transaktion eines transaktionalen Handlers (dort käme die
    Verlängerung erst mit dem Commit an).
    """
    schlaege: list[str] = []
    verlaengert: list[bool] = []
    original = leases.acquire

    def zaehlen(name: str, holder: str, *args: Any, **kwargs: Any) -> bool:
        verlaengert.append(True)
        return original(name, holder, *args, **kwargs)

    def lange(events: list[Event], delivery: Delivery) -> None:
        schlaege.append("handler")
        schleife._renewed_at -= leases.RENEW_INTERVAL.total_seconds() + 1  # als wäre die Erneuerung fällig
        verlaengert.clear()
        monkeypatch.setattr(leases, "acquire", zaehlen)
        delivery.alive()
        monkeypatch.setattr(leases, "acquire", original)

    spec = _abo(lange, transactional=transaktional)
    nummeriert()
    schleife = SubscriptionLoop(spec)

    assert schleife.drain(beat=lambda: schlaege.append("beat")) == 1

    # je Batch ein Lebenszeichen, dazu das aus dem Handler
    assert schlaege[0] == "beat"
    assert schlaege[schlaege.index("handler") + 1] == "beat"
    assert verlaengert == ([] if transaktional else [True])
    assert schleife.is_leader
    schleife.release()


class _WeckerMitBlick:
    """Wecksignal, das beim Warten festhält, ob der wartende Faden eine Datenbankverbindung belegt."""

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.verbindung_offen: list[bool] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.verbindung_offen.append(connections[DEFAULT_DB_ALIAS].connection is not None)
        self.stop.set()
        return True

    def clear(self) -> None:
        pass


@pytest.mark.django_db(transaction=True)
def test_dauerbetrieb_gibt_die_verbindung_vor_dem_warten_zurueck(
    leeres_register: dict[str, Subscriber], monkeypatch: pytest.MonkeyPatch
) -> None:
    nur_postgres()  # Die SQLite-Testdatenbank liegt im Speicher; Django schließt deren Verbindung nie
    # Wie mit Verbindungspool (dort ist CONN_MAX_AGE immer 0): nach der Arbeit geht die Verbindung zurück
    monkeypatch.setitem(connection.settings_dict, "CONN_MAX_AGE", 0)
    protokoll = Protokoll()
    spec = _abo(protokoll)
    nummeriert()
    stop = threading.Event()
    wecker = _WeckerMitBlick(stop)
    schleife = SubscriptionLoop(spec)

    faden = threading.Thread(target=schleife.run, args=(stop, wecker, 0.01))
    faden.start()
    faden.join(timeout=30)

    assert not faden.is_alive()
    assert len(protokoll.zugestellt) == 1, "die Runde hat die Datenbank benutzt"
    assert wecker.verbindung_offen == [False], "beim Warten belegt der Faden keine Verbindung"


@pytest.mark.django_db
def test_metrik_und_dienstguete_zeigen_geparkte_und_tote_ereignisse(leeres_register: dict[str, Subscriber]) -> None:
    from apps.common.service_levels import pruefe_tote_ereignisse
    from apps.events.metrics import parked_counts

    [befund] = pruefe_tote_ereignisse()
    assert befund.ok

    a = uuid.uuid4()
    for seq, zustand in ((1, ParkedState.TOT), (2, ParkedState.BLOCKIERT), (3, ParkedState.WIEDERHOLEN)):
        ParkedEvent.objects.create(
            subscription=NAME, event_seq=seq, aggregate_id=a, state=zustand, error_code="builtins.ValueError"
        )

    assert parked_counts() == {(NAME, "tot"): 1, (NAME, "blockiert"): 1, (NAME, "wiederholen"): 1}
    assert REGISTRY.get_sample_value("mandari_events_parked", {"subscription": NAME, "state": "tot"}) == 1.0
    [befund] = pruefe_tote_ereignisse()
    assert not befund.ok
    assert befund.key == f"events:tot:{NAME}"
    assert "builtins.ValueError" in befund.detail


# --- Befehl --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_befehl_once_liste_und_eingriffe(leeres_register: dict[str, Subscriber]) -> None:
    a = uuid.uuid4()
    protokoll = Protokoll(scheitert=lambda ereignis: ereignis.aggregate_id == a)
    _abo(protokoll)
    gut = nummeriert()
    schlecht = nummeriert(aggregate_id=a)
    ausgabe = StringIO()

    call_command("events_dispatch", "--once", stdout=ausgabe)
    assert "1 Ereignisse zugestellt" in ausgabe.getvalue()
    assert protokoll.zugestellt == _seqs(gut)
    assert leases.acquire(f"dispatch:{NAME}", "x"), "die Lease ist nach --once frei"
    leases.release(f"dispatch:{NAME}", "x")

    ParkedEvent.objects.update(state=ParkedState.TOT)
    geparkt_id = ParkedEvent.objects.get().pk
    ausgabe = StringIO()
    call_command("events_dispatch", "--list", stdout=ausgabe)
    text = ausgabe.getvalue()
    assert NAME in text and "0/0/1" in text
    assert f"tot: ID {geparkt_id}" in text and f"Folgenummer {schlecht.seq}" in text

    call_command("events_dispatch", "--retry-parked", str(geparkt_id), stdout=StringIO())
    assert _geparkt() == {schlecht.seq: (ParkedState.WIEDERHOLEN, 0)}
    call_command("events_dispatch", "--discard-parked", str(geparkt_id), stdout=StringIO())
    assert _geparkt() == {}
    with pytest.raises(CommandError):
        call_command("events_dispatch", "--discard-parked", str(geparkt_id))
    with pytest.raises(CommandError, match="Nicht registriert"):
        call_command("events_dispatch", "--once", "--subscription", "gibt.es.nicht")


@pytest.mark.django_db
def test_befehl_waehlt_nach_warteschlange_und_meldet_fremde_lease(leeres_register: dict[str, Subscriber]) -> None:
    protokoll = Protokoll()
    subscriber("index.abo", types=["test.*"], queue="index", from_beginning=True)(protokoll)
    subscriber("mail.abo", types=["test.*"], queue="mail", from_beginning=True)(Protokoll())
    nummeriert()
    assert leases.acquire("dispatch:mail.abo", "anderer-prozess")
    ausgabe = StringIO()

    call_command("events_dispatch", "--once", "--queues", "index,mail", stdout=ausgabe)

    assert "Übersprungen (Lease hält ein anderer Prozess): mail.abo" in ausgabe.getvalue()
    assert len(protokoll.zugestellt) == 1
    ausgabe = StringIO()
    call_command("events_dispatch", "--once", "--queues", "ai", stdout=ausgabe)
    assert "Keine Abonnements" in ausgabe.getvalue()

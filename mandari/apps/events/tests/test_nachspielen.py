# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nachspielen ab Folgenummer oder Zeitpunkt (``dispatch.replay``, ``events_dispatch --replay``, Issues #526, #511).

Der Cursor geht nur zurück; danach stellt die Zustellung die Ereignisse erneut zu, der Handler muss
das vertragen. Sicht löschen und nachspielen ergibt denselben Zustand, auch im Schattenbetrieb und mit
geparkten Ereignissen.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone

from apps.events import Delivery, subscriber
from apps.events.dispatch import Replay, deliver_batch, ensure_subscription, first_seq_since, replay, rewind
from apps.events.models import Event, ParkedEvent, ParkedState, Subscription, SubscriptionState
from apps.events.registry import Subscriber, get
from apps.events.tests.hilfen import Sicht, nummeriert

NAME = "test.nachspielen"


def _abonnement(zugestellt: list[int]) -> Subscriber:
    def handler(events: list[Event], delivery: Delivery) -> None:
        zugestellt.extend(ereignis.seq or 0 for ereignis in events)

    subscriber(NAME, types=["test.*"], transactional=False)(handler)
    return get(NAME)


@pytest.mark.django_db
def test_nachspielen_stellt_ab_folgenummer_erneut_zu(leeres_register: dict[str, Subscriber]) -> None:
    zugestellt: list[int] = []
    spec = _abonnement(zugestellt)
    ensure_subscription(spec)
    folgenummern = [nummeriert().seq or 0 for _ in range(4)]
    deliver_batch(spec)
    assert zugestellt == folgenummern

    assert rewind(NAME, folgenummern[2]) == (folgenummern[-1], folgenummern[1])
    deliver_batch(spec)
    assert zugestellt == folgenummern + folgenummern[2:]
    assert Subscription.objects.get(name=NAME).cursor_seq == folgenummern[-1]


@pytest.mark.django_db
def test_nachspielen_nur_rueckwaerts(leeres_register: dict[str, Subscriber]) -> None:
    spec = _abonnement([])
    ensure_subscription(spec)
    erstes = nummeriert().seq or 0
    with pytest.raises(ValueError, match="nur zurück"):
        rewind(NAME, erstes + 1)  # Cursor steht vor dem Ereignis: vorwärts übersprünge es
    with pytest.raises(ValueError):
        rewind(NAME, 0)
    assert rewind("gibt.es.nicht", 1) is None


@pytest.mark.django_db
def test_erste_folgenummer_seit_zeitpunkt() -> None:
    alt = nummeriert()
    Event.objects.filter(pk=alt.pk).update(recorded_at=timezone.now() - timedelta(days=2))
    neu = nummeriert()
    assert first_seq_since(timezone.now() - timedelta(days=1)) == neu.seq
    assert first_seq_since(timezone.now() + timedelta(days=1)) is None


@pytest.mark.django_db
def test_befehl_replay(leeres_register: dict[str, Subscriber]) -> None:
    spec = _abonnement([])
    ensure_subscription(spec)
    erstes, zweites = nummeriert(), nummeriert()
    deliver_batch(spec)

    ausgabe = StringIO()
    call_command("events_dispatch", "--replay", NAME, "--from-seq", str(zweites.seq), stdout=ausgabe)
    assert f"Cursor {zweites.seq} -> {erstes.seq}" in ausgabe.getvalue()

    Event.objects.filter(pk=erstes.pk).update(recorded_at=timezone.now() - timedelta(days=3))
    gestern = (timezone.localtime() - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M")
    ausgabe = StringIO()
    call_command("events_dispatch", "--replay", NAME, "--since", gestern, stdout=ausgabe)
    assert f"-> {(erstes.seq or 0) - 1}" in ausgabe.getvalue()

    with pytest.raises(CommandError, match="genau eines"):
        call_command("events_dispatch", "--replay", NAME)
    with pytest.raises(CommandError, match="genau eines"):
        call_command("events_dispatch", "--replay", NAME, "--from-seq", "1", "--since", gestern)
    with pytest.raises(CommandError, match="Format"):
        call_command("events_dispatch", "--replay", NAME, "--since", "gestern")
    with pytest.raises(CommandError, match="Format"):
        call_command("events_dispatch", "--replay", NAME, "--since", "2026-13-01T00:00")
    with pytest.raises(CommandError, match="gibt es nicht"):
        call_command("events_dispatch", "--replay", "unbekannt", "--from-seq", "1")


@pytest.mark.django_db
def test_eingriffe_der_kommandozeile_stehen_im_sicherheitsprotokoll(leeres_register: dict[str, Subscriber]) -> None:
    from apps.accounts.models import SecurityAuditLog

    spec = _abonnement([])
    ensure_subscription(spec)
    erstes, zweites = nummeriert(), nummeriert()
    deliver_batch(spec)
    geparkt = ParkedEvent.objects.create(
        subscription=NAME, event_seq=erstes.seq or 0, aggregate_id=uuid.uuid4(), state=ParkedState.TOT, attempts=5
    )
    # Ab der Folgenummer des Nachspielens hebt es geparkte Ereignisse auf (die Zustellung erreicht sie wieder)
    ParkedEvent.objects.create(
        subscription=NAME, event_seq=zweites.seq or 0, aggregate_id=uuid.uuid4(), state=ParkedState.TOT, attempts=8
    )

    call_command("events_dispatch", "--replay", NAME, "--from-seq", str(zweites.seq), stdout=StringIO())
    call_command("events_dispatch", "--replay", NAME, "--from-seq", str(zweites.seq), stdout=StringIO())  # ohne Wirkung
    assert list(ParkedEvent.objects.values_list("pk", flat=True)) == [geparkt.pk]
    call_command("events_dispatch", "--retry-parked", str(geparkt.pk), stdout=StringIO())
    call_command("events_dispatch", "--discard-parked", str(geparkt.pk), stdout=StringIO())
    with pytest.raises(CommandError):
        call_command("events_dispatch", "--discard-parked", str(geparkt.pk))

    eintraege = list(SecurityAuditLog.objects.filter(event="betrieb").order_by("created_at"))
    assert [eintrag.details["aktion"] for eintrag in eintraege] == [
        "abonnement_nachspielen",
        "geparkt_wiederholen",
        "geparkt_verworfen",
    ]
    assert all(eintrag.details["quelle"] == "kommandozeile" for eintrag in eintraege)
    assert all(eintrag.details["befehl"] == "events_dispatch" for eintrag in eintraege)
    assert eintraege[0].details["abonnement"] == NAME
    assert eintraege[0].details["nachher"] == (zweites.seq or 0) - 1
    assert eintraege[0].details["geparkt_aufgehoben"] == 1
    assert eintraege[1].details["zustand"] == ParkedState.TOT and eintraege[1].details["versuche"] == 5
    assert eintraege[2].details["folgenummer"] == erstes.seq


# --- Sicht löschen → Nachspielen → identischer Zustand (Issue #511) --------------------------------

SICHT = "test.nachspielen.sicht"


class _Fehler:
    """Lässt den Handler an Ereignissen der genannten Objekte scheitern."""

    def __init__(self) -> None:
        self.objekte: set[uuid.UUID] = set()

    def pruefen(self, events: list[Event]) -> None:
        if any(ereignis.aggregate_id in self.objekte for ereignis in events):
            raise RuntimeError("Ziel lehnt ab")


def _sicht_abonnement(sicht: Sicht, fehler: _Fehler, *, shadow: bool = False) -> Subscriber:
    def handler(events: list[Event], delivery: Delivery) -> None:
        fehler.pruefen(events)
        sicht.schreiben(events, schatten=delivery.shadow)

    subscriber(SICHT, types=["test.*"], from_beginning=True, shadow=shadow)(handler)
    spec = get(SICHT)
    ensure_subscription(spec)
    return spec


def _leeren(spec: Subscriber) -> None:
    while deliver_batch(spec).more:
        pass


def _faellig_machen() -> None:
    ParkedEvent.objects.filter(state=ParkedState.WIEDERHOLEN).update(
        next_attempt_at=timezone.now() - timedelta(hours=1)
    )


def _pruefsumme(sicht: Sicht) -> str:
    """Inhalt der Sicht unabhängig von der Reihenfolge der Zeilen, dazu die Reihenfolge je Objekt."""
    zeilen = sicht.zeilen()
    inhalt = sorted((str(e), str(a), s, sch) for e, a, s, sch in zeilen)
    je_objekt: dict[str, list[int]] = {}
    for _, objekt, seq, _ in zeilen:
        je_objekt.setdefault(str(objekt), []).append(seq)
    return hashlib.sha256(repr((inhalt, sorted(je_objekt.items()))).encode()).hexdigest()


def _sicht_leeren(sicht: Sicht) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {sicht.TABELLE}")  # noqa: S608 – fester Tabellenname


@pytest.mark.django_db
@pytest.mark.parametrize("zustand", [SubscriptionState.AKTIV, SubscriptionState.SCHATTEN])
def test_sicht_loeschen_und_nachspielen_ergibt_denselben_zustand(
    leeres_register: dict[str, Subscriber], sicht: Sicht, zustand: str
) -> None:
    fehler = _Fehler()
    spec = _sicht_abonnement(sicht, fehler, shadow=zustand == SubscriptionState.SCHATTEN)
    assert Subscription.objects.get(name=SICHT).state == zustand
    objekte = [uuid.uuid4() for _ in range(4)]
    for i in range(12):
        nummeriert(aggregate_id=objekte[i % 4])
    # Ein Objekt scheitert zeitweise: geparkt, danach in Reihenfolge nachgeholt
    fehler.objekte = {objekte[0]}
    _leeren(spec)
    assert ParkedEvent.objects.filter(subscription=SICHT).count() == 3
    fehler.objekte = set()
    _faellig_machen()
    _leeren(spec)
    assert not ParkedEvent.objects.exists()
    vorher = _pruefsumme(sicht)
    assert len(sicht.zeilen()) == 12
    assert all(schatten == (zustand == SubscriptionState.SCHATTEN) for *_, schatten in sicht.zeilen())

    _sicht_leeren(sicht)
    call_command("events_dispatch", "--replay", SICHT, "--from-seq", "1", stdout=StringIO())
    _leeren(spec)

    assert _pruefsumme(sicht) == vorher
    assert Subscription.objects.get(name=SICHT).state == zustand


@pytest.mark.django_db
def test_nachspielen_hebt_geparkte_auf_und_haelt_die_reihenfolge_je_objekt(
    leeres_register: dict[str, Subscriber], sicht: Sicht
) -> None:
    """Blieben sie geparkt, würde das schon zugestellte erste Ereignis hinter ihnen mitgeparkt (Reihenfolge)."""
    fehler = _Fehler()
    spec = _sicht_abonnement(sicht, fehler)
    objekt, anderes = uuid.uuid4(), uuid.uuid4()
    erstes = nummeriert(aggregate_id=objekt)
    _leeren(spec)
    zweites, drittes = nummeriert(aggregate_id=objekt), nummeriert(aggregate_id=objekt)
    nummeriert(aggregate_id=anderes)
    fehler.objekte = {objekt}
    _leeren(spec)
    assert set(ParkedEvent.objects.values_list("event_seq", "state")) == {
        (zweites.seq, ParkedState.WIEDERHOLEN),
        (drittes.seq, ParkedState.BLOCKIERT),
    }

    fehler.objekte = set()
    ergebnis = replay(SICHT, erstes.seq or 0)
    assert ergebnis is not None and ergebnis.parked_removed == 2
    assert not ParkedEvent.objects.exists()
    _sicht_leeren(sicht)
    _faellig_machen()
    _leeren(spec)

    assert [seq for _, a, seq, _ in sicht.zeilen() if a == objekt] == [erstes.seq, zweites.seq, drittes.seq]
    # Ein zweites Mal mit derselben Folgenummer ändert nichts mehr
    cursor = Subscription.objects.get(name=SICHT).cursor_seq
    assert replay(SICHT, cursor + 1) == Replay(before=cursor, after=cursor)


@pytest.mark.django_db
def test_nachspielen_laesst_aeltere_kettenglieder_geparkt(leeres_register: dict[str, Subscriber], sicht: Sicht) -> None:
    """Liegt der Kopf einer Kette vor der Folgenummer, bleibt er; die Folgeereignisse parkt die Zustellung neu."""
    fehler = _Fehler()
    spec = _sicht_abonnement(sicht, fehler)
    objekt = uuid.uuid4()
    kopf, zweites, drittes = (nummeriert(aggregate_id=objekt) for _ in range(3))
    fehler.objekte = {objekt}
    _leeren(spec)

    ergebnis = replay(SICHT, zweites.seq or 0)
    assert ergebnis is not None and ergebnis.parked_removed == 2
    _leeren(spec)
    assert list(ParkedEvent.objects.order_by("event_seq").values_list("event_seq", "state")) == [
        (kopf.seq, ParkedState.WIEDERHOLEN),
        (zweites.seq, ParkedState.BLOCKIERT),
        (drittes.seq, ParkedState.BLOCKIERT),
    ]
    fehler.objekte = set()
    _faellig_machen()
    _leeren(spec)
    assert sicht.seqs() == [kopf.seq, zweites.seq, drittes.seq]


@pytest.mark.django_db
def test_externer_lauf_verwirft_sein_ergebnis_wenn_waehrenddessen_nachgespielt_wird(
    leeres_register: dict[str, Subscriber],
) -> None:
    """Abstimmung mit dem laufenden Worker: Ein Lauf schreibt seinen veralteten Cursor nicht fest."""
    zugestellt: list[int] = []
    erstes, zweites = nummeriert(), nummeriert()

    def handler(events: list[Event], delivery: Delivery) -> None:
        zugestellt.extend(ereignis.seq or 0 for ereignis in events)
        if len(zugestellt) == 1:  # Nachspielen kommt, während der Lauf noch arbeitet
            replay(NAME, erstes.seq or 0)

    subscriber(NAME, types=["test.*"], transactional=False, from_beginning=True, batch=1)(handler)
    spec = get(NAME)
    ensure_subscription(spec)
    Subscription.objects.filter(name=NAME).update(cursor_seq=erstes.seq)

    ergebnis = deliver_batch(spec)
    assert ergebnis.delivered == 0 and ergebnis.more
    assert Subscription.objects.get(name=NAME).cursor_seq == (erstes.seq or 0) - 1
    _leeren(spec)
    assert zugestellt == [zweites.seq, erstes.seq, zweites.seq]
    assert Subscription.objects.get(name=NAME).cursor_seq == zweites.seq


@pytest.mark.django_db
def test_nachspielen_im_aufgeraeumten_teil_weist_darauf_hin(leeres_register: dict[str, Subscriber]) -> None:
    from apps.events import pruning

    spec = _abonnement([])
    ensure_subscription(spec)
    erstes, zweites = nummeriert(), nummeriert()
    deliver_batch(spec)
    pruning.record(erstes.seq or 0, timezone.now() - timedelta(days=90))

    ausgabe = StringIO()
    call_command("events_dispatch", "--replay", NAME, "--from-seq", str(erstes.seq), stdout=ausgabe)
    assert f"bis Folgenummer {erstes.seq} aufgeräumt" in ausgabe.getvalue()
    ausgabe = StringIO()
    Subscription.objects.filter(name=NAME).update(cursor_seq=zweites.seq)
    call_command("events_dispatch", "--replay", NAME, "--from-seq", str(zweites.seq), stdout=ausgabe)
    assert "aufgeräumt" not in ausgabe.getvalue()

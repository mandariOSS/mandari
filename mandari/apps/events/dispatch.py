# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zustellung an Abonnements (``docs/adr/20260929-ereignistechnik-postgres.md``, Punkt 7).

Ein Abonnement (``apps.events.registry``) hat einen Cursor: die Folgenummer des zuletzt
verarbeiteten Ereignisses. Ein Lauf (``deliver_batch``) hat zwei Schritte:

1. **Wiederholen:** fällige geparkte Ereignisse erneut zustellen (höchstens ein Batch).
2. **Fortlaufend:** Ereignisse mit ``seq > cursor`` und passendem Typ in Folgenummer-Reihenfolge
   lesen, dem Handler übergeben und den Cursor fortschreiben. Gelesen wird nur bis zur höchsten
   Folgenummer zu Beginn des Laufs. Der Sequenzierer macht Nummern in aufsteigender Reihenfolge
   sichtbar (``docs/adr/20260929-sequenzierer.md``), darunter kommt nichts mehr hinzu; Ereignisse
   anderer Typen überspringt der Cursor deshalb gefahrlos.

**Parken je Objekt** (``events_parked``): Scheitert der Handler an einem Batch, wird der Batch
einzeln zugestellt, damit nur die fehlerhaften Ereignisse zurückbleiben. Ein gescheitertes Ereignis
wird geparkt (``wiederholen``) und nach 10 s, 30 s, 2 min, 10 min, 1 h, 3 h und 6 h wiederholt;
scheitert auch der achte Versuch, ist es ``tot``, und ein Alarm geht hinaus. Solange ein Objekt
geparkte Ereignisse hat, werden seine Folgeereignisse mitgeparkt (``blockiert``); der Cursor läuft
für alle anderen Objekte weiter. Die geparkten Ereignisse eines Objekts bilden eine Kette nach
Folgenummer: Nur das erste ist ``wiederholen`` oder ``tot``, alle weiteren sind ``blockiert``. Ist
das erste zugestellt oder verworfen, rückt das nächste nach und wird sofort zugestellt.

**Transaktionen:**

- Datenbank-Sichten (``transactional=True``): Ein Lauf sperrt die Zeile des Abonnements
  (``SELECT … FOR UPDATE``) und ruft den Handler in einem Sicherungspunkt derselben Transaktion auf,
  in der auch Parken und Cursor festgeschrieben werden. Der Effekt tritt genau einmal ein, auch
  wenn zwei Prozesse dasselbe Abonnement bedienen; sie warten dann aufeinander.
- Externe Effekte (``transactional=False``): Der Handler läuft außerhalb einer Transaktion. Danach
  schreibt eine kurze Transaktion Parken und Cursor fest, aber nur, wenn der Cursor unter
  Zeilensperre noch derselbe ist. Sonst hat ein anderer Prozess denselben Batch schon
  abgeschlossen; dieser Lauf verwirft sein Ergebnis, und die doppelte Zustellung fängt die
  Idempotenz des Handlers ab. Weil die geparkten Objekte vor dem Handler ohne Sperre gelesen
  werden, prüft das Festschreiben die Ketten der mitgeparkten Objekte: Fehlt ihnen inzwischen das
  erste Ereignis (zugestellt oder verworfen), rückt das nächste sofort nach.

Eine Leader-Lease je Abonnement (``dispatch:<name>``) sorgt dafür, dass normalerweise genau ein
Prozess ein Abonnement bedient; mehrere Worker teilen sich so die Abonnements. Für die Korrektheit
sorgen Zeilensperre und Vergleich, nicht die Lease.

**Zustände:** ``pausiert`` stellt nichts zu, der Cursor bleibt stehen. ``schatten`` stellt zu wie
``aktiv``, der Handler bekommt aber ``delivery.shadow=True`` und schreibt in sein Schattenziel.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.db.models import Exists, Max, OuterRef
from django.db.models.functions import Now

from . import leases
from .metrics import DEAD, DELIVERED, DELIVERY_FAILURES
from .models import Event, ParkedEvent, ParkedState, Subscription, SubscriptionState
from .registry import Delivery, Subscriber, TargetUnavailableError

logger = logging.getLogger(__name__)

#: Versuche je Ereignis, danach ist es tot
MAX_ATTEMPTS = 8
#: Wartezeit nach dem 1., 2., … 7. gescheiterten Versuch
BACKOFF: tuple[timedelta, ...] = (
    timedelta(seconds=10),
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(hours=1),
    timedelta(hours=3),
    timedelta(hours=6),
)
#: Abfrageabstand im Dauerbetrieb (Sekunden); der Weckruf per LISTEN folgt mit #505
POLL_INTERVAL = 2.0
#: Pause, wenn ein Ziel nicht erreichbar ist oder ein Lauf unerwartet scheitert: wächst bis zur Obergrenze
PAUSE_MIN = 5.0
PAUSE_MAX = 300.0
#: So viele tote Ereignisse eines Laufs werden einzeln protokolliert
_TOTE_IM_LOG = 20


def backoff(attempts: int) -> timedelta:
    """Wartezeit nach dem ``attempts``-ten gescheiterten Versuch."""
    return BACKOFF[min(max(attempts, 1), len(BACKOFF)) - 1]


def error_code(exc: BaseException) -> str:
    """Ausnahmeklasse als Fehlercode; nie die Meldung, sie kann Inhalte enthalten."""
    klasse = type(exc)
    return f"{klasse.__module__}.{klasse.__qualname__}"[:200]


@dataclass
class RunResult:
    """Ergebnis eines Laufs."""

    delivered: int = 0
    parked: int = 0
    dead: int = 0
    #: Es gibt vermutlich sofort weitere Arbeit (voller Batch oder nachgerückte Ereignisse)
    more: bool = False

    def __add__(self, other: RunResult) -> RunResult:
        return RunResult(
            delivered=self.delivered + other.delivered,
            parked=self.parked + other.parked,
            dead=self.dead + other.dead,
            more=self.more or other.more,
        )


@dataclass(frozen=True)
class _Stand:
    cursor: int
    state: str
    #: Datenbankzeit; Wartezeiten rechnet die Datenbank, nicht die Uhr des Prozesses
    jetzt: datetime

    @property
    def shadow(self) -> bool:
        return self.state == SubscriptionState.SCHATTEN


@dataclass
class _Ausgang:
    """Was ein Handler-Aufruf aus einer Liste von Ereignissen gemacht hat."""

    zugestellt: list[Event] = field(default_factory=list)
    gescheitert: list[tuple[Event, str]] = field(default_factory=list)
    #: Folgeereignisse eines Objekts, das in dieser Liste gescheitert ist
    blockiert: list[Event] = field(default_factory=list)


def _seq(ereignis: Event) -> int:
    if ereignis.seq is None:  # Zugestellt werden nur nummerierte Ereignisse
        raise ValueError("Ereignis ohne Folgenummer")
    return ereignis.seq


# --- Zustand des Abonnements -------------------------------------------------------------------


def head_seq() -> int:
    """Höchste vergebene Folgenummer (0 bei leerem Journal)."""
    return Event.objects.aggregate(hoechste=Max("seq"))["hoechste"] or 0


def ensure_subscription(spec: Subscriber) -> bool:
    """Legt die Zeile des Abonnements an, falls sie fehlt; ``True``, wenn sie neu ist.

    Ein neues Abonnement beginnt am Ende des Journals (oder mit ``from_beginning`` am Anfang) und
    im Zustand ``schatten``, wenn es mit ``shadow=True`` registriert ist. Danach gilt die Zeile.
    """
    cursor = 0 if spec.from_beginning else head_seq()
    zustand = SubscriptionState.SCHATTEN if spec.shadow else SubscriptionState.AKTIV
    with transaction.atomic():
        _, neu = Subscription.objects.get_or_create(name=spec.name, defaults={"cursor_seq": cursor, "state": zustand})
    if neu:
        logger.info("Abonnement %s angelegt (Cursor %s, %s)", spec.name, cursor, zustand)
    return neu


def _stand(spec: Subscriber, *, sperren: bool) -> _Stand:
    for _ in range(2):
        abfrage = Subscription.objects.filter(name=spec.name)
        if sperren:
            abfrage = abfrage.select_for_update()
        zeile = abfrage.annotate(jetzt=Now()).values_list("cursor_seq", "state", "jetzt").first()
        if zeile is not None:
            cursor, zustand, jetzt = zeile
            return _Stand(cursor=cursor, state=zustand, jetzt=jetzt)
        ensure_subscription(spec)
    raise RuntimeError(f"Abonnement {spec.name} konnte nicht angelegt werden")


def _cursor_setzen(name: str, cursor: int) -> None:
    Subscription.objects.filter(name=name).update(cursor_seq=cursor, updated_at=Now())


# --- Handler aufrufen ----------------------------------------------------------------------------


def _aufrufen(spec: Subscriber, ereignisse: list[Event], delivery: Delivery) -> None:
    if spec.transactional:
        # Sicherungspunkt: Ein gescheiterter Aufruf hinterlässt keine halben Änderungen
        with transaction.atomic():
            spec.handler(list(ereignisse), delivery)
    else:
        spec.handler(list(ereignisse), delivery)


def _transaktion_unbrauchbar(spec: Subscriber) -> bool:
    """Konnte der Sicherungspunkt nicht zurückgerollt werden (Verbindung weg), ist der Lauf verloren."""
    return spec.transactional and transaction.get_rollback()


def _zustellen(spec: Subscriber, ereignisse: list[Event], delivery: Delivery) -> _Ausgang:
    """Übergibt die Ereignisse dem Handler, bei einem Fehler einzeln; ``TargetUnavailableError`` bricht ab."""
    ausgang = _Ausgang()
    if not ereignisse:
        return ausgang
    if len(ereignisse) > 1:
        try:
            _aufrufen(spec, ereignisse, delivery)
        except TargetUnavailableError:
            raise
        except Exception:
            if _transaktion_unbrauchbar(spec):
                raise
            logger.info("Abonnement %s: Batch gescheitert, Ereignisse werden einzeln zugestellt", spec.name)
        else:
            ausgang.zugestellt.extend(ereignisse)
            return ausgang

    gesperrt: set[uuid.UUID] = set()
    for ereignis in ereignisse:
        if ereignis.aggregate_id in gesperrt:
            ausgang.blockiert.append(ereignis)
            continue
        try:
            _aufrufen(spec, [ereignis], delivery)
        except TargetUnavailableError:
            raise
        except Exception as exc:
            if _transaktion_unbrauchbar(spec):
                raise
            code = error_code(exc)
            ausgang.gescheitert.append((ereignis, code))
            gesperrt.add(ereignis.aggregate_id)
            logger.warning(
                "Zustellung gescheitert: Abonnement %s, Folgenummer %s, Objekt %s (%s)",
                spec.name,
                ereignis.seq,
                ereignis.aggregate_id,
                code,
                exc_info=exc,
            )
        else:
            ausgang.zugestellt.append(ereignis)
    return ausgang


def _zaehlen(name: str, zugestellt: int, gescheitert: int) -> None:
    if zugestellt:
        DELIVERED.labels(subscription=name).inc(zugestellt)
    if gescheitert:
        DELIVERY_FAILURES.labels(subscription=name).inc(gescheitert)


# --- fortlaufende Zustellung ---------------------------------------------------------------------


def _lesen(spec: Subscriber, cursor: int) -> tuple[list[Event], int, bool]:
    """Nächster Batch ab ``cursor``: Ereignisse, neuer Cursor, ob sofort weitere folgen könnten."""
    ende = head_seq()
    if ende <= cursor:
        return [], cursor, False
    ereignisse = list(
        Event.objects.filter(seq__gt=cursor, seq__lte=ende).filter(spec.type_filter()).order_by("seq")[: spec.batch]
    )
    if len(ereignisse) == spec.batch:
        return ereignisse, _seq(ereignisse[-1]), True
    return ereignisse, ende, False


def _strom_zustellen(spec: Subscriber, stand: _Stand, ereignisse: list[Event]) -> _Ausgang:
    objekte = {ereignis.aggregate_id for ereignis in ereignisse}
    geparkt = set(
        ParkedEvent.objects.filter(subscription=spec.name, aggregate_id__in=objekte)
        .values_list("aggregate_id", flat=True)
        .distinct()
    )
    zustellbar = [ereignis for ereignis in ereignisse if ereignis.aggregate_id not in geparkt]
    ausgang = _zustellen(spec, zustellbar, Delivery(subscription=spec.name, shadow=stand.shadow))
    ausgang.blockiert += [ereignis for ereignis in ereignisse if ereignis.aggregate_id in geparkt]
    return ausgang


def _strom_abschliessen(spec: Subscriber, stand: _Stand, ausgang: _Ausgang, cursor: int, mehr: bool) -> RunResult:
    """Parkt Gescheitertes und Blockiertes und setzt den Cursor, in der laufenden Transaktion."""
    zeilen = [
        ParkedEvent(
            subscription=spec.name,
            event_seq=_seq(ereignis),
            aggregate_id=ereignis.aggregate_id,
            state=ParkedState.WIEDERHOLEN,
            attempts=1,
            next_attempt_at=stand.jetzt + backoff(1),
            error_code=code,
        )
        for ereignis, code in ausgang.gescheitert
    ]
    zeilen += [
        ParkedEvent(
            subscription=spec.name,
            event_seq=_seq(ereignis),
            aggregate_id=ereignis.aggregate_id,
            state=ParkedState.BLOCKIERT,
            attempts=0,
        )
        for ereignis in ausgang.blockiert
    ]
    # Nach einem Zurücksetzen des Cursors (Nachspielen) kann ein Ereignis schon geparkt sein
    ParkedEvent.objects.bulk_create(zeilen, ignore_conflicts=True)
    # Externe Handler: Welche Objekte geparkt sind, wurde vor dem Handler ohne Sperre gelesen. Ist das
    # erste Ereignis einer Kette seitdem zugestellt oder verworfen worden, hätten die eben mitgeparkten
    # Ereignisse keinen Kopf mehr und würden nie fällig. Unter der Sperre rückt deshalb nach, wo nötig.
    nachgerueckt = _nachruecken(spec.name, {ereignis.aggregate_id for ereignis in ausgang.blockiert})
    _cursor_setzen(spec.name, cursor)
    transaction.on_commit(functools.partial(_zaehlen, spec.name, len(ausgang.zugestellt), len(ausgang.gescheitert)))
    return RunResult(delivered=len(ausgang.zugestellt), parked=len(zeilen), more=mehr or nachgerueckt > 0)


def _fortlaufend(spec: Subscriber) -> RunResult:
    if spec.transactional:
        with transaction.atomic():
            stand = _stand(spec, sperren=True)
            if stand.state == SubscriptionState.PAUSIERT:
                return RunResult()
            ereignisse, cursor, mehr = _lesen(spec, stand.cursor)
            if cursor == stand.cursor:
                return RunResult()
            ausgang = _strom_zustellen(spec, stand, ereignisse)
            return _strom_abschliessen(spec, stand, ausgang, cursor, mehr)

    stand = _stand(spec, sperren=False)
    if stand.state == SubscriptionState.PAUSIERT:
        return RunResult()
    ereignisse, cursor, mehr = _lesen(spec, stand.cursor)
    if cursor == stand.cursor:
        return RunResult()
    ausgang = _strom_zustellen(spec, stand, ereignisse)
    with transaction.atomic():
        aktuell = _stand(spec, sperren=True)
        if aktuell.cursor != stand.cursor:
            logger.warning(
                "Abonnement %s: ein anderer Prozess hat den Batch ab %s schon abgeschlossen; Ergebnis verworfen",
                spec.name,
                stand.cursor,
            )
            return RunResult(more=True)
        return _strom_abschliessen(spec, aktuell, ausgang, cursor, mehr)


# --- Wiederholung geparkter Ereignisse -----------------------------------------------------------


def _faellige(name: str, jetzt: datetime, limit: int) -> list[ParkedEvent]:
    return list(
        ParkedEvent.objects.filter(
            subscription=name, state=ParkedState.WIEDERHOLEN, next_attempt_at__lte=jetzt
        ).order_by("event_seq")[:limit]
    )


def _nachruecken(name: str, objekte: set[uuid.UUID]) -> int:
    """Macht das nächste blockierte Ereignis jedes Objekts sofort fällig; gibt deren Zahl zurück."""
    nachgerueckt = 0
    for objekt in objekte:
        naechstes = (
            ParkedEvent.objects.filter(subscription=name, aggregate_id=objekt)
            .order_by("event_seq")
            .values_list("pk", "state")
            .first()
        )
        if naechstes is not None and naechstes[1] == ParkedState.BLOCKIERT:
            ParkedEvent.objects.filter(pk=naechstes[0]).update(state=ParkedState.WIEDERHOLEN, next_attempt_at=Now())
            nachgerueckt += 1
    return nachgerueckt


def _tot_melden(name: str, tote: list[tuple[int, uuid.UUID, str]]) -> None:
    """Alarm nach dem Commit: Metrik, Fehlerprotokoll und Alarmmail (höchstens eine je Abonnement und Tag)."""
    DEAD.labels(subscription=name).inc(len(tote))
    for seq, objekt, code in tote[:_TOTE_IM_LOG]:
        logger.error(
            "Ereignis nach %s Versuchen aufgegeben (tot): Abonnement %s, Folgenummer %s, Objekt %s (%s)",
            MAX_ATTEMPTS,
            name,
            seq,
            objekt,
            code,
        )
    try:
        from apps.common.service_levels import pruefe_tote_ereignisse, sende_alarme

        sende_alarme(pruefe_tote_ereignisse())
    except Exception:  # noqa: BLE001 – der Alarmweg darf die Zustellung nie anhalten
        logger.warning("Alarm zu toten Ereignissen nicht versendet", exc_info=True)


def _wiederholung_abschliessen(
    spec: Subscriber, stand: _Stand, faellig: list[ParkedEvent], vorhanden: set[int], ausgang: _Ausgang
) -> RunResult:
    """Löscht Zugestelltes, zählt Fehlversuche, markiert Tote; in der laufenden Transaktion."""
    zugestellt = {_seq(ereignis) for ereignis in ausgang.zugestellt}
    gescheitert = {_seq(ereignis): code for ereignis, code in ausgang.gescheitert}
    fehlend = [geparkt for geparkt in faellig if geparkt.event_seq not in vorhanden]
    if fehlend:
        # Das Journal hat das Ereignis nicht mehr (Aufbewahrungsfrist); die Kette darf nicht hängen
        logger.warning(
            "Abonnement %s: %s geparkte Ereignisse ohne Journaleintrag verworfen (Folgenummern %s)",
            spec.name,
            len(fehlend),
            [geparkt.event_seq for geparkt in fehlend],
        )
    zugestellt_hier = [geparkt for geparkt in faellig if geparkt.event_seq in zugestellt]
    erledigt = zugestellt_hier + fehlend
    ParkedEvent.objects.filter(pk__in=[geparkt.pk for geparkt in erledigt]).delete()
    nachgerueckt = _nachruecken(spec.name, {geparkt.aggregate_id for geparkt in erledigt})

    tote: list[tuple[int, uuid.UUID, str]] = []
    fehlversuche = 0
    for geparkt in faellig:
        code = gescheitert.get(geparkt.event_seq)
        if code is None:
            continue
        fehlversuche += 1
        versuche = geparkt.attempts + 1
        if versuche >= MAX_ATTEMPTS:
            ParkedEvent.objects.filter(pk=geparkt.pk).update(
                state=ParkedState.TOT, attempts=versuche, next_attempt_at=None, error_code=code
            )
            tote.append((geparkt.event_seq, geparkt.aggregate_id, code))
        else:
            ParkedEvent.objects.filter(pk=geparkt.pk).update(
                attempts=versuche, next_attempt_at=stand.jetzt + backoff(versuche), error_code=code
            )
    transaction.on_commit(functools.partial(_zaehlen, spec.name, len(zugestellt_hier), fehlversuche))
    if tote:
        transaction.on_commit(functools.partial(_tot_melden, spec.name, tote))
    return RunResult(
        delivered=len(zugestellt_hier),
        dead=len(tote),
        more=len(faellig) == spec.batch or nachgerueckt > 0 or bool(fehlend),
    )


def _ereignisse_zu(faellig: list[ParkedEvent]) -> list[Event]:
    return list(Event.objects.filter(seq__in=[geparkt.event_seq for geparkt in faellig]).order_by("seq"))


def _wiederholen(spec: Subscriber) -> RunResult:
    if spec.transactional:
        with transaction.atomic():
            stand = _stand(spec, sperren=True)
            if stand.state == SubscriptionState.PAUSIERT:
                return RunResult()
            faellig = _faellige(spec.name, stand.jetzt, spec.batch)
            if not faellig:
                return RunResult()
            ereignisse = _ereignisse_zu(faellig)
            delivery = Delivery(subscription=spec.name, shadow=stand.shadow, retry=True)
            ausgang = _zustellen(spec, ereignisse, delivery)
            return _wiederholung_abschliessen(spec, stand, faellig, {_seq(e) for e in ereignisse}, ausgang)

    stand = _stand(spec, sperren=False)
    if stand.state == SubscriptionState.PAUSIERT:
        return RunResult()
    faellig = _faellige(spec.name, stand.jetzt, spec.batch)
    if not faellig:
        return RunResult()
    ereignisse = _ereignisse_zu(faellig)
    ausgang = _zustellen(spec, ereignisse, Delivery(subscription=spec.name, shadow=stand.shadow, retry=True))
    with transaction.atomic():
        aktuell = _stand(spec, sperren=True)
        # Nur Zeilen abschließen, die seitdem niemand verändert hat (anderer Prozess, Verwerfen im Admin)
        unveraendert = set(
            ParkedEvent.objects.filter(
                pk__in=[geparkt.pk for geparkt in faellig], state=ParkedState.WIEDERHOLEN
            ).values_list("pk", "attempts")
        )
        faellig = [geparkt for geparkt in faellig if (geparkt.pk, geparkt.attempts) in unveraendert]
        return _wiederholung_abschliessen(spec, aktuell, faellig, {_seq(e) for e in ereignisse}, ausgang)


# --- ein Lauf ------------------------------------------------------------------------------------


def deliver_batch(spec: Subscriber) -> RunResult:
    """Ein Lauf für ein Abonnement: fällige Wiederholungen, dann der nächste Batch ab dem Cursor.

    Wirft ``TargetUnavailableError``, wenn der Handler das Ziel als nicht erreichbar meldet; dann
    ist von diesem Schritt nichts festgeschrieben.
    """
    return _wiederholen(spec) + _fortlaufend(spec)


# --- Eingriffe (Betrieb, später Admin-Seite) -----------------------------------------------------


def _mit_sperre_des_abonnements(name: str) -> None:
    """Sperrt die Zeile des Abonnements; Eingriffe warten so auf einen laufenden Zustellungslauf."""
    list(Subscription.objects.select_for_update().filter(name=name).values_list("name", flat=True))


def discard_parked(parked_id: int) -> bool:
    """Verwirft ein geparktes Ereignis (etwa ein totes); das nächste desselben Objekts rückt nach."""
    with transaction.atomic():
        geparkt = ParkedEvent.objects.filter(pk=parked_id).first()
        if geparkt is None:
            return False
        _mit_sperre_des_abonnements(geparkt.subscription)
        if not ParkedEvent.objects.filter(pk=parked_id).delete()[0]:
            return False
        _nachruecken(geparkt.subscription, {geparkt.aggregate_id})
        logger.warning(
            "Geparktes Ereignis verworfen: Abonnement %s, Folgenummer %s", geparkt.subscription, geparkt.event_seq
        )
        return True


def retry_parked(parked_id: int) -> bool:
    """Stellt das erste geparkte Ereignis eines Objekts (etwa ein totes) sofort erneut zu, mit allen Versuchen.

    Folgeereignisse eines Objekts lassen sich nicht vorziehen, sonst litte die Reihenfolge.
    """
    with transaction.atomic():
        geparkt = ParkedEvent.objects.filter(pk=parked_id).first()
        if geparkt is None:
            return False
        _mit_sperre_des_abonnements(geparkt.subscription)
        vorgaenger = ParkedEvent.objects.filter(
            subscription=geparkt.subscription, aggregate_id=geparkt.aggregate_id, event_seq__lt=geparkt.event_seq
        )
        if vorgaenger.exists():
            return False
        return bool(
            ParkedEvent.objects.filter(pk=parked_id).update(
                state=ParkedState.WIEDERHOLEN, attempts=0, next_attempt_at=Now()
            )
        )


def repair_chains(name: str) -> int:
    """Macht Ketten ohne erstes Ereignis wieder zustellbar (etwa nach Löschen von Hand); gibt die Zahl zurück."""
    vorgaenger = ParkedEvent.objects.filter(
        subscription=name, aggregate_id=OuterRef("aggregate_id"), event_seq__lt=OuterRef("event_seq")
    )
    with transaction.atomic():
        _mit_sperre_des_abonnements(name)
        waisen = list(
            ParkedEvent.objects.filter(subscription=name, state=ParkedState.BLOCKIERT)
            .filter(~Exists(vorgaenger))
            .values_list("pk", flat=True)
        )
        if waisen:
            ParkedEvent.objects.filter(pk__in=waisen).update(state=ParkedState.WIEDERHOLEN, next_attempt_at=Now())
            logger.warning("Abonnement %s: %s Ketten geparkter Ereignisse wieder freigegeben", name, len(waisen))
    return len(waisen)


# --- Dauerbetrieb --------------------------------------------------------------------------------


@dataclass
class SubscriptionLoop:
    """Schleife für ein Abonnement: Lease halten, zustellen, bei Bedarf pausieren."""

    spec: Subscriber
    holder: str = field(default_factory=leases.new_holder_id)
    is_leader: bool = False
    _renewed_at: float = field(default=0.0, init=False, repr=False)
    _pause: float = field(default=0.0, init=False, repr=False)
    _paused_until: float = field(default=0.0, init=False, repr=False)

    def ensure_lease(self) -> bool:
        """Übernimmt die Lease oder erneuert sie, wenn die Erneuerung fällig ist."""
        faellig = time.monotonic() - self._renewed_at >= leases.RENEW_INTERVAL.total_seconds()
        if self.is_leader and not faellig:
            return True
        war_leader = self.is_leader
        self.is_leader = leases.acquire(self.spec.lease_name, self.holder)
        if self.is_leader:
            self._renewed_at = time.monotonic()
            if not war_leader:
                logger.info("Zustellung %s: Lease übernommen (%s)", self.spec.name, self.holder)
                ensure_subscription(self.spec)
                repair_chains(self.spec.name)
        elif war_leader:
            logger.warning("Zustellung %s: Lease an einen anderen Prozess verloren (%s)", self.spec.name, self.holder)
        return self.is_leader

    @property
    def paused(self) -> bool:
        return time.monotonic() < self._paused_until

    def _aussetzen(self) -> None:
        self._pause = min(PAUSE_MAX, max(PAUSE_MIN, self._pause * 2))
        self._paused_until = time.monotonic() + self._pause

    def drain(self, stop: threading.Event | None = None) -> int:
        """Stellt zu, bis nichts Fälliges mehr übrig ist; gibt die Zahl zugestellter Ereignisse zurück."""
        gesamt = 0
        # Die Lease wird auch in einer Pause erneuert: Ein anderer Worker käme an das Ziel auch nicht heran
        while (stop is None or not stop.is_set()) and self.ensure_lease() and not self.paused:
            try:
                ergebnis = deliver_batch(self.spec)
            except TargetUnavailableError:
                self._aussetzen()
                logger.warning(
                    "Zustellung %s: Ziel nicht erreichbar, nächster Versuch in %.0f s", self.spec.name, self._pause
                )
                break
            self._pause = 0.0
            gesamt += ergebnis.delivered
            if not ergebnis.more:
                break
        return gesamt

    def release(self) -> None:
        if self.is_leader:
            leases.release(self.spec.lease_name, self.holder)
            self.is_leader = False

    def run(self, stop: threading.Event, wake: threading.Event | None = None, interval: float = POLL_INTERVAL) -> None:
        """Dauerbetrieb bis ``stop``; ein laufender Batch wird noch festgeschrieben, dann die Lease freigegeben."""
        try:
            while not stop.is_set():
                close_old_connections()  # eine im Warten veraltete Verbindung nicht weiterverwenden
                try:
                    self.drain(stop)
                except DatabaseError:
                    logger.warning("Zustellung %s: Datenbankfehler, neuer Versuch folgt", self.spec.name, exc_info=True)
                    self.is_leader = False
                    connection.close()
                except Exception:  # noqa: BLE001 – die Schleife darf nicht sterben; Pause gegen Endlosfehler
                    self._aussetzen()
                    logger.exception("Zustellung %s: unerwarteter Fehler, Pause %.0f s", self.spec.name, self._pause)
                # Verbindung vor dem Warten zurückgeben (mit Verbindungspool: an den Pool). So hängt die Zahl
                # belegter Verbindungen an der gleichzeitigen Arbeit, nicht an der Zahl der Abonnements.
                close_old_connections()
                if wake is None:
                    stop.wait(interval)
                else:
                    wake.wait(interval)
                    wake.clear()
        finally:
            try:
                self.release()
            except DatabaseError:
                logger.warning("Zustellung %s: Lease konnte nicht freigegeben werden", self.spec.name, exc_info=True)
            connection.close()


@dataclass
class Dispatcher:
    """Zustellung für mehrere Abonnements: im Dauerbetrieb je Abonnement ein Faden."""

    subscribers: list[Subscriber]
    loops: list[SubscriptionLoop] = field(init=False)
    _wake: list[threading.Event] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.loops = [SubscriptionLoop(spec) for spec in self.subscribers]
        self._wake = [threading.Event() for _ in self.loops]

    def drain(self) -> int:
        """Einmal alles Fällige zustellen (nacheinander); gibt die Zahl zugestellter Ereignisse zurück."""
        return sum(loop.drain() for loop in self.loops)

    def release(self) -> None:
        for loop in self.loops:
            try:
                loop.release()
            except DatabaseError:
                logger.warning("Zustellung %s: Lease konnte nicht freigegeben werden", loop.spec.name, exc_info=True)

    def wake(self) -> None:
        """Weckt alle Schleifen vorzeitig (z. B. nach einer neuen Folgenummer)."""
        for ereignis in self._wake:
            ereignis.set()

    def run(self, stop: threading.Event, interval: float = POLL_INTERVAL) -> None:
        """Dauerbetrieb bis ``stop``: je Abonnement ein Faden, danach werden alle Leases freigegeben."""
        faeden = [
            threading.Thread(
                target=loop.run, args=(stop, wake, interval), name=f"events-dispatch-{loop.spec.name}", daemon=True
            )
            for loop, wake in zip(self.loops, self._wake, strict=True)
        ]
        for faden in faeden:
            faden.start()
        try:
            while not stop.wait(1.0):
                pass
        finally:
            stop.set()
            self.wake()
            for faden in faeden:
                faden.join(timeout=60)

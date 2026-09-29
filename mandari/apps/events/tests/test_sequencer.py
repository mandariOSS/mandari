# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sequenzierer (Issue #503, ``docs/adr/20260929-sequenzierer.md``).

Kernnachweis ist der Nebenläufigkeitstest: 20 parallele Transaktionen mit zufälliger
Commit-Reihenfolge und zwei Langläufer, dazu ein Leser, der wie jeder Abonnent nur
``seq > cursor`` liest. Er muss jedes festgeschriebene Ereignis genau einmal sehen und kein
verworfenes. Das gelingt nur gegen echtes PostgreSQL (CI); mit SQLite wird übersprungen.

Wichtig für die CI: ``pg_snapshot_xmin`` gilt für den ganzen Cluster. Testtransaktionen anderer
pytest-Worker können den Sequenzierer deshalb kurz aufhalten; die Tests warten mit Frist, statt
feste Laufzahlen anzunehmen.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from io import StringIO
from typing import Any

import psycopg
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.utils import timezone
from prometheus_client import REGISTRY

from apps.events import leases
from apps.events.metrics import blocked_seconds, sequencer_backlog
from apps.events.models import Event, Lease
from apps.events.sequencer import LEASE_NAME, SEQUENCED_CHANNEL, Sequencer, assign_batch
from apps.events.tests.hilfen import ereignis_anlegen, folgenummern, nur_postgres, roh_einfuegen

Verbindungen = Callable[..., psycopg.Connection[Any]]

#: Frist, bis alle festgeschriebenen Ereignisse nummeriert sein müssen (Cluster-Last in der CI)
FRIST = 120.0


def _vergeben(event_ids: list[uuid.UUID]) -> list[int]:
    """Folgenummern, die alle schon vergeben sein müssen."""
    nummern = folgenummern(event_ids)
    vergeben = [nummer for nummer in nummern if nummer is not None]
    assert len(vergeben) == len(nummern), f"nicht alle Ereignisse nummeriert: {nummern}"
    return vergeben


def _bis_alle_nummeriert(sequencer: Sequencer, event_ids: list[uuid.UUID], frist: float = FRIST) -> list[int]:
    ende = time.monotonic() + frist
    while True:
        sequencer.drain()
        if None not in folgenummern(event_ids):
            return _vergeben(event_ids)
        assert time.monotonic() < ende, f"nicht alle Ereignisse nummeriert: {folgenummern(event_ids)}"
        time.sleep(0.05)


def _scheitert_an_der_abgrenzung(holder: str, frist: float = FRIST) -> None:
    """``assign_batch`` muss mit ``LeaseLostError`` abbrechen, sobald es etwas zu vergeben gäbe.

    Ohne vergebbare Zeile prüft der Lauf die Lease nicht; in der CI kann das kurz der Fall sein,
    solange Transaktionen anderer Worker die Grenze halten.
    """
    ende = time.monotonic() + frist
    while True:
        try:
            anzahl = assign_batch(holder)
        except leases.LeaseLostError:
            return
        assert anzahl == 0, "ohne Lease darf nichts vergeben werden"
        assert time.monotonic() < ende, "der Lauf hätte an der Abgrenzung scheitern müssen"
        time.sleep(0.05)


def _festgeschrieben(anzahl: int = 1) -> list[uuid.UUID]:
    with transaction.atomic():
        return [ereignis_anlegen().event_id for _ in range(anzahl)]


# --- ohne Datenbank bzw. SQLite ----------------------------------------------------------------


def test_stau_ohne_wartendes_ereignis_ist_null() -> None:
    assert blocked_seconds(None, 900.0) == 0.0


def test_stau_ist_das_alter_der_haltenden_transaktion_mindestens_die_wartezeit() -> None:
    assert blocked_seconds(12.0, 400.0) == 400.0
    # Alter unbekannt (fremde Sitzung ohne Leserecht): Wartezeit als untere Grenze
    assert blocked_seconds(12.0, None) == 12.0
    assert blocked_seconds(12.0, 3.0) == 12.0


@pytest.mark.django_db
def test_befehl_meldet_ohne_postgresql_einen_klaren_fehler() -> None:
    if connection.vendor == "postgresql":
        pytest.skip("prüft den Abbruch außerhalb von PostgreSQL")
    with pytest.raises(CommandError, match="braucht PostgreSQL"):
        call_command("events_sequencer", "--once")
    assert sequencer_backlog() is None


# --- PostgreSQL ----------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_nummern_nach_commit_in_schreibreihenfolge_und_weckruf(pg_verbindungen: Verbindungen) -> None:
    nur_postgres()
    zuhoerer = pg_verbindungen()
    zuhoerer.execute(f"LISTEN {SEQUENCED_CHANNEL}")
    erste = _festgeschrieben(3)
    zweite = _festgeschrieben(2)
    sequencer = Sequencer(batch_size=2)

    nummern = _bis_alle_nummeriert(sequencer, erste + zweite)

    assert nummern == sorted(nummern) and len(set(nummern)) == 5
    meldungen = [m for m in zuhoerer.notifies(timeout=0.5) if m.channel == SEQUENCED_CHANNEL]
    assert meldungen, "nach der Vergabe muss die Zustellung geweckt werden"


@pytest.mark.django_db(transaction=True)
def test_offene_aeltere_transaktion_haelt_zurueck_und_kommt_danach_zuerst(pg_verbindungen: Verbindungen) -> None:
    nur_postgres()
    langlaeufer = pg_verbindungen(autocommit=False)
    alt = roh_einfuegen(langlaeufer)  # erhält die kleinere Transaktionskennung, bleibt offen
    neu = _festgeschrieben()[0]
    sequencer = Sequencer()

    sequencer.drain()
    assert folgenummern([neu]) == [None], "jüngere Ereignisse warten, solange eine ältere Transaktion läuft"
    time.sleep(1.1)
    werte = sequencer_backlog()
    assert werte is not None
    stau, aelteste = werte
    assert stau >= 1.0
    assert aelteste is not None and aelteste >= 1.0
    assert (REGISTRY.get_sample_value("mandari_events_sequencer_blocked_seconds") or 0.0) >= 1.0

    langlaeufer.commit()
    nummer_alt, nummer_neu = _bis_alle_nummeriert(sequencer, [alt, neu])

    assert nummer_alt < nummer_neu, "Reihenfolge nach Transaktionskennung"
    werte = sequencer_backlog()
    assert werte is not None and werte[0] == 0.0


@pytest.mark.django_db(transaction=True)
def test_verworfene_transaktion_bekommt_keine_nummer(pg_verbindungen: Verbindungen) -> None:
    nur_postgres()
    verworfen = pg_verbindungen(autocommit=False)
    weg = roh_einfuegen(verworfen)
    verworfen.rollback()
    bleibt = _festgeschrieben()

    _bis_alle_nummeriert(Sequencer(), bleibt)

    assert not Event.objects.filter(event_id=weg).exists()


@pytest.mark.django_db(transaction=True)
def test_ohne_lease_wird_nichts_vergeben() -> None:
    nur_postgres()
    assert leases.acquire(LEASE_NAME, "anderer-prozess")
    _festgeschrieben(2)
    sequencer = Sequencer()

    assert sequencer.drain() == 0
    assert not sequencer.is_leader
    _scheitert_an_der_abgrenzung(sequencer.holder)
    assert not Event.objects.filter(seq__isnull=False).exists()


@pytest.mark.django_db(transaction=True)
def test_uebernahme_nach_ausfall_ohne_doppelte_nummern() -> None:
    nur_postgres()
    a = Sequencer()
    b = Sequencer()
    erste = _festgeschrieben(3)
    _bis_alle_nummeriert(a, erste)
    assert not b.ensure_lease(), "solange a die Lease hält, wartet b"

    # a fällt aus: Die Lease läuft ab, b übernimmt
    Lease.objects.filter(name=LEASE_NAME).update(expires_at=timezone.now() - timedelta(seconds=1))
    zweite = _festgeschrieben(3)
    nummern = _bis_alle_nummeriert(b, zweite)
    assert b.is_leader

    # a meldet sich zurück (etwa nach einer langen Pause) und darf nichts mehr vergeben
    dritte = _festgeschrieben(2)
    _scheitert_an_der_abgrenzung(a.holder)
    assert folgenummern(dritte) == [None, None]
    nachzuegler = _bis_alle_nummeriert(b, dritte)

    alle = list(Event.objects.values_list("seq", flat=True))
    assert len(alle) == len(set(alle)) == 8
    assert max(_vergeben(erste)) < min(nummern) and max(nummern) < min(nachzuegler)


@pytest.mark.django_db(transaction=True)
def test_befehl_once_nummeriert_und_gibt_die_lease_frei() -> None:
    nur_postgres()
    ereignisse = _festgeschrieben(4)
    ausgabe = StringIO()
    ende = time.monotonic() + FRIST
    while None in folgenummern(ereignisse):
        assert time.monotonic() < ende
        call_command("events_sequencer", "--once", "--batch", "3", stdout=ausgabe)

    assert "Folgenummern vergeben" in ausgabe.getvalue()
    assert not Lease.objects.filter(name=LEASE_NAME).exists()


@pytest.mark.django_db(transaction=True)
def test_befehl_once_bricht_ab_wenn_ein_anderer_die_lease_haelt() -> None:
    nur_postgres()
    assert leases.acquire(LEASE_NAME, "anderer-prozess")
    with pytest.raises(CommandError, match="Lease"):
        call_command("events_sequencer", "--once")


@pytest.mark.django_db(transaction=True)
def test_dauerbetrieb_nummeriert_laufend_und_gibt_beim_ende_frei() -> None:
    nur_postgres()
    stop = threading.Event()
    sequencer = Sequencer()

    def laufen() -> None:
        try:
            sequencer.run(stop, interval=0.05)
        finally:
            connection.close()

    faden = threading.Thread(target=laufen)
    faden.start()
    try:
        ereignisse = _festgeschrieben(3)
        ende = time.monotonic() + FRIST
        while None in folgenummern(ereignisse):
            assert time.monotonic() < ende
            time.sleep(0.05)
    finally:
        stop.set()
        faden.join(timeout=30)

    assert not faden.is_alive()
    assert not Lease.objects.filter(name=LEASE_NAME).exists()


# --- Nebenläufigkeit -----------------------------------------------------------------------------


@dataclass
class _Schreiber:
    nummer: int
    langlaeufer: bool
    verbindung: psycopg.Connection[Any]
    zufall: random.Random
    ereignisse: list[uuid.UUID] = field(default_factory=list)
    festgeschrieben: bool = False
    erstes_schreiben: float = 0.0
    commit_fertig: float = 0.0

    def __call__(self, start: threading.Barrier) -> None:
        start.wait()
        # Langläufer beginnen, nachdem die ersten anderen schon festgeschrieben haben könnten
        time.sleep(0.2 if self.langlaeufer else self.zufall.uniform(0.0, 0.4))
        self.erstes_schreiben = time.monotonic()
        for _ in range(self.zufall.randint(1, 3)):
            self.ereignisse.append(roh_einfuegen(self.verbindung))
            time.sleep(self.zufall.uniform(0.0, 0.05))
        time.sleep(1.2 if self.langlaeufer else self.zufall.uniform(0.0, 0.3))
        if self.langlaeufer or self.zufall.random() > 0.15:
            self.verbindung.commit()
            self.festgeschrieben = True
        else:
            self.verbindung.rollback()
        self.commit_fertig = time.monotonic()


@pytest.mark.django_db(transaction=True)
def test_zwanzig_parallele_transaktionen_ohne_luecke(pg_verbindungen: Verbindungen) -> None:
    nur_postgres()
    saat = random.randrange(1_000_000)
    zufall = random.Random(saat)
    schreiber = [
        _Schreiber(i, i >= 20, pg_verbindungen(autocommit=False), random.Random(zufall.random())) for i in range(22)
    ]
    leser_verbindung = pg_verbindungen()
    gesehen: list[tuple[int, uuid.UUID]] = []
    alles_fertig = threading.Event()
    sequencer = Sequencer(batch_size=7)

    def sequenzieren() -> None:
        try:
            while not alles_fertig.is_set():
                sequencer.drain()
                time.sleep(0.01)
        finally:
            sequencer.release()
            connection.close()

    def lesen() -> None:
        cursor = 0
        while True:
            letzte_runde = alles_fertig.is_set()
            zeilen = leser_verbindung.execute(
                "SELECT seq, event_id FROM events_event WHERE seq > %s ORDER BY seq", (cursor,)
            ).fetchall()
            for seq, event_id in zeilen:
                gesehen.append((seq, event_id))
                cursor = seq
            if letzte_runde:
                return
            time.sleep(0.005)

    start = threading.Barrier(len(schreiber))
    faeden = [threading.Thread(target=s, args=(start,)) for s in schreiber]
    hintergrund = [threading.Thread(target=sequenzieren), threading.Thread(target=lesen)]
    for faden in hintergrund + faeden:
        faden.start()
    for faden in faeden:
        faden.join(timeout=60)

    festgeschrieben = [e for s in schreiber if s.festgeschrieben for e in s.ereignisse]
    verworfen = [e for s in schreiber if not s.festgeschrieben for e in s.ereignisse]
    try:
        ende = time.monotonic() + FRIST
        while None in folgenummern(festgeschrieben):
            assert time.monotonic() < ende, f"Sequenzierer hängt (Saat {saat})"
            time.sleep(0.05)
    finally:
        # Der Leser liest danach noch einmal vollständig und endet
        alles_fertig.set()
        for faden in hintergrund:
            faden.join(timeout=30)

    hinweis = f"Saat {saat}"
    assert all(not faden.is_alive() for faden in faeden + hintergrund), hinweis
    assert verworfen, f"der Test soll auch verworfene Transaktionen enthalten ({hinweis})"
    gesehene_ids = [event_id for _, event_id in gesehen]
    doppelt = [e for e, n in Counter(gesehene_ids).items() if n > 1]
    assert not doppelt, f"doppelt gelesen: {doppelt} ({hinweis})"
    assert set(gesehene_ids) == set(festgeschrieben), f"Lücke: Leser hat Ereignisse übersprungen ({hinweis})"
    assert not Event.objects.filter(event_id__in=verworfen).exists(), hinweis
    gelesene_nummern = [seq for seq, _ in gesehen]
    assert gelesene_nummern == sorted(gelesene_nummern), hinweis

    nummern = {s.nummer: _vergeben(s.ereignisse) for s in schreiber if s.festgeschrieben}
    for s in schreiber:
        if s.festgeschrieben:
            eigene = nummern[s.nummer]
            assert eigene == sorted(eigene), f"Schreibreihenfolge in Transaktion {s.nummer} ({hinweis})"
    # Wer erst nach dem Commit eines anderen zu schreiben beginnt, steht mit allen Ereignissen dahinter
    for a in schreiber:
        for b in schreiber:
            if a.festgeschrieben and b.festgeschrieben and b.erstes_schreiben > a.commit_fertig:
                assert min(nummern[b.nummer]) > max(nummern[a.nummer]), f"{a.nummer} vor {b.nummer} ({hinweis})"

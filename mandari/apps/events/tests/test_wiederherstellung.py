# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wiederherstellung: Journal, Cursor und Sichten nach dem Einspielen einer Sicherung (Issue #573).

Nachgestellt wird, was ``pg_restore`` mit den Tabellen der Ereignistechnik tut: Journal, Abonnements,
geparkte Ereignisse, eine Datenbank-Sicht und die Sequenz stehen wieder auf dem Stand der Sicherung.
Ein externes Ziel (wie der Suchindex: externe Version gleich Folgenummer, ein älterer Stand verliert)
ist nicht mitgesichert und kennt die Folgenummern, die nach der Sicherung vergeben wurden. Den echten
Rundlauf mit ``pg_dump``/``pg_restore`` prüft ``.github/workflows/backup-roundtrip.yml``.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import timedelta
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.events import wiederherstellung
from apps.events.dispatch import deliver_batch, rewind
from apps.events.models import Event, Lease, Subscription
from apps.events.registry import Delivery, Subscriber, get, subscriber
from apps.events.sequencer import LEASE_NAME, Sequencer
from apps.events.tests.hilfen import Sicht, ereignis_anlegen, nur_postgres

pytestmark = pytest.mark.django_db(transaction=True)

#: Tabellen, die eine Sicherung zurückbringt (dazu die Sequenz)
_TABELLEN = ("events_event", "events_subscription", "events_parked", Sicht.TABELLE)


class ExternesZiel:
    """Wie der Suchindex: je Objekt die Version (Folgenummer); ein älterer Stand wird verworfen."""

    def __init__(self) -> None:
        self.versionen: dict[uuid.UUID, int] = {}
        self.verworfen: list[int] = []

    def handler(self, events: list[Event], delivery: Delivery) -> None:
        for ereignis in events:
            seq = ereignis.seq or 0
            if self.versionen.get(ereignis.aggregate_id, 0) >= seq:
                self.verworfen.append(seq)
            else:
                self.versionen[ereignis.aggregate_id] = seq


@pytest.fixture
def ziel(leeres_register: dict[str, Subscriber], sicht: Sicht) -> ExternesZiel:
    nur_postgres()
    extern = ExternesZiel()
    subscriber("test.sicht", types=["test.*"], from_beginning=True)(lambda events, delivery: sicht.schreiben(events))
    subscriber("test.extern", types=["test.*"], transactional=False, from_beginning=True)(extern.handler)
    return extern


def _schreiben(objekte: list[uuid.UUID], anzahl: int) -> None:
    with transaction.atomic():
        for i in range(anzahl):
            ereignis_anlegen(aggregate_id=objekte[i % len(objekte)])
    # In der CI halten offene Transaktionen paralleler Testprozesse die Grenze clusterweit kurz auf
    sequenzierer, ende = Sequencer(), time.monotonic() + 60
    while Event.objects.filter(seq__isnull=True).exists():
        assert time.monotonic() < ende, "nicht alle Ereignisse nummeriert"
        sequenzierer.drain()
        time.sleep(0.05)
    sequenzierer.release()
    for name in ("test.sicht", "test.extern"):
        while deliver_batch(get(name)).more:
            pass


def _sichern() -> None:
    with connection.cursor() as cursor:
        for tabelle in _TABELLEN:
            cursor.execute(f"DROP TABLE IF EXISTS sicherung_{tabelle}")
            cursor.execute(f"CREATE TABLE sicherung_{tabelle} AS SELECT * FROM {tabelle}")
        cursor.execute("DROP TABLE IF EXISTS sicherung_sequenz")
        cursor.execute("CREATE TABLE sicherung_sequenz AS SELECT last_value, is_called FROM events_seq")


def _wiederherstellen() -> None:
    """Wie ``pg_restore`` der ganzen Datenbank: alle Tabellen und die Sequenz auf dem Stand der Sicherung."""
    with connection.cursor() as cursor:
        for tabelle in _TABELLEN:
            cursor.execute(f"DELETE FROM {tabelle}")
            cursor.execute(f"INSERT INTO {tabelle} SELECT * FROM sicherung_{tabelle}")  # noqa: S608
            cursor.execute(f"DROP TABLE sicherung_{tabelle}")
        cursor.execute("SELECT setval('events_seq', last_value, is_called) FROM sicherung_sequenz")
        cursor.execute("DROP TABLE sicherung_sequenz")


def _hoechste() -> int:
    return Event.objects.order_by("-seq").values_list("seq", flat=True).first() or 0


def _pruefsumme(sicht: Sicht) -> str:
    return hashlib.sha256(repr(sorted((str(e), str(a), s) for e, a, s, _ in sicht.zeilen())).encode()).hexdigest()


def test_nach_wiederherstellung_neue_folgenummern_und_sichten_nachspielbar(ziel: ExternesZiel, sicht: Sicht) -> None:
    objekte = [uuid.uuid4() for _ in range(5)]
    _schreiben(objekte, 10)
    _sichern()
    gesichert = _hoechste()

    # Nach der Sicherung: weitere Ereignisse, das externe Ziel kennt ihre Folgenummern
    _schreiben(objekte, 10)
    verloren = _hoechste()
    assert ziel.versionen[objekte[0]] > gesichert

    _wiederherstellen()
    assert _hoechste() == gesichert
    assert set(Subscription.objects.values_list("cursor_seq", flat=True)) == {gesichert}
    ausgabe = StringIO()
    call_command("events_after_restore", "--apply", stdout=ausgabe)
    assert "angehoben" in ausgabe.getvalue() and "test.extern" in ausgabe.getvalue()

    # Neue Ereignisse liegen jenseits aller vergebenen Nummern: Das externe Ziel übernimmt sie
    _schreiben(objekte, 5)
    neu = [seq or 0 for seq in Event.objects.filter(seq__gt=gesichert).values_list("seq", flat=True)]
    assert len(neu) == 5 and min(neu) > gesichert + wiederherstellung.DEFAULT_GAP > verloren
    assert ziel.verworfen == [], "neue Ereignisse wären als veraltet verworfen worden"
    assert all(ziel.versionen[objekt] in neu for objekt in objekte)

    # Sicht nachspielen: löschen, ab Folgenummer 1 zustellen, gleicher Inhalt wie vorher
    vorher = _pruefsumme(sicht)
    assert len(sicht.zeilen()) == 15
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {Sicht.TABELLE}")
    rewind("test.sicht", 1)
    while deliver_batch(get("test.sicht")).more:
        pass
    assert _pruefsumme(sicht) == vorher


def test_anheben_nur_einmal_und_nicht_bei_laufendem_sequenzierer(ziel: ExternesZiel) -> None:
    _schreiben([uuid.uuid4()], 3)
    Lease.objects.create(name=LEASE_NAME, holder="anderer", expires_at=timezone.now() + timedelta(seconds=30))
    with pytest.raises(CommandError, match="Sequenzierer"):
        call_command("events_after_restore", "--apply", stdout=StringIO())

    Lease.objects.all().delete()
    erwartet = max(wiederherstellung.state().sequence_value, _hoechste()) + 1000
    call_command("events_after_restore", "--apply", "--gap", "1000", stdout=StringIO())
    assert wiederherstellung.state().sequence_value == erwartet
    ausgabe = StringIO()
    call_command("events_after_restore", "--apply", "--gap", "1000", stdout=ausgabe)
    assert "bereits angehoben" in ausgabe.getvalue()
    assert wiederherstellung.state().sequence_value == erwartet


def test_pruefen_aendert_nichts_und_meldet_cursor_hinter_dem_journal(ziel: ExternesZiel) -> None:
    _schreiben([uuid.uuid4()], 2)
    vorher = wiederherstellung.state().sequence_value
    ausgabe = StringIO()
    call_command("events_after_restore", stdout=ausgabe)
    assert wiederherstellung.state().sequence_value == vorher
    assert f"höchste Folgenummer {_hoechste()}" in ausgabe.getvalue()

    Subscription.objects.filter(name="test.sicht").update(cursor_seq=_hoechste() + 1)
    with pytest.raises(CommandError, match="test.sicht"):
        call_command("events_after_restore", stdout=StringIO())


def test_eingriff_steht_im_sicherheitsprotokoll(ziel: ExternesZiel) -> None:
    from apps.accounts.models import SecurityAuditLog

    _schreiben([uuid.uuid4()], 1)
    call_command("events_after_restore", "--apply", stdout=StringIO())
    call_command("events_after_restore", "--apply", stdout=StringIO())  # ohne Wirkung, ohne Eintrag

    (eintrag,) = SecurityAuditLog.objects.filter(event="betrieb")
    details: dict[str, Any] = eintrag.details
    assert (details["aktion"], details["befehl"], details["quelle"]) == (
        "folgenummer_angehoben",
        "events_after_restore",
        "kommandozeile",
    )
    assert details["nachher"] - details["vorher"] == wiederherstellung.DEFAULT_GAP
